"""把程序打包成一个 Windows 单文件 exe。

用法::

    python build.py            # 打包（生成 dist/课堂心得评阅工具.exe）
    python build.py --check    # 只做打包前检查，不真的打包

打包前会做两件事：

1. 跑一遍全部单元测试（不通过就不打包）；
2. 把 ``web/index.html`` 内联成 ``core/_frontend.py``——
   这样 exe **不需要任何外部资源文件**，"界面文件丢失"这类打包事故从根上避免。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_INDEX = ROOT / "web" / "index.html"
FRONTEND_MODULE = ROOT / "core" / "_frontend.py"
APP_NAME = "课堂心得评阅工具"
ENTRY = "app.py"

HIDDEN_IMPORTS = [
    "openpyxl",
    "openpyxl.styles",
    "openpyxl.utils",
    "openpyxl.worksheet.worksheet",
    "et_xmlfile",
    "core._frontend",
]

EXCLUDES = [
    # 这些库如果碰巧装在环境里，PyInstaller 会把它们一起打进来，白白撑大体积
    "numpy", "pandas", "matplotlib", "scipy", "PIL", "PyQt5", "PySide6",
    "tkinter", "test", "unittest", "pydoc", "doctest", "pytest", "setuptools",
    "pip", "wheel", "IPython", "notebook", "bs4", "requests", "lxml",
]


def generate_frontend_module() -> int:
    """把 index.html 内联进 Python 模块（用 repr，任何引号都不会出问题）。"""
    html = WEB_INDEX.read_text(encoding="utf-8")
    header = (
        '"""打包时自动生成，请勿手改。\n\n'
        "内容来自 web/index.html —— exe 里没有外部资源文件，界面就靠这个模块。\n"
        "重新生成：python build.py\n"
        '"""\n\n'
    )
    FRONTEND_MODULE.write_text(
        header + "INDEX_HTML = " + repr(html) + "\n", encoding="utf-8"
    )
    return len(html)


def run_tests() -> bool:
    print("== 1/3 跑单元测试 ==")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=ROOT,
    )
    return result.returncode == 0


def clean() -> None:
    for name in ("build", "dist", f"{APP_NAME}.spec"):
        target = ROOT / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


def build(onefile: bool = True, console: bool = True) -> int:
    print("== 3/3 调用 PyInstaller ==")
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
        "--onefile" if onefile else "--onedir",
        "--console" if console else "--windowed",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT),
        # 不打包 web/ 目录：界面已经内联进 core/_frontend.py 了
    ]
    for item in HIDDEN_IMPORTS:
        args += ["--hidden-import", item]
    for item in EXCLUDES:
        args += ["--exclude-module", item]
    args.append(ENTRY)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(args, cwd=ROOT, env=env).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="打包成单文件 exe")
    parser.add_argument("--check", action="store_true", help="只做检查和生成，不打包")
    parser.add_argument("--skip-tests", action="store_true", help="跳过单元测试")
    parser.add_argument("--onedir", action="store_true", help="打成文件夹而不是单文件（启动更快）")
    parser.add_argument("--windowed", action="store_true", help="不显示控制台窗口（不推荐：老师看不到退出提示）")
    args = parser.parse_args()

    if not args.skip_tests and not run_tests():
        print("\n单元测试没通过，已停止打包。")
        return 1

    print("\n== 2/3 内联界面 ==")
    size = generate_frontend_module()
    print(f"   web/index.html → core/_frontend.py（{size} 字节）")

    if args.check:
        print("\n检查完成（--check 模式，未打包）。")
        return 0

    clean()
    code = build(onefile=not args.onedir, console=not args.windowed)
    if code != 0:
        print("\n打包失败。")
        return code

    exe = ROOT / "dist" / f"{APP_NAME}.exe"
    if exe.exists():
        print(f"\n打包完成：{exe}")
        print(f"体积：{exe.stat().st_size / 1024 / 1024:.1f} MB")
        print("\n下一步可以验证一下打包结果：")
        print(f'  "{exe}" --selftest')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
