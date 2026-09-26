"""学生名单（群昵称 → 真名 / 学号）。

为什么单独一个模块：群里学生用的是"赢赢""logic"这种昵称，成绩表上必须是真名+学号。
名单既用于**改名**（昵称 → 真名），也用于**消歧**（`张三：内容` 到底是不是一个人名）。

支持四种输入形式，`Roster.coerce()` 会自动识别：

1. ``Roster``            —— 已经是名单对象
2. ``str``               —— 粘贴的多行文本，支持带表头：:

    姓名,学号,群昵称
    张三,2023123456,赢赢
    李四,2023123457,logic

   没表头时按「姓名 学号 群昵称...」的顺序猜（第一个含 6~14 位数字的当学号）。
   分隔符支持 Tab / 中英文逗号 / 分号 / 竖线 / 两个以上空格（Excel 直接复制粘贴即可）。
3. ``Mapping``           —— ``{"赢赢": "张三", "logic": "李四"}``（键=群昵称，值=真名）
4. ``Iterable[str]``     —— ``["张三", "李四"]``（只有真名，没有昵称映射）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# 姓名归一化放在 textutil 里，roster / parser 都依赖它，避免循环 import
from .textutil import name_key, normalize_name

__all__ = ["Roster", "RosterEntry"]

# 表头关键词 → 列含义
_NAME_HDR = {"姓名", "名字", "真名", "学生", "学生姓名", "名称"}
_SID_HDR = {"学号", "学籍号", "学籍", "student_id", "sid", "id"}
_ALIAS_HDR = {"群昵称", "昵称", "微信昵称", "微信名", "微信", "别名", "备注", "群名片", "微信备注"}
_IGNORE_HDR = {"序号", "行号", "编号", "no", "index", "#"}

_SPLIT_RE = re.compile(r"[\t,，;；|]+|\s{2,}")
_DIGITS_RE = re.compile(r"^\d{6,14}$")


@dataclass
class RosterEntry:
    """一名学生：真名 + 可选学号 + 可选的群昵称列表。"""

    name: str
    student_id: str | None = None
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "student_id": self.student_id, "aliases": list(self.aliases)}


def _classify_header(cell: str) -> str | None:
    c = cell.strip().lower().replace(" ", "")
    if c in _IGNORE_HDR:
        return "ignore"
    if c in _NAME_HDR:
        return "name"
    if c in _SID_HDR:
        return "sid"
    if c in _ALIAS_HDR:
        return "alias"
    return None


class Roster:
    """学生名单。空名单视为"没提供名单"。"""

    def __init__(self, entries: Iterable[RosterEntry] | None = None):
        self.entries: list[RosterEntry] = [e for e in (entries or []) if e and e.name]
        self._exact: dict[str, RosterEntry] = {}
        for entry in self.entries:
            for cand in [entry.name, *entry.aliases]:
                k = name_key(cand)
                if k:
                    self._exact.setdefault(k, entry)

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #

    @classmethod
    def from_names(cls, names: Iterable[str]) -> Roster:
        return cls(RosterEntry(name=normalize_name(n)) for n in names if n and str(n).strip())

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, str]) -> Roster:
        """``{"群昵称": "真名"}``；若值本身是 dict 则支持 ``{name, student_id, aliases}``。"""
        entries: list[RosterEntry] = []
        for nickname, target in mapping.items():
            if isinstance(target, Mapping):
                entries.append(
                    RosterEntry(
                        name=normalize_name(str(target.get("name") or target.get("姓名") or nickname)),
                        student_id=(str(target.get("student_id") or target.get("学号") or "") or None),
                        aliases=[normalize_name(str(a)) for a in (target.get("aliases") or []) if str(a).strip()],
                    )
                )
            else:
                entries.append(
                    RosterEntry(
                        name=normalize_name(str(target)),
                        aliases=[normalize_name(str(nickname))] if str(nickname).strip() else [],
                    )
                )
        return cls(entries)

    @classmethod
    def from_items(cls, items: Iterable[Any]) -> Roster:
        """列表输入：元素可以是字符串（真名）或 dict（``{"name":..., "student_id":..., "aliases":[...]}``）。"""
        entries: list[RosterEntry] = []
        for item in items:
            if isinstance(item, Mapping):
                name = item.get("name") or item.get("姓名") or item.get("真名")
                if not name:
                    continue
                raw_aliases = item.get("aliases") or item.get("群昵称") or []
                if isinstance(raw_aliases, str):
                    raw_aliases = _SPLIT_RE.split(raw_aliases)
                entries.append(
                    RosterEntry(
                        name=normalize_name(str(name)),
                        student_id=(str(item.get("student_id") or item.get("学号") or "") or None),
                        aliases=[normalize_name(str(a)) for a in raw_aliases if str(a).strip()],
                    )
                )
            elif isinstance(item, RosterEntry):
                entries.append(item)
            elif str(item).strip():
                entries.append(RosterEntry(name=normalize_name(str(item))))
        return cls(entries)

    @classmethod
    def from_text(cls, text: str) -> Roster:
        """粘贴的名单文本（Excel 直接复制即可）。"""
        entries: list[RosterEntry] = []
        header: list[str] | None = None

        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            cells = [c.strip() for c in _SPLIT_RE.split(line) if c.strip()]
            if len(cells) <= 1:
                cells = [c for c in line.split() if c]
            if not cells:
                continue

            if header is None:
                classified = [_classify_header(c) for c in cells]
                if any(c is not None for c in classified):
                    header = [c or "ignore" for c in classified]
                    continue

            if header is not None:
                name = sid = None
                aliases: list[str] = []
                for idx, value in enumerate(cells):
                    role = header[idx] if idx < len(header) else "ignore"
                    if role == "name" and name is None:
                        name = value
                    elif role == "sid" and sid is None and _DIGITS_RE.match(value):
                        sid = value
                    elif role == "alias":
                        aliases.append(value)
                if name:
                    entries.append(RosterEntry(normalize_name(name), sid, [normalize_name(a) for a in aliases]))
                continue

            # 没有表头：第一个单元格当姓名，其余里第一个纯数字当学号，剩下的当群昵称
            name, sid, aliases = cells[0], None, []
            for value in cells[1:]:
                if sid is None and _DIGITS_RE.match(value):
                    sid = value
                else:
                    aliases.append(normalize_name(value))
            entries.append(RosterEntry(normalize_name(name), sid, aliases))

        return cls(entries)

    @classmethod
    def coerce(cls, obj: Any) -> Roster | None:
        """把调用方随手传进来的东西变成 Roster；空则返回 None。"""
        if obj is None:
            return None
        if isinstance(obj, Roster):
            return obj if obj.entries else None
        if isinstance(obj, str):
            result = cls.from_text(obj)
        elif isinstance(obj, Mapping):
            result = cls.from_mapping(obj)
        elif isinstance(obj, Iterable):
            result = cls.from_items(obj)
        else:
            return None
        return result if result.entries else None

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def keys(self) -> set[str]:
        """全部匹配键（真名 + 群昵称），供解析器做"只认名单"的严格模式。"""
        return set(self._exact)

    def resolve(self, nickname: str | None) -> tuple[RosterEntry, str] | None:
        """把群昵称解析到名单条目。

        返回 ``(entry, how)``，``how`` 含义：

        - ``"alias"``：命中群昵称列 —— 最可信
        - ``"name"``：昵称本身就是真名 —— 可信
        - ``"guess"``：前缀/包含匹配 —— **需要老师人工确认**（解析器会打 ``alias_guess`` 标记）
        """
        k = name_key(nickname)
        if not k or not self.entries:
            return None

        entry = self._exact.get(k)
        if entry is not None:
            how = "alias" if k not in {name_key(entry.name)} else "name"
            return entry, how

        if len(k) >= 2:
            for entry in self.entries:
                for cand in [entry.name, *entry.aliases]:
                    ck = name_key(cand)
                    if len(ck) >= 2 and (k.startswith(ck) or ck.startswith(k)):
                        return entry, "guess"
        return None

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __iter__(self):
        return iter(self.entries)
