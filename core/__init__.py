"""AI 辅助课堂心得评阅与打分 —— 核心包。

第一步（当前）：``core.parser`` + ``core.models`` + ``core.roster``
后续步骤：``core.rules``（评分配置）、``core.grader``（大模型打分）、
         ``core.exporter``（Excel 导出）、``app.py``（Streamlit 界面）

这里用惰性导出（PEP 562），这样 ``python -m core.parser`` 直接跑 CLI 时
不会触发 runpy 的 "found in sys.modules" 警告。
"""

from typing import Any

__all__ = [
    "ChatMessage",
    "ParseResult",
    "Submission",
    "Roster",
    "RosterEntry",
    "parse_wechat_text",
    "extract_submissions",
    "normalize_name",
    "name_key",
    "looks_like_name",
]

_LAZY = {
    "ChatMessage": ("core.models", "ChatMessage"),
    "ParseResult": ("core.models", "ParseResult"),
    "Submission": ("core.models", "Submission"),
    "Roster": ("core.roster", "Roster"),
    "RosterEntry": ("core.roster", "RosterEntry"),
    "parse_wechat_text": ("core.parser", "parse_wechat_text"),
    "extract_submissions": ("core.parser", "extract_submissions"),
    "normalize_name": ("core.textutil", "normalize_name"),
    "name_key": ("core.textutil", "name_key"),
    "looks_like_name": ("core.textutil", "looks_like_name"),
}


def __getattr__(name: str) -> Any:  # pragma: no cover - 便利性导出
    if name in _LAZY:
        from importlib import import_module

        module_name, attr = _LAZY[name]
        return getattr(import_module(module_name), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:  # pragma: no cover
    return sorted(__all__)
