"""微信群聊记录解析器（第一步交付物）。

设计目标
--------
1. **零第三方依赖**：只用标准库 re / dataclasses / difflib，方便演示与单测。
2. **宽容格式**：同一段文本里可以混用多种粘贴格式，逐行判定而不是整体套一个正则。
3. **可解释**：每条消息都记录命中的格式(fmt)与告警(flags)，
   所以 UI 层可以做一个"姓名/正文可编辑的解析预览表"来兜底。
4. **不猜到底**：解析是启发式的，宁可标 needs_review，也不要静默猜错。

支持的格式（按匹配优先级从上到下）
----------------------------------
A. ``[2024-05-20 14:30] 张三：内容``       带方括号时间戳
B. ``2024-05-20 14:30 张三：内容``         时间戳在前
C. ``张三 2024-05-20 14:30:12``            微信 PC 端导出（昵称+时间，正文另起行）
D. ``张三：内容`` / ``张三: 内容``           手机端"复制多条消息"，最常见
E. ``张三``                                昵称单独成行，靠空行分隔消息块

时间戳变体均支持：``2024-05-20``、``2024/5/20``、``2024.5.20``、``2024年5月20日``，
以及可选的 ``HH:MM`` / ``HH:MM:SS``。

已知局限（有意保留，交给 UI 人工校正）
--------------------------------------
- 正文里形如"我觉得：……"的行不会被误判成昵称（有停用词表 + 结构规则），
  但启发式不可能 100% 准确；传入 ``known_names``（学生名单）可几乎消除误判。
- 全角空格按普通空格归一化，正文靠全角空格做的缩进不会保留。
- 纯图片/表情消息会被标记为 ``placeholder``，不会当成正文。

单独运行（快速看效果）::

    python -m core.parser tests/fixtures/paste_colon.txt
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from .models import ChatMessage, ParseResult, Submission
from .roster import Roster
from .textutil import name_key, normalize_name, looks_like_name  # noqa: F401  (对外重新导出)

__all__ = [
    "parse_wechat_text",
    "parse_messages",
    "extract_submissions",
    "normalize_name",
    "name_key",
    "looks_like_name",
    "match_roster",
]

# --------------------------------------------------------------------------- #
# 正则表
# --------------------------------------------------------------------------- #

# 不可见字符：零宽空格/连接符/BOM/词连接符
_ZW_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]")

# 时间戳：日期 + 可选时间
_TS = (
    r"\d{4}(?:[-/.]\d{1,2}[-/.]\d{1,2}|年\d{1,2}月\d{1,2}日?)"
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?"
)

# A. [2024-05-20 14:30] 张三：内容
_RE_BRACKET_TS_NAME = re.compile(
    rf"^[\[【]\s*(?P<ts>{_TS})\s*[\]】]\s*"
    rf"(?P<name>[^：:\[\]【】]{{1,24}}?)\s*(?:[：:]\s*(?P<content>.*))?$"
)
# B. 2024-05-20 14:30 张三：内容
_RE_TS_NAME = re.compile(
    rf"^(?P<ts>{_TS})\s+(?P<name>[^：:]{{1,24}}?)\s*(?:[：:]\s*(?P<content>.*))?$"
)
# C. 张三 2024-05-20 14:30:12
_RE_NAME_TS = re.compile(rf"^(?P<name>[^：:]{{1,24}}?)\s+(?P<ts>{_TS})\s*$")
# D. 张三：内容
_RE_NAME_COLON = re.compile(r"^(?P<name>[^：:]{1,24})[：:]\s*(?P<content>.*)$")
# F. 独立成行的时间戳（微信手机端"复制多条消息"：昵称行 → 时间行 → 正文行）
_RE_TS_ONLY = re.compile(rf"^(?P<ts>{_TS})\s*$")

# 头行里的昵称清洗、以及"这段文字像不像人名"的判定都在 core/textutil.py

# 系统消息关键词（只在短行上生效，避免误删提到"撤回"的正文）
_SYSTEM_RE = re.compile(
    "|".join(
        [
            r"撤回了一条消息",
            r"拍了拍",
            r"加入(?:了)?群聊",
            r"邀请.{0,12}加入",
            r"(?:移出|踢出)(?:了)?群聊",
            r"修改群名为",
            r"领取了.{0,8}的红包",
            r"退出了群聊",
            r"以上是打招呼的内容",
            r"开启了朋友验证",
            r"你已添加了",
            r"群公告",
            r"该内容已被发布者删除",
            r"^\s*\[?系统消息\]?\s*[:：]?",
            r"^\s*系统通知\s*[:：]?",
        ]
    )
)
_SYSTEM_MAX_LEN = 40  # 超过这个长度就不当系统消息，宁可留给老师人工判断

# 占位消息：[图片] [表情] [动画表情] [语音] ...
_PLACEHOLDER_WORDS = (
    "图片", "表情", "动画表情", "语音", "视频", "文件", "链接", "小程序", "音乐",
    "位置", "名片", "聊天记录", "转账", "红包", "视频号", "直播", "GIF",
    "未知消息", "卡片消息", "收藏", "接龙", "投票", "合并转发",
)
_PLACEHOLDER_LINE_RE = re.compile(r"^[\[【](?:%s)[\]】]\s*$" % "|".join(map(re.escape, _PLACEHOLDER_WORDS)))
_PLACEHOLDER_INLINE_RE = re.compile(r"[\[【](?:%s)[\]】]" % "|".join(map(re.escape, _PLACEHOLDER_WORDS)))

# 有意义的字符（中文/拉丁/数字）——用来判断"正文是不是空的"
_MEANINGFUL_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]")

# 正文里抽取学号：10~12 位、以 15~20 开头
_SID_RE = re.compile(r"(?<!\d)((?:1[5-9]|20)\d{8,10})(?!\d)")

# 正文里自报姓名："我是张三，……" / "我叫张三。" / "姓名：张三"
_SELF_NAME_RES = (
    # 张三 2023123456  /  张三，学号 2023123456
    re.compile(r"^([\u4e00-\u9fa5·]{2,4})\s*[，,、\s]\s*(?:学号)?\s*((?:1[5-9]|20)\d{8,10})"),
    # 2023123456 张三
    re.compile(r"^((?:1[5-9]|20)\d{8,10})\s*[，,、\s]\s*([\u4e00-\u9fa5·]{2,4})"),
    # 我是张三 / 我叫张三 / 姓名：张三 / 名字 张三
    re.compile(
        r"(?:^|[\s，,。；;])(?:我是|我叫|本人是|姓名[:：]?\s*|名字[:：]?\s*)"
        r"([\u4e00-\u9fa5·]{2,4})(?=[\s，,。；;！!？?、：:]|\d|$)"
    ),
)

# 昵称停用词、姓名判定的具体规则见 core/textutil.py


# --------------------------------------------------------------------------- #
# 文本与姓名归一化
# --------------------------------------------------------------------------- #

def _normalize_text(text: str | None) -> str:
    """统一换行、去不可见字符、全角空格归一化、去掉行尾空白。"""
    if text is None:
        return ""
    s = str(text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\u00a0", " ").replace("\u3000", " ")
    s = _ZW_RE.sub("", s)
    return "\n".join(line.rstrip() for line in s.split("\n"))


def match_roster(name: str | None, roster: object | None) -> str | None:
    """把群昵称映射成名单里的正式姓名；匹配不到返回 None。

    ``roster`` 可以是 ``Roster`` / 名单文本 / ``{群昵称: 真名}`` / ``["张三", ...]``。
    带群昵称映射时优先用映射，其次同名，最后做前缀包含匹配。
    """
    obj = Roster.coerce(roster)
    if not obj:
        return None
    hit = obj.resolve(name)
    return hit[0].name if hit else None


# --------------------------------------------------------------------------- #
# 内容分类
# --------------------------------------------------------------------------- #

def _content_kind(content: str) -> str:
    """'text' | 'empty'（纯占位符/空）| 'symbol'（纯表情符号）"""
    s = _PLACEHOLDER_INLINE_RE.sub("", content or "")
    s = _ZW_RE.sub("", s).strip()
    if not s:
        return "empty"
    if not _MEANINGFUL_RE.search(s):
        return "symbol"
    return "text"


def _is_system(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and len(t) <= _SYSTEM_MAX_LEN and bool(_SYSTEM_RE.search(t))


def _similar(a: str, b: str) -> float:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# --------------------------------------------------------------------------- #
# 逐行状态机
# --------------------------------------------------------------------------- #

@dataclass
class _Header:
    name: str          # 清洗后的昵称
    raw_name: str      # 原文里的昵称，原样保留给 UI 核对
    content: str
    timestamp: str | None
    fmt: str


def _name_ok(raw: str, roster: set[str], strict: bool) -> bool:
    clean = normalize_name(raw)
    if not looks_like_name(clean):
        return False
    if strict and roster:
        return name_key(clean) in roster
    return True


def _match_header(
    line: str,
    *,
    roster: set[str],
    strict: bool,
    prev_blank: bool,
    is_first: bool,
    allow_bare: bool,
) -> _Header | None:
    """按优先级尝试把一行识别为"发言头"。"""
    m = _RE_BRACKET_TS_NAME.match(line)
    if m and _name_ok(m.group("name"), roster, strict):
        return _Header(normalize_name(m.group("name")), m.group("name").strip(),
                       (m.group("content") or "").strip(), m.group("ts"), "bracket_ts_name")

    m = _RE_TS_NAME.match(line)
    if m and _name_ok(m.group("name"), roster, strict):
        return _Header(normalize_name(m.group("name")), m.group("name").strip(),
                       (m.group("content") or "").strip(), m.group("ts"), "ts_name_colon")

    m = _RE_NAME_TS.match(line)
    if m and _name_ok(m.group("name"), roster, strict):
        return _Header(normalize_name(m.group("name")), m.group("name").strip(),
                       "", m.group("ts"), "name_ts")

    m = _RE_NAME_COLON.match(line)
    if m and _name_ok(m.group("name"), roster, strict):
        return _Header(normalize_name(m.group("name")), m.group("name").strip(),
                       (m.group("content") or "").strip(), None, "name_colon")

    if allow_bare and (is_first or prev_blank) and len(normalize_name(line)) <= 12 \
            and _name_ok(line, roster, strict):
        return _Header(normalize_name(line), line.strip(), "", None, "bare_name")

    return None


def parse_messages(
    text: str | None,
    *,
    roster: object | None = None,
    known_names: object | None = None,
    strict_names: bool = False,
    drop_system: bool = True,
    keep_raw: bool = False,
    allow_bare_name: bool = True,
) -> tuple[list[ChatMessage], dict[str, int], list[str]]:
    """把群聊文本拆成逐条消息。返回 (messages, stats, warnings)。

    ``roster`` / ``known_names`` 都接受：名单文本、``{群昵称: 真名}``、
    ``["张三", ...]`` 或 ``Roster`` 对象（``known_names`` 是保留的旧参数名）。
    """
    norm = _normalize_text(text)
    lines = norm.split("\n") if norm.strip() else []

    roster_obj = Roster.coerce(roster if roster is not None else known_names)
    roster_keys = roster_obj.keys() if roster_obj else set()
    strict = bool(strict_names and roster_keys)

    messages: list[ChatMessage] = []
    warnings: list[str] = []
    stats = {
        "lines": len([l for l in lines if l.strip()]),
        "messages": 0,
        "system_dropped": 0,
        "placeholder_dropped": 0,
        "unnamed": 0,
    }

    cur: ChatMessage | None = None
    prev_blank = True          # 文首等价于"前面是空行"
    prev_header_empty = False  # 上一条是"头行但没有正文"→ 下一非空行必定是正文
    line_no = 0

    for i, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            prev_blank = True
            continue
        line_no = i

        header = None
        if not prev_header_empty:
            header = _match_header(
                line,
                roster=roster_keys,
                strict=strict,
                prev_blank=prev_blank,
                is_first=(cur is None),
                allow_bare=allow_bare_name,
            )

        if header is not None:
            # 头行本身是系统消息（例如 "系统消息：..." / 纯公告头）
            if drop_system and not header.content and _is_system(header.name):
                stats["system_dropped"] += 1
                prev_blank = False
                prev_header_empty = False
                continue
            if drop_system and header.content and _is_system(header.content):
                stats["system_dropped"] += 1
                prev_blank = False
                prev_header_empty = False
                continue

            cur = ChatMessage(
                index=len(messages),
                name=header.name,
                raw_name=header.raw_name,
                content=header.content,
                timestamp=header.timestamp,
                line_no=i,
                fmt=header.fmt,
                raw_block=line if keep_raw else "",
            )
            messages.append(cur)
            prev_header_empty = not header.content
            prev_blank = False
            continue

        # 微信手机端"复制多条消息"格式：昵称行 → 时间行 → 正文行。
        # 上一条头行没有正文时，紧跟的独立时间戳行是它的时间，不是它的正文。
        if prev_header_empty and cur is not None and cur.timestamp is None:
            m_ts = _RE_TS_ONLY.match(line)
            if m_ts:
                cur.timestamp = m_ts.group("ts")
                if keep_raw:
                    cur.raw_block = f"{cur.raw_block}\n{line}".strip() if cur.raw_block else line
                prev_blank = False
                continue  # prev_header_empty 保持 True：下一非空行才是正文

        # --- 非头行 ------------------------------------------------------ #
        if drop_system and _is_system(line):
            stats["system_dropped"] += 1
            prev_blank = False
            prev_header_empty = False
            continue

        is_placeholder_line = bool(_PLACEHOLDER_LINE_RE.match(line))

        if cur is None:
            # 没有任何头行 → 无名发言（可能是老师开场白，也可能是漏了昵称）
            cur = ChatMessage(
                index=len(messages),
                name="",
                raw_name="",
                content="",
                timestamp=None,
                line_no=i,
                fmt="unnamed",
                flags=["no_speaker"],
                raw_block="",
            )
            messages.append(cur)
            stats["unnamed"] += 1

        if is_placeholder_line and cur.content.strip():
            # 独立的 [图片]/[表情] 噪声行，且当前消息已有正文 → 丢弃
            stats["placeholder_dropped"] += 1
            prev_blank = False
            prev_header_empty = False
            continue

        cur.content = f"{cur.content}\n{line}".strip() if cur.content else line
        if keep_raw:
            cur.raw_block = f"{cur.raw_block}\n{line}".strip() if cur.raw_block else line
        prev_blank = False
        prev_header_empty = False

    stats["messages"] = len(messages)

    if stats["unnamed"]:
        warnings.append(
            f"有 {stats['unnamed']} 条发言没有识别出说话人，已标记 no_speaker（默认不计入成绩）。"
        )
    if stats["system_dropped"]:
        warnings.append(f"已忽略 {stats['system_dropped']} 行系统提示（撤回/入群/拍一拍等）。")
    if not messages and norm.strip():
        warnings.append("没能解析出任何消息，请检查粘贴内容是否包含昵称或时间戳。")

    return messages, stats, warnings


# --------------------------------------------------------------------------- #
# 消息 → 学生心得
# --------------------------------------------------------------------------- #

def extract_submissions(
    messages: Iterable[ChatMessage],
    *,
    roster: object | None = None,
    known_names: object | None = None,
    exclude_names: Sequence[str] | None = None,
    include_unnamed: bool = False,
    min_chars: int = 15,
    resubmit_ratio: float = 0.75,
) -> list[Submission]:
    """把逐条消息按"同一个学生"聚合，产出待评阅清单。"""
    roster_obj = Roster.coerce(roster if roster is not None else known_names)
    excluded = {name_key(n) for n in (exclude_names or ()) if n and str(n).strip()}

    groups: dict[str, list[ChatMessage]] = {}
    order: list[str] = []

    for msg in messages:
        key = name_key(msg.name)
        if not key:
            if not include_unnamed:
                continue
            key = f"__unnamed_{msg.index}"
        if key in excluded:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(msg)

    submissions: list[Submission] = []

    for key in order:
        group = groups[key]
        flags: list[str] = []
        kept: list[str] = []          # 同一学生的多段正文（去重后）
        placeholder_kinds: set[str] = set()

        for msg in group:
            kind = _content_kind(msg.content)
            if kind != "text":
                # 纯图片/表情/空气泡：不算正文。只有"整组都没有文字"时才算问题，
                # 否则学生后面补发了文字，不该再因为他发过表情而告警。
                placeholder_kinds.add("placeholder" if kind == "empty" else "symbol_only")
                continue
            text = _PLACEHOLDER_INLINE_RE.sub("", msg.content).strip()
            if not text:
                continue
            if kept and _similar(kept[-1], text) >= resubmit_ratio:
                kept[-1] = text      # 同一人改了重发：保留最后一次
                flags.append("resubmitted")
            else:
                kept.append(text)

        content = "\n".join(kept).strip()
        if not content:
            if "placeholder" in placeholder_kinds:
                flags.append("placeholder")
            if "symbol_only" in placeholder_kinds:
                flags.append("symbol_only")

        raw_name = normalize_name(group[0].raw_name) or group[0].name

        # 名单：昵称 → 真名 + 学号（名单里的学号优先于正文里写的）
        canonical = normalize_name(group[0].name)
        sid_from_roster = None
        hit = roster_obj.resolve(raw_name) if roster_obj else None
        if hit is not None:
            entry, how = hit
            canonical = entry.name
            sid_from_roster = entry.student_id
            if how == "guess":
                # 前缀/包含匹配出来的，需要老师人工确认
                flags.append("alias_guess")
        elif roster_obj and canonical:
            flags.append("not_in_roster")
        if not canonical:
            canonical = raw_name or "（未识别）"

        # 学号：名单优先；否则取正文里第一个
        student_id = sid_from_roster
        if not student_id:
            for msg in group:
                m = _SID_RE.search(msg.content or "")
                if m:
                    student_id = m.group(1)
                    break

        if not content:
            flags.append("no_content")
        elif len(content) < min_chars:
            flags.append("too_short")
        if len(group) > 1:
            flags.append("merged")
        if not name_key(group[0].name):
            flags.append("no_speaker")

        submissions.append(
            Submission(
                name=canonical,
                content=content,
                student_id=student_id,
                timestamp=group[0].timestamp,
                message_count=len(group),
                flags=sorted(set(flags)),
                raw_name=raw_name,
            )
        )

    return submissions


# --------------------------------------------------------------------------- #
# 顶层入口
# --------------------------------------------------------------------------- #

def parse_wechat_text(
    text: str | None,
    *,
    roster: object | None = None,
    known_names: object | None = None,
    exclude_names: Sequence[str] | None = None,
    strict_names: bool = False,
    include_unnamed: bool = False,
    min_chars: int = 15,
    keep_raw: bool = False,
) -> ParseResult:
    """一行入口：粘贴的群聊文本 → 消息明细 + 待评阅心得清单 + 统计/告警。"""
    messages, stats, warnings = parse_messages(
        text,
        roster=roster,
        known_names=known_names,
        strict_names=strict_names,
        keep_raw=keep_raw,
    )
    submissions = extract_submissions(
        messages,
        roster=roster,
        known_names=known_names,
        exclude_names=exclude_names,
        include_unnamed=include_unnamed,
        min_chars=min_chars,
    )

    stats["submissions"] = len(submissions)
    stats["needs_review"] = sum(1 for s in submissions if s.needs_review)
    if stats["needs_review"]:
        warnings.append(
            f"有 {stats['needs_review']} 位同学的结果需要人工确认（见 flags 列）。"
        )
    return ParseResult(messages=messages, submissions=submissions, stats=stats, warnings=warnings)


# --------------------------------------------------------------------------- #
# CLI：python -m core.parser 文件.txt
# --------------------------------------------------------------------------- #

def _main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    roster_path = next((a.split("=", 1)[1] for a in argv[1:] if a.startswith("--roster=")), None)

    if args and args[0] not in ("-", "--stdin"):
        with open(args[0], "r", encoding="utf-8") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()

    roster = None
    if roster_path:
        with open(roster_path, "r", encoding="utf-8") as fh:
            roster = fh.read()

    result = parse_wechat_text(raw, roster=roster)
    print(f"== 消息 {len(result.messages)} 条 / 待评阅 {len(result.submissions)} 人 ==")
    for s in result.submissions:
        sid = f" 学号={s.student_id}" if s.student_id else ""
        flg = f"  [{','.join(s.flags)}]" if s.flags else ""
        label = s.name if s.raw_name in ("", s.name) else f"{s.name}（群昵称 {s.raw_name}）"
        body = s.content.replace("\n", " ⏎ ")
        if len(body) > 60:
            body = body[:60] + "…"
        print(f"- {label}{sid} ({s.char_count}字){flg}\n    {body}")
    print("\nstats:", result.stats)
    for w in result.warnings:
        print("warn:", w)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv))
