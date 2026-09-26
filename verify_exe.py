"""验证打包出来的 exe 是不是真的能用。

模拟老师"双击 exe"的完整过程（只是不真的弹浏览器）：

1. 把 exe 当独立进程启动；
2. 轮询端口直到服务就绪（onefile 首次运行要先解压，会比较慢）；
3. 从**外部**发 HTTP 请求走一遍：看界面 → 存设置 → 解析 → 打分 → 导出 Excel；
4. 校验导出的 xlsx 能被真正打开、内容正确；
5. 点「退出程序」，确认进程真的结束了（老师关得掉）；
6. 清理测试产生的 config.ini。

用法::

    python verify_exe.py
    python verify_exe.py --exe dist\\课堂心得评阅工具.exe --port 8899 --keep-config
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_EXE = ROOT / "dist" / "课堂心得评阅工具.exe"

CHAT = """张三
2026年09月26日 20:15
老师举了个打电话的例子讲三次握手，我一下就懂了。以前只知道背概念，现在明白了为什么是三次。

李四
2026年09月26日 20:16
老师举了个打电话的例子讲三次握手，我一下就懂了。以前只知道背概念，现在明白了为什么是三次。

王五
2026年09月26日 20:17
通过本次课程的学习，我深刻认识到团队合作具有重要意义。首先，我们要不断学习新知识。
其次，我们要勇于创新。总而言之，这次课程让我受益匪浅，为我们今后的发展奠定基础。
"""

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'通过' if ok else '失败'}] {name}" + (f"　—　{detail}" if detail else ""))
    return bool(ok)


def post(base: str, path: str, payload: dict | None = None, raw: bool = False):
    data = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read()
        return resp.status, (body if raw else json.loads(body.decode("utf-8"))), dict(resp.headers)


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=60) as resp:
        return resp.status, resp.read()


def wait_ready(base: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = get(base, "/api/health")
            if status == 200:
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.6)
    return False


def port_is_busy(port: int) -> bool:
    """端口上已经有东西在监听（多半是上一轮遗留的进程）——那这次验证就不作数了。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def kill_tree(pid: int) -> None:
    """PyInstaller onefile 是「引导进程 + 工作子进程」，必须连子进程一起杀。"""
    if sys.platform != "win32":
        return
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, check=False)


def pids_listening_on(port: int) -> list[int]:
    """找出正在监听某个端口的进程（Windows 下靠 netstat 解析）。"""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace").stdout
    except OSError:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{port}"):
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    continue
    return pids


def cleanup_port(port: int) -> bool:
    """兜底清理：万一 exe 没退干净，别让它占着端口影响下次验证。"""
    killed = False
    for pid in pids_listening_on(port):
        kill_tree(pid)
        killed = True
    if killed:
        time.sleep(1.0)
    return not port_is_busy(port)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="验证打包后的 exe")
    parser.add_argument("--exe", default=str(DEFAULT_EXE))
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--keep-config", action="store_true", help="保留测试写入的 config.ini")
    args = parser.parse_args()

    exe = Path(args.exe)
    if not exe.is_file():
        print(f"找不到 exe：{exe}\n请先运行 python build.py")
        return 1

    base = f"http://127.0.0.1:{args.port}"
    config_file = exe.parent / "config.ini"
    if config_file.exists():
        config_file.unlink()

    size_mb = exe.stat().st_size / 1024 / 1024
    print(f"\n验证对象：{exe}")
    print(f"文件大小：{size_mb:.2f} MB\n")

    # 端口上如果已经挂着上一轮遗留的服务，这次验证就没意义了
    if port_is_busy(args.port):
        print(f"端口 {args.port} 上还有上一轮遗留的进程，正在清理…")
        if not cleanup_port(args.port):
            print(f"清理失败，请手动结束占用 {args.port} 的进程，或用 --port 换一个端口。")
            return 1

    print("[1] 启动 exe（模拟双击）")
    proc = subprocess.Popen(
        [str(exe), "--no-browser", "--port", str(args.port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    started = time.time()
    ready = wait_ready(base)
    check("服务在 60 秒内就绪", ready, f"耗时 {time.time() - started:.1f} 秒"
          + ("（onefile 首次启动要先解压，正常 1~3 秒）" if ready else ""))
    if not ready:
        kill_tree(proc.pid)
        print("\n服务没起来，exe 输出：")
        print(proc.stdout.read() if proc.stdout else "")
        return 1

    try:
        print("\n[2] 界面与设置")
        status, body = get(base, "/")
        html = body.decode("utf-8")
        check("打开界面", status == 200 and "课堂心得评阅台" in html, f"{len(html)} 字符")
        check("界面不依赖外部资源", "cdn" not in html and "<script src=" not in html)

        _, cfg, _ = post(base, "/api/config", {
            "api_key": "sk-faketest1234567890", "class_name": "计科2201",
        })
        check("保存设置后进入真实打分模式", cfg["config"]["mode"] == "live",
              f"脱敏 Key={cfg['config']['api_key_masked']}")
        check("config.ini 写在 exe 旁边", config_file.exists(), str(config_file))
        # 换回演示模式，避免后面真的去联网
        post(base, "/api/config", {"api_key": ""})

        print("\n[3] 解析群聊记录")
        _, parsed, _ = post(base, "/api/parse", {
            "chat_text": CHAT, "roster_text": "", "min_chars": 15,
        })
        subs = parsed["submissions"]
        check("解析出 3 位学生", len(subs) == 3,
              "、".join(f"{s['name']}({s['char_count']}字)" for s in subs))

        print("\n[4] 打分（演示模式，不联网）")
        _, rubric_body = get(base, "/api/rubric")
        rubric = json.loads(rubric_body)
        _, started_job, _ = post(base, "/api/grade", {
            "submissions": subs, "rubric": rubric["rubric"], "mode": "mock",
        })
        results = []
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(0.4)
            _, snap_body = get(base, f"/api/grade/status?job_id={started_job['job_id']}")
            snap = json.loads(snap_body)
            if snap["state"] in ("done", "error"):
                results = snap.get("results", [])
                break
        check("打分完成", len(results) == 3)
        for r in results:
            hits = "、".join(f"{h['name']} {int(h['ratio'] * 100)}%" for h in r["similarity"]) or "无"
            print(f"      {r['name']}: {r['total']}/{r['max_total']} 分　"
                  f"风险={r['risk_level']}　雷同={hits}")
        by_name = {r["name"]: r for r in results}
        check("雷同的两人被互相标出",
              bool(by_name["张三"]["similarity"]) and by_name["张三"]["similarity"][0]["name"] == "李四")
        check("套话多的那份被标记", by_name["王五"]["ai_signals"]["score"] > 0,
              f"AI 味分数 {by_name['王五']['ai_signals']['score']}")

        print("\n[5] 导出 Excel")
        _, blob, headers = post(base, "/api/export", {
            "results": results, "rubric": rubric["rubric"],
            "class_name": "计科2201", "mock": True,
        }, raw=True)
        check("返回 xlsx 二进制", blob[:2] == b"PK" and len(blob) > 5000, f"{len(blob)} 字节")
        from urllib.parse import unquote

        encoded_name = headers.get("X-Filename-Encoded", "")
        decoded_name = unquote(encoded_name) if encoded_name else ""
        check("下载文件名没乱码",
              "attachment" in headers.get("Content-Disposition", "")
              and "计科2201" in decoded_name and decoded_name.endswith(".xlsx"),
              decoded_name)
        try:
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(blob))
            check("Excel 能被打开", wb.sheetnames == ["成绩表", "评分规则", "雷同比对"],
                  "、".join(wb.sheetnames))
            ws = wb["成绩表"]
            text = "\n".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
            check("成绩表内容完整",
                  all(n in text for n in ("张三", "李四", "王五")) and "计科2201" in text
                  and "总分" in text and "已复核" in text)
        except Exception as exc:  # pragma: no cover
            check("Excel 能被打开", False, str(exc))

        print("\n[6] 退出程序")
        post(base, "/api/shutdown", {})
        time.sleep(2.5)
        try:
            get(base, "/api/health")
            still_alive = True
        except Exception:
            still_alive = False
        check("点了退出后服务停止响应", not still_alive)
        try:
            proc.wait(timeout=10)
            check("exe 进程已结束", True, f"退出码 {proc.returncode}")
        except subprocess.TimeoutExpired:
            check("exe 进程已结束", False)
            proc.kill()
    finally:
        if proc.poll() is None:
            kill_tree(proc.pid)
        cleanup_port(args.port)
        if not args.keep_config and config_file.exists():
            config_file.unlink()

    failed = [c for c in CHECKS if not c[1]]
    print("\n" + "=" * 46)
    print(f"共 {len(CHECKS)} 项，失败 {len(failed)} 项")
    print("=" * 46)
    if failed:
        for name, _, detail in failed:
            print(f"  失败：{name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
