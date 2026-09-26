"""课堂心得评阅工具 —— 程序入口。

双击 exe（或 ``python app.py``）后会在本机起一个服务并自动打开浏览器，
老师全程在网页里操作；**关掉这个黑窗口就等于退出程序**。

命令行参数
----------
``--port 8765``     指定端口（被占用时会自动往后找）
``--no-browser``    不自动打开浏览器
``--selftest``      自检：不打开浏览器，把「解析→打分→导出」全流程跑一遍并输出结果
``--host 0.0.0.0``  允许同网段其它电脑访问（演示用）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

from core.config import APP_NAME, app_dir, config_path, is_frozen
from core.webapp import DEFAULT_PORT, create_server, serve

BANNER = """
================================================================
  {app}
================================================================

  程序已经启动，请在弹出的浏览器里操作。

  界面地址：{url}
  （如果浏览器没有自动打开，请手动把上面的地址复制到浏览器）

  配置文件：{cfg}

  ★ 用完之后，直接关掉这个黑色窗口就等于退出程序。
  ★ 网页右上角也有「退出程序」按钮。

================================================================
"""


def build_banner(state, url: str) -> str:
    return BANNER.format(app=APP_NAME, url=url, cfg=config_path())


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


def _force_utf8_output() -> None:
    """中文输出的兜底。

    真实的控制台窗口里 Python 走 Windows Unicode API，中文本来就正常；
    但输出被重定向到管道/文件时，Python 会用系统 ANSI 代码页（简体中文是 GBK），
    中文就变乱码。统一改成 UTF-8，两种场景都正常。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def find_running_instance(host: str, port: int) -> str | None:
    """看看这个端口上是不是已经有一个本程序在跑了。

    老师很可能会双击好几次 exe。如果已经有一个在跑，就不要再起第二个，
    直接把浏览器指到已有的那个界面上——否则会出现"两个界面、两份成绩"的混乱。
    """
    import json
    import urllib.error
    import urllib.request

    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("ok") and "version" in data:
            return f"http://{host}:{port}/"
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return None


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(prog=APP_NAME, description="AI 辅助课堂心得评阅与打分")
    parser.add_argument("--port", type=int, default=None, help=f"服务端口（默认 {DEFAULT_PORT}，也可用环境变量 PORT）")
    parser.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1，也可用环境变量 HOST）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--selftest", action="store_true", help="跑一遍全流程自检后退出")
    args = parser.parse_args(argv)

    # 端口/监听地址支持环境变量，方便部署到容器托管平台
    port = args.port if args.port is not None else _env_int("PORT", DEFAULT_PORT)
    host = args.host or os.environ.get("HOST", "127.0.0.1")
    args.port, args.host = port, host

    if args.selftest:
        return selftest()

    # 已经有一个在跑了？直接把浏览器指过去，不要再起第二个。
    existing = find_running_instance(args.host, args.port)
    if existing and not args.no_browser:
        print(f"\n程序已经在运行了，正在打开已有的界面：{existing}\n")
        webbrowser.open(existing)
        _pause_if_frozen()
        return 0

    try:
        server = create_server(args.port, host=args.host)
    except OSError as exc:
        print(f"\n启动失败：{exc}\n")
        _pause_if_frozen()
        return 1

    port = server.state.port
    url = f"http://{args.host}:{port}/"

    print(build_banner(server.state, url))
    sys.stdout.flush()

    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    try:
        serve(server)
    except KeyboardInterrupt:
        print("\n已退出。")
    print("\n程序已退出，可以关闭这个窗口了。")
    _pause_if_frozen()
    return 0


def _pause_if_frozen() -> None:
    """exe 双击运行时，出错退出也别让窗口一闪而过。"""
    if is_frozen() and sys.stdin and sys.stdin.isatty():
        try:
            input("按回车键关闭窗口…")
        except EOFError:
            pass


# --------------------------------------------------------------------------- #
# 自检：把全流程跑一遍，用于验证打包出来的 exe 真的能用
# --------------------------------------------------------------------------- #

SAMPLE_CHAT = """张三
2026年09月26日 20:15
今天的课让我理解了三次握手的必要性。以前只知道背概念，现在明白了为什么是三次。

李四
2026年09月26日 20:20
我是李四，学号 2023123456。三权分立的核心在于制衡，这让我重新理解了制度设计。

王五
2026年09月26日 20:30
通过本次课程的学习，我深刻认识到团队合作具有重要意义。首先，我们要不断学习。
其次，我们要勇于创新。最后，我们要勇于担当。总而言之，这次课程让我受益匪浅。
"""

SAMPLE_ROSTER = "姓名,学号,群昵称\n张三,2023123456,张三\n李四,2023123457,李四\n"


def selftest() -> int:
    """不依赖浏览器和 API Key，验证进程内的完整链路。返回 0 表示全部通过。"""
    import io
    import urllib.request

    _force_utf8_output()
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1) 界面文件
    from core.webapp import load_index_html

    html = load_index_html()
    check("界面文件已内置", "课堂心得" in html and len(html) > 2000, f"{len(html)} 字节")

    # 2) 起服务
    server = create_server(DEFAULT_PORT + 100)
    port = server.state.port
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=serve, args=(server,), daemon=True)
    thread.start()
    time.sleep(0.4)

    def post(path: str, payload: dict | None = None) -> tuple[int, bytes, dict]:
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            base + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read(), dict(resp.headers)

    def get(path: str) -> tuple[int, bytes]:
        with urllib.request.urlopen(base + path, timeout=60) as resp:
            return resp.status, resp.read()

    try:
        status, body = get("/")
        check("GET / 返回界面", status == 200 and "课堂心得" in body.decode("utf-8"),
              f"HTTP {status}")

        status, body = get("/api/config")
        cfg = json.loads(body)
        check("GET /api/config", status == 200 and cfg.get("ok") is True,
              f"mode={cfg.get('config', {}).get('mode')}")

        status, body = get("/api/rubric")
        rubric = json.loads(body)["rubric"]
        check("GET /api/rubric", rubric.get("total") == 10,
              f"总分 {rubric.get('total')}，维度 {len(rubric['criteria'])} 个")

        status, body, _ = post("/api/parse", {
            "chat_text": SAMPLE_CHAT, "roster_text": SAMPLE_ROSTER, "min_chars": 15,
        })
        parsed = json.loads(body)
        subs = parsed.get("submissions", [])
        check("POST /api/parse 解析出 3 人", len(subs) == 3,
              "、".join(f"{s['name']}({s['char_count']}字)" for s in subs))
        check("名单映射生效（保留群昵称）", any(s["name"] == "李四" for s in subs),
              f"学号 {[s.get('student_id') for s in subs]}")

        status, body, _ = post("/api/grade", {
            "submissions": subs, "rubric": rubric, "mode": "mock",
        })
        started = json.loads(body)
        job_id = started.get("job_id")
        check("POST /api/grade 建任务", bool(job_id), f"job={job_id} mode={started.get('mode')}")

        results: list[dict] = []
        for _ in range(60):
            time.sleep(0.3)
            _, body = get(f"/api/grade/status?job_id={job_id}")
            snap = json.loads(body)
            if snap.get("state") in ("done", "error"):
                results = snap.get("results", [])
                break
        check("打分完成", len(results) == 3,
              "、".join(f"{r['name']}={r['total']}" for r in results))
        check("同批次雷同被检出（王五 vs 其它）", any(r.get("ai_signals") for r in results),
              "AI 味信号已计算")

        status, body, headers = post("/api/export", {
            "results": results, "rubric": rubric, "class_name": "计科2201", "mock": True,
        })
        ok_zip = body[:2] == b"PK"
        check("POST /api/export 产出 xlsx", status == 200 and ok_zip and len(body) > 5000,
              f"{len(body)} 字节")
        try:
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(body))
            check("导出的 Excel 可被打开", True, "工作表：" + "、".join(wb.sheetnames))
        except Exception as exc:  # pragma: no cover
            check("导出的 Excel 可被打开", False, str(exc))
    except Exception as exc:  # pragma: no cover
        check("自检过程异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        server.shutdown()
        server.server_close()

    failed = [c for c in checks if not c["ok"]]
    print("\n================ 自检结果 ================")
    for item in checks:
        print(f"[{'通过' if item['ok'] else '失败'}] {item['name']}"
              + (f"　—　{item['detail']}" if item["detail"] else ""))
    print(f"=========================================")
    print(f"共 {len(checks)} 项，失败 {len(failed)} 项")
    print(f"运行模式：{'已打包 exe' if is_frozen() else '源码'}")
    print(f"程序目录：{app_dir()}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
