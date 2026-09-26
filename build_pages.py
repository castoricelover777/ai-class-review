"""组装在线版静态站（GitHub Pages 用）。

在线版把**同一份 Python 核心**通过 Pyodide 跑在浏览器里，所以站点需要：

    index.html      界面（就是 exe 用的那一份，界面里有后端自动探测）
    core/*.py       解析 / 名单 / 规则 / 打分 / 导出 —— 原样拷贝，不复制不改写
    .nojekyll       让 Pages 不要用 Jekyll 处理这些文件

这样做的好处是：**在线版和 exe 版共用同一份核心代码**，
不存在"网页版改了、exe 版忘了改"的问题（本地测试
``tests/test_bridge.py`` 里有用例专门盯着两者结果一致）。

用法::

    python build_pages.py             # 生成到 _site/
    python build_pages.py --out d:/x  # 指定输出目录
    python build_pages.py --serve     # 生成后用本地服务跑起来（方便预览）
"""

from __future__ import annotations

import argparse
import http.server
import shutil
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "_site"

# 浏览器版需要的模块（webapp.py / app.py 不用——那是本机服务的事）
CORE_FILES = [
    "__init__.py",
    "models.py",
    "textutil.py",
    "roster.py",
    "parser.py",
    "rules.py",
    "grader.py",
    "exporter.py",
    "config.py",
    "bridge.py",
]

# 界面依赖的静态文件（目前只有这一份 HTML，CSS/JS 都内联在里面）
WEB_FILES = ["index.html"]


def build(out: Path) -> dict[str, int]:
    if out.exists():
        shutil.rmtree(out)
    (out / "core").mkdir(parents=True)

    copied = {"html": 0, "py": 0, "bytes": 0}
    for name in WEB_FILES:
        src = ROOT / "web" / name
        if not src.is_file():
            raise SystemExit(f"缺少界面文件：{src}")
        shutil.copyfile(src, out / name)
        copied["html"] += 1
        copied["bytes"] += src.stat().st_size

    for name in CORE_FILES:
        src = ROOT / "core" / name
        if not src.is_file():
            raise SystemExit(f"缺少模块：{src}")
        shutil.copyfile(src, out / "core" / name)
        copied["py"] += 1
        copied["bytes"] += src.stat().st_size

    (out / ".nojekyll").write_text("", encoding="utf-8")
    return copied


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="组装在线版静态站")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--serve", action="store_true", help="生成后本地起服务预览")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    out = Path(args.out)
    stats = build(out)
    print(f"已生成：{out}")
    print(f"  界面 {stats['html']} 个、Python 模块 {stats['py']} 个，共 {stats['bytes'] / 1024:.1f} KB")

    if not args.serve:
        print("\n本地预览：")
        print(f"  cd {out} && python -m http.server {args.port}")
        print(f"  然后浏览器打开 http://127.0.0.1:{args.port}/")
        return 0

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(out), **kw)

        def log_message(self, fmt, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"\n本地预览：http://127.0.0.1:{args.port}/   （Ctrl+C 退出）")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
