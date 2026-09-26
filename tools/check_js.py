"""检查界面里的内联 JS 语法（这个项目的界面没有构建步骤，所以需要这个闸门）。

界面是单文件 HTML、CSS/JS 全部内联，浏览器只在运行时才会报语法错。
改完界面先跑一下这个，比"打开浏览器发现白屏"快得多。

用法::

    python tools/check_js.py                 # 检查 web/index.html
    python tools/check_js.py 某个文件.html
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "web" / "index.html"
    html = target.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    code = "\n".join(scripts)
    print(f"{target.name}：{len(scripts)} 段内联脚本，共 {len(code)} 字符")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_js = Path(tmp) / "inline.mjs"
        tmp_js.write_text(code, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(tmp_js)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print("语法检查：失败")
        print(result.stdout)
        print(result.stderr)
        return result.returncode
    print("语法检查：通过")

    # 顺手看一眼有没有绕过适配层直接调 HTTP 的地方（在线版会因此失效）
    outside = [
        line.strip() for line in code.splitlines()
        if 'api("/api/' in line and "async" not in line
    ]
    http_backend_only = [ln for ln in outside if "return" not in ln and "await api" not in ln]
    if http_backend_only:
        print("注意：下面几行不在 httpBackend 里却直接调了 HTTP 接口，在线版会失效：")
        for line in http_backend_only:
            print("   ", line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
