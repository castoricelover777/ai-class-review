"""把打包好的 exe 上传到 GitHub Release（发新版时用）。

鉴权顺序：
1. 环境变量 ``GITHUB_TOKEN``（推荐，CI 里用这个）
2. 本机 git 凭据管理器里已保存的 github.com 凭据（日常开发用）

用法::

    python tools/upload_release.py                       # 发布 v1.0.0 附件
    python tools/upload_release.py --tag v1.1.0          # 指定标签
    python tools/upload_release.py --dry-run             # 只看会传什么

注意事项
--------
GitHub 会把**非 ASCII 的附件名**替换成 ``default.exe``（实测上传和改名都不行），
所以附件名固定用 ASCII：``ClassReviewTool-<tag>.exe``。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "castoricelover777/ai-class-review"
DEFAULT_EXE = ROOT / "dist" / "课堂心得评阅工具.exe"
DEFAULT_DOC = ROOT / "dist" / "使用说明.txt"

# 附件名必须是 ASCII（GitHub 会把中文名换成 default.exe）
ASSET_TEMPLATE = "ClassReviewTool-{tag}.exe"
DOC_ASSET = "README-for-teacher.txt"


def get_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    try:
        result = subprocess.run(["git", "credential", "fill"],
                                input="protocol=https\nhost=github.com\n\n",
                                capture_output=True, text=True, timeout=25, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"拿不到凭据，请设置环境变量 GITHUB_TOKEN（{exc}）")
    for line in (result.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("拿不到凭据，请设置环境变量 GITHUB_TOKEN")


class GitHub:
    API = "https://api.github.com"

    def __init__(self, repo: str):
        self.repo = repo
        self.token = get_token()

    def call(self, path: str, method: str = "GET", payload=None, raw: bytes | None = None,
             content_type: str = "application/json", url: str | None = None, retries: int = 4):
        data = raw if raw is not None else (
            json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None)
        last: Exception | None = None
        for attempt in range(retries):
            req = urllib.request.Request(url or (self.API + path), data=data, method=method, headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "class-review-release",
                "Content-Type": content_type,
            })
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    body = resp.read()
                    return resp.status, (json.loads(body) if body else {})
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                try:
                    return exc.code, json.loads(body)
                except json.JSONDecodeError:
                    return exc.code, {"message": body[:300]}
            except Exception as exc:                 # 网络抖动就重试
                last = exc
                time.sleep(2 * (attempt + 1))
        raise SystemExit(f"请求连续失败：{type(last).__name__}: {last}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="把 exe 传到 GitHub Release")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--tag", default="v1.0.0")
    parser.add_argument("--exe", default=str(DEFAULT_EXE))
    parser.add_argument("--doc", default=str(DEFAULT_DOC), help="随 exe 一起发的说明文件")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    exe = Path(args.exe)
    if not exe.is_file():
        print(f"找不到 exe：{exe}\n请先运行 python build.py")
        return 1

    asset_exe = ASSET_TEMPLATE.format(tag=args.tag)
    gh = GitHub(args.repo)

    status, release = gh.call(f"/repos/{args.repo}/releases/tags/{args.tag}")
    if status != 200:
        print(f"找不到 release {args.tag}（HTTP {status}）。先在网页上创建，或用 API 建一个。")
        return 1
    release_id = release["id"]
    print(f"Release: {release['html_url']}")

    jobs = [(exe, asset_exe)]
    doc = Path(args.doc)
    if doc.is_file():
        jobs.append((doc, DOC_ASSET))
    else:
        print(f"（没有 {doc}，只传 exe）")

    existing = {a["name"]: a for a in release.get("assets", [])}
    print("现有附件:", list(existing) or "(无)")

    for path, name in jobs:
        size_mb = path.stat().st_size / 1024 / 1024
        if args.dry_run:
            print(f"[dry-run] 会传 {name}（{size_mb:.2f} MB）")
            continue
        if name in existing:
            status, _ = gh.call(f"/repos/{args.repo}/releases/assets/{existing[name]['id']}", "DELETE")
            print(f"  删掉旧附件 {name} → HTTP {status}")
        blob = path.read_bytes()
        upload_url = release["upload_url"].split("{")[0]
        print(f"  上传 {name}（{size_mb:.2f} MB）…", flush=True)
        for attempt in range(1, 7):
            try:
                status, data = gh.call(None, "POST", raw=blob,
                                       content_type="application/octet-stream",
                                       url=f"{upload_url}?name={quote(name)}", retries=1)
                if status in (200, 201):
                    print(f"    成功：{data['name']}  {data['browser_download_url']}")
                    break
                print(f"    第 {attempt} 次：HTTP {status} {str(data.get('message'))[:100]}")
            except SystemExit as exc:
                print(f"    第 {attempt} 次：{exc}")
            time.sleep(3 * attempt)

    status, release = gh.call(f"/repos/{args.repo}/releases/{release_id}")
    print("\n最终附件：")
    for asset in release.get("assets", []):
        print(f"  {asset['name']:<32} {asset['size'] / 1024 / 1024:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
