"""组装在线版静态站（GitHub Pages 用）。

在线版把**同一份 Python 核心**通过 Pyodide 跑在浏览器里，所以站点需要：

    index.html      界面（就是 exe 用的那一份，界面里有后端自动探测）
    core/*.py       解析 / 名单 / 规则 / 打分 / 导出 —— 原样拷贝，不复制不改写
    vendor.zip      openpyxl + et_xmlfile（导出 Excel 用），随站点一起发
    .nojekyll       让 Pages 不要用 Jekyll 处理这些文件

**为什么要把 openpyxl 打包进去**：在线版如果改用 micropip 现装，启动就依赖
PyPI 能不能连上——网络一抖页面就卡在"正在准备 Excel 导出…"不动了。
打包成本地文件后，整个在线版只依赖两样东西：本站点 + Pyodide 运行时。

这样做的好处还有：**在线版和 exe 版共用同一份核心代码**，
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
import importlib
import io
import shutil
import socketserver
import sys
import zipfile
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

# 导出 Excel 需要的包，会被打进 vendor.zip
VENDOR_PACKAGES = ["openpyxl", "et_xmlfile"]

# 界面依赖的静态文件（目前只有这一份 HTML，CSS/JS 都内联在里面）
WEB_FILES = ["index.html"]


def make_vendor_zip(out: Path) -> tuple[int, int]:
    """把 openpyxl / et_xmlfile 的源码打包成 vendor.zip。返回 (文件数, 字节数)。"""
    buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in VENDOR_PACKAGES:
            try:
                module = importlib.import_module(name)
            except ImportError:
                print(f"  ! 本机没装 {name}，在线版将无法导出 xlsx（会退化成 CSV）")
                continue
            root = Path(module.__file__).resolve().parent
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                arcname = str(path.relative_to(root.parent)).replace("\\", "/")
                archive.write(path, arcname)
                count += 1
    data = buffer.getvalue()
    if count:
        (out / "vendor.zip").write_bytes(data)
    return count, len(data)


def build(out: Path, vendor: bool = True) -> dict[str, int]:
    if out.exists():
        shutil.rmtree(out)
    (out / "core").mkdir(parents=True)

    copied = {"html": 0, "py": 0, "vendor": 0, "bytes": 0}
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

    if vendor:
        files, size = make_vendor_zip(out)
        copied["vendor"] = files
        copied["bytes"] += size

    (out / ".nojekyll").write_text("", encoding="utf-8")
    return copied


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="组装在线版静态站")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--no-vendor", action="store_true",
                        help="不打包 openpyxl（在线版导出会退化成 CSV）")
    parser.add_argument("--serve", action="store_true", help="生成后本地起服务预览")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    out = Path(args.out)
    stats = build(out, vendor=not args.no_vendor)
    print(f"已生成：{out}")
    print(f"  界面 {stats['html']} 个、Python 模块 {stats['py']} 个"
          f"、Excel 组件 {stats['vendor']} 个文件，共 {stats['bytes'] / 1024:.1f} KB")

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
