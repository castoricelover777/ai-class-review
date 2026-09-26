"""数据模型：解析层的输入/输出契约。

第一步只定义模型，不涉及任何业务逻辑（打分、导出在后续步骤实现）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatMessage:
    """从群聊文本里解析出来的**一条**发言。"""

    index: int                      # 消息序号（从 0 开始，按解析顺序）
    name: str                       # 清洗后的说话人昵称（可能为空 = 无名说话人）
    raw_name: str                   # 原始昵称，保留给 UI 显示/纠错
    content: str                    # 正文（多行已合并、已去首尾空白）
    timestamp: str | None = None    # 原文里的时间戳字符串，可能为 None
    line_no: int = 0                # 在原文里的起始行号（1-based，方便老师定位）
    fmt: str = ""                   # 命中的格式名，见 parser 顶部注释
    flags: list[str] = field(default_factory=list)
    raw_block: str = ""             # 仅当 keep_raw=True 时填充

    @property
    def char_count(self) -> int:
        return len(self.content.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "raw_name": self.raw_name,
            "content": self.content,
            "timestamp": self.timestamp,
            "line_no": self.line_no,
            "fmt": self.fmt,
            "flags": list(self.flags),
        }


@dataclass
class Submission:
    """一个学生的**一份**待评阅心得（可能由同一人的多条发言合并而来）。"""

    name: str                                      # 学生姓名（若给了名单则用名单里的正式写法）
    content: str                                   # 合并后的心得正文
    student_id: str | None = None                  # 学号（从正文里抽取，可能为 None）
    timestamp: str | None = None                   # 该学生第一条发言的时间
    message_count: int = 1                         # 合并了几条发言
    flags: list[str] = field(default_factory=list) # needs_review / placeholder / resubmitted ...
    raw_name: str = ""                             # 群昵称原文，供人工核对

    @property
    def char_count(self) -> int:
        return len(self.content.strip())

    @property
    def needs_review(self) -> bool:
        return bool(self.flags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "raw_name": self.raw_name,
            "student_id": self.student_id,
            "content": self.content,
            "timestamp": self.timestamp,
            "message_count": self.message_count,
            "char_count": self.char_count,
            "flags": list(self.flags),
        }


@dataclass
class ParseResult:
    """一次解析的完整结果：给 UI 用，既有明细也有统计与告警。"""

    messages: list[ChatMessage] = field(default_factory=list)
    submissions: list[Submission] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.submissions]
