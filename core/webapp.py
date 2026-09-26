"""本地 HTTP 服务：给 exe 里的网页界面提供 API。

只有两个第三方依赖面：``openpyxl``（导出 Excel）。
HTTP 服务用标准库 ``http.server``，大模型调用用 ``urllib``——
依赖越少，打包成 exe 越不容易出问题。

接口一览
--------
===========================  ==================================================
``GET  /``                   界面（单文件 HTML，全部内联）
``GET  /api/config``         读配置（Key 是脱敏的）
``POST /api/config``         写配置（保存 API Key / 模型 / 班级名）
``GET  /api/rubric``         默认评分规则
``POST /api/parse``          解析群聊文本 → 待评阅清单
``POST /api/grade``          开始打分（后台任务），返回 job_id
``GET  /api/grade/status``   查打分进度 / 取结果
``POST /api/export``         按当前（老师改过的）分数导出 Excel
``POST /api/shutdown``       退出程序
===========================  ==================================================
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import AppConfig, resource_path
from .exporter import suggested_filename, to_bytes
from .grader import GradeResult, grade_batch
from .parser import parse_wechat_text
from .rules import DEFAULT_RUBRIC, Rubric

__all__ = ["AppState", "create_server", "serve", "DEFAULT_PORT"]

DEFAULT_PORT = 8765
MAX_BODY = 32 * 1024 * 1024      # 32MB，防止误传超大文件把内存吃满

_FALLBACK_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>界面文件丢失</title>
<body style="font-family:system-ui;padding:48px;line-height:1.8">
<h1>界面文件丢失</h1>
<p>没有找到 <code>web/index.html</code>，也没有内置界面。</p>
<p>请重新下载完整的程序包。</p></body></html>"""


# --------------------------------------------------------------------------- #
# 应用状态
# --------------------------------------------------------------------------- #

@dataclass
class GradeJob:
    id: str
    state: str = "running"          # running / done / error
    done: int = 0
    total: int = 0
    results: list[GradeResult] = field(default_factory=list)
    error: str | None = None
    mode: str = "mock"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def snapshot(self, include_results: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "job_id": self.id,
            "state": self.state,
            "done": self.done,
            "total": self.total,
            "mode": self.mode,
            "error": self.error,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
        }
        if include_results and self.state == "done":
            data["results"] = [r.to_dict() for r in self.results]
        return data


class AppState:
    """进程内的共享状态：配置、任务表、Rubric 等。"""

    def __init__(self, config_path: Path | None = None):
        self.cfg = AppConfig.load(config_path)
        self.jobs: dict[str, GradeJob] = {}
        self.lock = threading.Lock()
        self.port = DEFAULT_PORT
        self.should_exit = False

    # ---- 配置 ---------------------------------------------------------- #

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = ("api_key", "base_url", "model", "class_name", "concurrency", "timeout")
        values = {k: payload[k] for k in allowed if k in payload}
        # 前端回传的是脱敏 Key（带 *）时不要覆盖真实 Key
        if "api_key" in values and "*" in str(values["api_key"]):
            values.pop("api_key")
        self.cfg.update(**values)
        self.cfg.save()
        return self.cfg.public_dict()

    # ---- 任务 ---------------------------------------------------------- #

    def new_job(self, mode: str, total: int) -> GradeJob:
        job = GradeJob(id=uuid.uuid4().hex[:12], total=total, mode=mode)
        with self.lock:
            self.jobs[job.id] = job
            # 只保留最近 20 个任务，避免长会话内存膨胀
            if len(self.jobs) > 20:
                for old in sorted(self.jobs.values(), key=lambda j: j.started_at)[: len(self.jobs) - 20]:
                    self.jobs.pop(old.id, None)
        return job

    def get_job(self, job_id: str) -> GradeJob | None:
        with self.lock:
            return self.jobs.get(job_id)


# --------------------------------------------------------------------------- #
# 界面文件
# --------------------------------------------------------------------------- #

def load_index_html() -> str:
    """优先读磁盘上的 ``web/index.html``（开发时改完刷新即可），
    打包后磁盘上没有这个文件，就用打包时生成的 ``core/_frontend.py``。"""
    path = resource_path("web/index.html")
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        from . import _frontend      # 打包时由 build.py 生成

        return _frontend.INDEX_HTML
    except (ImportError, AttributeError):
        return _FALLBACK_HTML


# --------------------------------------------------------------------------- #
# 请求处理
# --------------------------------------------------------------------------- #

class _Handler(BaseHTTPRequestHandler):
    server_version = "ClassReview/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础工具 ------------------------------------------------------ #

    @property
    def state(self) -> AppState:
        return self.server.state        # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        # 控制台是给老师看的，不刷 HTTP 日志；真正有意义的信息由任务线程打印。
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _fail(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("提交的内容太大了")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求内容不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求内容必须是一个 JSON 对象")
        return data

    # ---- 路由 ---------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        path = route.path
        query = parse_qs(route.query)

        if path in ("/", "/index.html"):
            html = load_index_html().encode("utf-8")
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/api/health":
            self._json({"ok": True, "pid": _pid(), "version": "1.0"})
            return
        if path == "/api/config":
            self._json({"ok": True, "config": self.state.cfg.public_dict()})
            return
        if path == "/api/rubric":
            self._json({"ok": True, "rubric": DEFAULT_RUBRIC.to_dict()})
            return
        if path == "/api/grade/status":
            job_id = (query.get("job_id") or [""])[0]
            job = self.state.get_job(job_id)
            if job is None:
                self._fail("找不到这个打分任务，可能程序重启过，请重新打分", 404)
                return
            self._json({"ok": True, **job.snapshot(include_results=True)})
            return
        self._fail(f"没有这个地址：{path}", 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._fail(str(exc))
            return

        try:
            if path == "/api/config":
                self._json({"ok": True, "config": self.state.update_config(payload)})
                return
            if path == "/api/rubric/validate":
                rubric = Rubric.from_dict(payload.get("rubric") or {})
                errors = rubric.validate()
                self._json({"ok": True, "errors": errors, "total": rubric.total})
                return
            if path == "/api/parse":
                self._json({"ok": True, **self._handle_parse(payload)})
                return
            if path == "/api/grade":
                self._json({"ok": True, **self._handle_grade(payload)})
                return
            if path == "/api/export":
                self._handle_export(payload)
                return
            if path == "/api/shutdown":
                self._json({"ok": True, "message": "程序正在退出"})
                threading.Thread(target=self._shutdown, daemon=True).start()
                return
        except ValueError as exc:
            self._fail(str(exc))
            return
        except Exception as exc:  # pragma: no cover - 兜底，避免界面直接白屏
            self._fail(f"服务器内部错误：{exc}", 500)
            return

        self._fail(f"没有这个地址：{path}", 404)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # ---- 业务 ---------------------------------------------------------- #

    def _handle_parse(self, payload: dict[str, Any]) -> dict[str, Any]:
        chat_text = str(payload.get("chat_text") or "")
        if not chat_text.strip():
            raise ValueError("请先粘贴群聊记录")
        roster_text = str(payload.get("roster_text") or "")
        exclude_raw = str(payload.get("exclude_names") or "")
        exclude = [x.strip() for x in exclude_raw.replace("\n", ",").replace("、", ",").split(",") if x.strip()]

        result = parse_wechat_text(
            chat_text,
            roster=roster_text or None,
            exclude_names=exclude,
            strict_names=bool(payload.get("strict_names")),
            min_chars=_as_int(payload.get("min_chars"), 15, 0, 100000),
        )
        submissions = [s.to_dict() for s in result.submissions]
        return {
            "submissions": submissions,
            "stats": result.stats,
            "warnings": result.warnings,
        }

    def _handle_grade(self, payload: dict[str, Any]) -> dict[str, Any]:
        submissions = payload.get("submissions")
        if not isinstance(submissions, list) or not submissions:
            raise ValueError("没有待打分的心得，请先点「解析预览」")
        if len(submissions) > 500:
            raise ValueError("一次最多评阅 500 份，请分批处理")

        rubric = Rubric.from_dict(payload.get("rubric") or {})
        errors = rubric.validate()
        if errors:
            raise ValueError("评分规则有问题：" + "；".join(errors))

        requested = payload.get("mode")
        mode = requested if requested in ("mock", "live") else self.state.cfg.mode
        if mode == "live" and not self.state.cfg.has_api_key:
            raise ValueError("还没有配置 API Key，无法真实打分（可以先点「演示模式」体验流程）")

        job = self.state.new_job(mode, len(submissions))
        cfg = self.state.cfg

        def run() -> None:
            label = "演示模式" if mode == "mock" else f"调用 {cfg.model}"
            print(f"\n开始评阅 {len(submissions)} 份心得（{label}）…", flush=True)
            try:
                def on_progress(done: int, total: int) -> None:
                    job.done = done
                    job.total = total
                    # 控制台给老师一个"程序还在干活"的反馈
                    print(f"\r  正在评阅 {done}/{total} …", end="", flush=True)

                job.results = grade_batch(
                    submissions, rubric, cfg, mode=mode, progress=on_progress
                )
                job.state = "done"
                failed = sum(1 for r in job.results if r.error)
                print(f"\r  评阅完成：{len(job.results)} 份"
                      + (f"，其中 {failed} 份失败" if failed else "")
                      + "。请回到浏览器继续操作。", flush=True)
            except Exception as exc:  # pragma: no cover - 线程内兜底
                job.error = str(exc)
                job.state = "error"
                print(f"\r  评阅中断：{exc}", flush=True)
            finally:
                job.finished_at = time.time()

        threading.Thread(target=run, daemon=True).start()
        return {"job_id": job.id, "total": job.total, "mode": mode}

    def _handle_export(self, payload: dict[str, Any]) -> None:
        raw_results = payload.get("results")
        if not isinstance(raw_results, list) or not raw_results:
            raise ValueError("还没有可导出的成绩")
        rubric = Rubric.from_dict(payload.get("rubric") or {})
        results = [GradeResult.from_dict(r) for r in raw_results if isinstance(r, dict)]
        if not results:
            raise ValueError("成绩数据不完整，无法导出")

        class_name = str(payload.get("class_name") or self.state.cfg.class_name or "")
        mock = bool(payload.get("mock")) or all(r.source == "mock" for r in results)
        data = to_bytes(results, rubric, class_name=class_name, mock=mock)
        filename = suggested_filename(rubric, class_name)
        self._send(
            200,
            data,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            extra={
                # HTTP 头只能安全传 ASCII：中文文件名一律做百分号编码，
                # 同时给 Content-Disposition 一个 ASCII 兜底名 + RFC 5987 的 filename*。
                "Content-Disposition": (
                    "attachment; filename=\"grades.xlsx\"; "
                    f"filename*=UTF-8''{_url_quote(filename)}"
                ),
                "X-Filename-Encoded": _url_quote(filename),
            },
        )

    def _shutdown(self) -> None:
        time.sleep(0.3)
        self.state.should_exit = True
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def _url_quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _pid() -> int:
    import os

    return os.getpid()


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #

def create_server(port: int = DEFAULT_PORT, config_path: Path | None = None,
                  host: str = "127.0.0.1") -> ThreadingHTTPServer:
    state = AppState(config_path)
    for candidate in range(port, port + 40):
        try:
            server = ThreadingHTTPServer((host, candidate), _Handler)
        except OSError:
            continue
        server.daemon_threads = True
        server.state = state          # type: ignore[attr-defined]
        server.verbose = False        # type: ignore[attr-defined]
        state.port = candidate
        return server
    raise OSError(f"{port}~{port + 39} 端口都被占用了，请关掉其它程序再试")


def serve(server: ThreadingHTTPServer) -> None:
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()
