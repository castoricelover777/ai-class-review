"""应用配置：API Key / 模型 / 并发等。

老师第一次双击 exe 时没有 Key，界面会提示填一个；填完写进 config.ini，
之后就是纯无脑双击。配置文件优先放 exe 旁边，写不进去（比如装在 Program Files）
就退到 %APPDATA%。
"""

from __future__ import annotations

import configparser
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "课堂心得评阅工具"
CONFIG_FILENAME = "config.ini"

SECTION = "llm"

DEFAULTS: dict[str, str] = {
    "api_key": "",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "concurrency": "4",
    "timeout": "90",
    "class_name": "",
}


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """exe 所在目录；开发时是项目根目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(relative: str) -> Path:
    """读取随包资源。打包后资源被解到 sys._MEIPASS，开发时在项目根。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return app_dir() / relative


def _candidate_config_paths() -> list[Path]:
    paths = [app_dir() / CONFIG_FILENAME]
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / APP_NAME / CONFIG_FILENAME)
    return paths


def config_path(create_dir: bool = True) -> Path:
    """挑一个可写的 config.ini 路径。"""
    candidates = _candidate_config_paths()
    for path in candidates:
        if path.exists():
            return path
    # 都不存在：用第一个能写进去的
    for path in candidates:
        try:
            if create_dir:
                path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / f".write_test_{os.getpid()}"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return path
        except OSError:
            continue
    return candidates[0]


@dataclass
class AppConfig:
    api_key: str = ""
    base_url: str = DEFAULTS["base_url"]
    model: str = DEFAULTS["model"]
    concurrency: int = 4
    timeout: int = 90
    class_name: str = ""
    path: Path | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        target = Path(path) if path else config_path()
        cfg = cls(path=target)
        parser = configparser.ConfigParser()
        try:
            parser.read(target, encoding="utf-8")
        except (OSError, configparser.Error):
            return cfg

        if parser.has_section(SECTION):
            section = parser[SECTION]
            cfg.api_key = section.get("api_key", "").strip()
            cfg.base_url = section.get("base_url", DEFAULTS["base_url"]).strip() or DEFAULTS["base_url"]
            cfg.model = section.get("model", DEFAULTS["model"]).strip() or DEFAULTS["model"]
            cfg.concurrency = _as_int(section.get("concurrency"), 4, lo=1, hi=16)
            cfg.timeout = _as_int(section.get("timeout"), 90, lo=10, hi=600)
            cfg.class_name = section.get("class_name", "").strip()
        return cfg

    def save(self, path: Path | None = None) -> Path:
        target = Path(path) if path else (self.path or config_path())
        parser = configparser.ConfigParser()
        parser[SECTION] = {
            "api_key": self.api_key or "",
            "base_url": self.base_url or DEFAULTS["base_url"],
            "model": self.model or DEFAULTS["model"],
            "concurrency": str(self.concurrency),
            "timeout": str(self.timeout),
            "class_name": self.class_name or "",
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                parser.write(fh)
            self.path = target
        except OSError:
            pass
        return target

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key) and len(self.api_key) > 8

    @property
    def mode(self) -> str:
        """``live`` = 真调大模型；``mock`` = 演示模式（未配 Key）。"""
        return "live" if self.has_api_key else "mock"

    def masked_key(self) -> str:
        if not self.api_key:
            return ""
        if len(self.api_key) <= 12:
            return self.api_key[:2] + "*" * 6
        return f"{self.api_key[:6]}{'*' * 8}{self.api_key[-4:]}"

    def public_dict(self) -> dict:
        """给界面用的安全视图，**不含完整 Key**。"""
        return {
            "has_api_key": self.has_api_key,
            "api_key_masked": self.masked_key(),
            "base_url": self.base_url,
            "model": self.model,
            "concurrency": self.concurrency,
            "timeout": self.timeout,
            "class_name": self.class_name,
            "mode": self.mode,
            "config_path": str(self.path or ""),
            "frozen": is_frozen(),
            "app_dir": str(app_dir()),
        }

    def update(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if value is None or not hasattr(self, key):
                continue
            if key == "concurrency":
                self.concurrency = _as_int(value, self.concurrency, lo=1, hi=16)
            elif key == "timeout":
                self.timeout = _as_int(value, self.timeout, lo=10, hi=600)
            else:
                setattr(self, key, str(value).strip())

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("path", None)
        return data


def _as_int(value, default: int, lo: int, hi: int) -> int:
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))
