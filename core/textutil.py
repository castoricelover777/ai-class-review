"""文本与姓名归一化的公共工具。

单独一层是为了让 ``core.parser``（解析）和 ``core.roster``（名单）都能用，
而不至于互相 import。
"""

from __future__ import annotations

import re

__all__ = ["normalize_name", "name_key", "looks_like_name", "strip_invisible", "ZW_RE"]

# 不可见字符：零宽空格/连接符/BOM/词连接符
ZW_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]")

# 群昵称后缀：张三(wxid_abc123) / 张三<wxid_xxx> / 张三(abcdefgh)
_RE_WXID_SUFFIX = re.compile(
    r"\s*[（(\[<]\s*(?:wxid_[A-Za-z0-9_-]+|[A-Za-z][A-Za-z0-9_-]{7,})\s*[)）\]>]\s*$"
)
# 群昵称里的组织后缀：张三-计科2201 → 张三
_RE_ALIAS_SUFFIX = re.compile(r"^([\u4e00-\u9fa5]{2,4})\s*[-—_/|·].+$")
# Emoji / 符号
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u2190-\u21ff]"
)
# 明确不能出现在昵称里的标点（括号除外：昵称常带括号备注）
_NAME_BAD_PUNCT_RE = re.compile(r"[。！？!?；;，,、~～…“”‘’\"'《》<>]")

# 昵称停用词：出现在行首，或（长度 ≥2 字符且昵称总长 ≥4 时）出现在中间，
# 就判定"这是正文句子而不是昵称"。
_NAME_STOPWORDS = (
    "我", "你", "他", "她", "它", "咱", "大家", "各位", "同学", "老师",
    "所以", "因为", "但是", "而且", "如果", "虽然", "不过", "另外", "还有",
    "就是", "这个", "那个", "这些", "那些", "感觉", "觉得", "认为", "发现",
    "心得", "感想", "体会", "收获", "内容", "作业", "问题", "观点", "理解",
    "认识", "看法", "想法", "总结", "补充", "首先", "其次", "最后", "总之",
    "今天", "上课", "课程", "这次", "通过", "其实", "可能", "应该", "需要",
    "希望", "谢谢", "好的", "收到", "老师好", "是", "有", "在", "从", "对",
)


def strip_invisible(text: str | None) -> str:
    """去掉零宽字符等不可见字符。"""
    return ZW_RE.sub("", text or "")


def normalize_name(raw: str | None, *, drop_alias: bool = False) -> str:
    """清洗群昵称：去 wxid 后缀、去 emoji、去首尾标点。

    注意：**不剥离句末句号等句读**——那是区分"昵称"和"正文句子"的重要信号。

    >>> normalize_name('张三(wxid_abc123def)')
    '张三'
    >>> normalize_name('  李四  ')
    '李四'
    """
    if not raw:
        return ""
    s = ZW_RE.sub("", str(raw))
    s = s.replace("\u3000", " ").strip()
    s = _RE_WXID_SUFFIX.sub("", s)
    s = _EMOJI_RE.sub("", s)
    s = s.strip(" \t:：,，.-—_@")
    if drop_alias:
        m = _RE_ALIAS_SUFFIX.match(s)
        if m:
            s = m.group(1)
    return s.strip()


def name_key(name: str | None) -> str:
    """归一化去重键：用于判断"这两个昵称是不是同一个人"。"""
    s = normalize_name(name, drop_alias=True)
    s = re.sub(r"[\s\-_.·—/|]+", "", s)
    s = re.sub(r"[（(].*?[)）]", "", s)
    return s.lower()


def looks_like_name(candidate: str | None) -> bool:
    """判断一段文本像不像"人昵称"（用于消解 ``张三：内容`` 的歧义）。"""
    s = normalize_name(candidate)
    if not s or len(s) > 24:
        return False
    if re.search(r"\s", s):                       # 昵称内部通常没有空格
        return False
    if s[0] in "（([【":                          # "（1）第一点" 这类正文
        return False
    if _NAME_BAD_PUNCT_RE.search(s):              # 含句读/引号 → 正文
        return False
    if re.search(r"(?:19|20)\d{2}", s):           # 含 19xx/20xx → 像日期/学号
        return False
    if len(re.findall(r"[\u4e00-\u9fa5A-Za-z]", s)) < 2:   # 至少两个文字字符
        return False
    low = s.lower()
    for w in _NAME_STOPWORDS:
        if low.startswith(w):
            return False
        if len(w) >= 2 and w in low and len(s) >= 4:
            return False
    return True
