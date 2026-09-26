"""Excel 导出：成绩表 + 评分规则存档 + 雷同比对。

一份要交给教务/存档的成绩表，光有分数不够，所以导出三张表：

1. **成绩表**：一行一个学生，各维度分 + 总分 + 风险标记 + 评语，带冻结表头与筛选。
2. **评分规则**：本次用的是哪套规则（维度、分值、档位描述、课程要求），
   存档后半年后回看成绩也能说清楚"这分是怎么打的"。
3. **雷同比对**：本批次两两相似度，供老师复核抄袭嫌疑。
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .grader import GradeResult
from .rules import Rubric

__all__ = ["build_workbook", "to_bytes", "save", "suggested_filename"]

# 与界面同一套配色：纸白 / 墨黑 / 朱批红
INK = "1C1917"
PAPER = "F7F5F1"
SEAL = "A63A2E"
AMBER = "B45309"
MOSS = "3F6B4A"
LINE = "D9D3CA"

_HEADER_FILL = PatternFill("solid", fgColor=INK)
_HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
_TITLE_FONT = Font(color=INK, bold=True, size=14)
_SUB_FONT = Font(color="6B6259", size=10)
_BAND_FILL = PatternFill("solid", fgColor=PAPER)

_RISK_FONT = {
    "high": Font(color=SEAL, bold=True),
    "medium": Font(color=AMBER, bold=True),
    "low": Font(color=AMBER),
    "none": Font(color=MOSS),
}
_RISK_TEXT = {"high": "高", "medium": "中", "low": "低", "none": "—"}

_THIN = Side(style="thin", color=LINE)
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _sheet_title(ws: Worksheet, title: str, subtitle: str, span: int) -> int:
    """写一个跨列的大标题，返回下一可用行号。"""
    ws.cell(row=1, column=1, value=title).font = _TITLE_FONT
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(1, span))
    ws.cell(row=1, column=1).alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 26

    ws.cell(row=2, column=1, value=subtitle).font = _SUB_FONT
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=max(1, span))
    ws.row_dimensions[2].height = 18
    return 4


def _write_header(ws: Worksheet, row: int, headers: Sequence[str]) -> None:
    for col, text in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=text)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _BORDER
    ws.row_dimensions[row].height = 28


def _autosize(ws: Worksheet, headers: Sequence[str], min_width: int = 8, max_width: int = 46) -> None:
    for col, name in enumerate(headers, start=1):
        longest = len(str(name)) * 2
        for row in range(1, ws.max_row + 1):
            value = ws.cell(row=row, column=col).value
            if value is None:
                continue
            text = str(value)
            # 中文按 2 个字符宽估算
            width = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
            longest = max(longest, width)
        ws.column_dimensions[get_column_letter(col)].width = max(min_width, min(max_width, longest + 2))


def _styled(ws: Worksheet, row: int, col: int, value: Any, *, wrap: bool = False, align: str = "left"):
    cell = ws.cell(row=row, column=col, value=value)
    cell.border = _BORDER
    cell.alignment = Alignment(horizontal=align, vertical="top", wrap_text=wrap)
    return cell


# --------------------------------------------------------------------------- #
# 表一：成绩表
# --------------------------------------------------------------------------- #

def _grade_sheet(wb: Workbook, results: Sequence[GradeResult], rubric: Rubric, meta: dict[str, Any]) -> None:
    ws = wb.create_sheet("成绩表")
    criteria = list(rubric.criteria)
    headers = ["序号", "学号", "姓名", "群昵称"]
    headers += [f"{c.name}\n(满分{c.max_score:g})" for c in criteria]
    headers += ["总分", "满分", "得分率", "风险", "风险说明", "总评", "老师补充", "字数", "已复核"]

    # 列位置集中在这里算一次，避免到处写算术表达式
    col_total = 5 + len(criteria)
    col_max = col_total + 1
    col_ratio = col_total + 2
    col_risk = col_total + 3
    col_risk_why = col_total + 4
    col_comment = col_total + 5
    col_manual = col_total + 6
    col_chars = col_total + 7
    col_reviewed = col_total + 8
    center_cols = {1, 2, col_total, col_max, col_ratio, col_risk, col_chars, col_reviewed}
    wrap_cols = {col_risk_why, col_comment, col_manual}

    row = _sheet_title(
        ws,
        f"{meta.get('class_name') or ''} 课堂心得成绩表".strip(),
        f"评分规则：{rubric.name}　|　导出时间：{meta.get('exported_at', '')}　|　共 {len(results)} 人"
        + ("　|　【演示分数：未配置 API Key，不能作为成绩依据】" if meta.get("mock") else ""),
        len(headers),
    )
    _write_header(ws, row, headers)
    header_row = row
    row += 1

    for idx, item in enumerate(results, start=1):
        by_key = {c.key: c for c in item.criteria}
        values: list[Any] = [idx, item.student_id or "", item.name, item.raw_name or ""]
        for c in criteria:
            got = by_key.get(c.key)
            score = got.score if got else 0
            band = got.band if got and got.band else ""
            values.append(f"{score:g}" + (f"（{band}）" if band else ""))
        values += [
            item.total,
            item.max_total,
            f"{item.ratio * 100:.0f}%",
            _RISK_TEXT.get(item.risk_level, item.risk_level),
            "；".join(item.risk_reasons) or (item.error or ""),
            item.overall_comment,
            item.manual_comment or "",
            len(item.content.strip()),
            "是" if item.reviewed else "",
        ]

        for col, value in enumerate(values, start=1):
            cell = _styled(
                ws, row, col, value,
                wrap=col in wrap_cols,
                align="center" if col in center_cols else "left",
            )
            if idx % 2 == 0:
                cell.fill = _BAND_FILL
            if col == col_total:
                cell.font = Font(bold=True, size=12)
            elif col == col_risk:
                cell.font = _RISK_FONT.get(item.risk_level, Font())
        row += 1

    # 平均分行
    if results:
        _styled(ws, row, 1, "平均", align="center").font = Font(bold=True)
        for col in range(5, 5 + len(criteria)):
            key = criteria[col - 5].key
            vals = [next((c.score for c in r.criteria if c.key == key), 0.0) for r in results]
            avg = round(sum(vals) / len(vals), 2)
            _styled(ws, row, col, avg, align="center").font = Font(bold=True)
        _styled(ws, row, col_total, round(sum(r.total for r in results) / len(results), 2),
                align="center").font = Font(bold=True)
        for col in (col_max, col_ratio, col_risk, col_risk_why, col_comment, col_manual,
                    col_chars, col_reviewed):
            _styled(ws, row, col, None)

    ws.freeze_panes = ws.cell(row=header_row + 1, column=5)
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(headers))}{header_row + len(results)}"
    _autosize(ws, headers)


# --------------------------------------------------------------------------- #
# 表二：评分规则存档
# --------------------------------------------------------------------------- #

def _rubric_sheet(wb: Workbook, rubric: Rubric, meta: dict[str, Any]) -> None:
    ws = wb.create_sheet("评分规则")
    headers = ["维度", "满分", "档位", "档位分值", "档位说明"]
    row = _sheet_title(ws, "本次使用的评分规则", f"{rubric.name}　|　满分 {rubric.total:g} 分", len(headers))
    _write_header(ws, row, headers)
    row += 1

    for c in rubric.criteria:
        start = row
        if c.levels:
            for lv in c.levels:
                _styled(ws, row, 1, c.name, wrap=True)
                _styled(ws, row, 2, c.max_score, align="center")
                _styled(ws, row, 3, lv.label, align="center")
                _styled(ws, row, 4, round(c.max_score * lv.ratio, 2), align="center")
                _styled(ws, row, 5, lv.description, wrap=True)
                row += 1
            if row - start > 1:
                ws.merge_cells(start_row=start, start_column=1, end_row=row - 1, end_column=1)
                ws.merge_cells(start_row=start, start_column=2, end_row=row - 1, end_column=2)
        else:
            _styled(ws, row, 1, c.name, wrap=True)
            _styled(ws, row, 2, c.max_score, align="center")
            for col in (3, 4, 5):
                _styled(ws, row, col, None)
            row += 1
        if c.description:
            _styled(ws, row, 1, f"说明：{c.description}", wrap=True).font = _SUB_FONT
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
            row += 1
        row += 1   # 空一行分隔维度

    row += 1
    extra: list[tuple[str, str]] = [("课程要求 / 课堂背景", rubric.context or "（未填写）")]
    policy = {"forbidden": "禁止使用 AI 代写", "allowed": "允许使用 AI 辅助", "unknown": "未说明"}
    extra.append(("AI 使用规定", policy.get(rubric.ai_policy, "未说明")))
    extra.append(("字数下限", f"{rubric.min_chars} 字"))
    extra.append(("雷同判定阈值", f"{rubric.similarity_threshold:.0%}"))
    if rubric.extra_instructions:
        extra.append(("老师额外要求", rubric.extra_instructions))
    extra.append(("导出时间", str(meta.get("exported_at", ""))))

    for label, value in extra:
        cell = _styled(ws, row, 1, label)
        cell.font = Font(bold=True)
        vcell = _styled(ws, row, 2, value, wrap=True)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        row += 1

    _autosize(ws, headers, max_width=60)


# --------------------------------------------------------------------------- #
# 表三：雷同比对
# --------------------------------------------------------------------------- #

def _similarity_sheet(wb: Workbook, results: Sequence[GradeResult], meta: dict[str, Any]) -> None:
    ws = wb.create_sheet("雷同比对")
    headers = ["学生 A", "学生 B", "相似度", "重合片段"]
    row = _sheet_title(
        ws,
        "同批次心得雷同比对",
        "由工具做字符级比对（4-gram + 最长公共片段）得出，仅作提示；引用课件造成的正常重合请忽略。",
        len(headers),
    )
    _write_header(ws, row, headers)
    row += 1

    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[float, str, str, str]] = []
    for item in results:
        for hit in item.similarity:
            key = tuple(sorted([item.name, hit.name]))
            if key in seen:
                continue
            seen.add(key)
            pairs.append((hit.ratio, item.name, hit.name, hit.snippet))
    pairs.sort(reverse=True)

    if not pairs:
        _styled(ws, row, 1, "本批次没有发现相似度超过阈值的组合。").font = Font(color=MOSS)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
    for ratio, a, b, snippet in pairs:
        _styled(ws, row, 1, a)
        _styled(ws, row, 2, b)
        cell = _styled(ws, row, 3, f"{ratio * 100:.0f}%", align="center")
        cell.font = _RISK_FONT["high"] if ratio >= 0.8 else _RISK_FONT["medium"]
        _styled(ws, row, 4, snippet, wrap=True)
        row += 1

    _autosize(ws, headers, max_width=60)


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #

def build_workbook(
    results: Sequence[GradeResult],
    rubric: Rubric,
    *,
    class_name: str = "",
    mock: bool = False,
    exported_at: datetime | None = None,
) -> Workbook:
    when = exported_at or datetime.now()
    meta = {
        "class_name": class_name or "",
        "exported_at": when.strftime("%Y-%m-%d %H:%M"),
        "mock": mock,
    }
    wb = Workbook()
    wb.remove(wb.active)          # 去掉默认空表
    _grade_sheet(wb, results, rubric, meta)
    _rubric_sheet(wb, rubric, meta)
    _similarity_sheet(wb, results, meta)
    wb.active = 0
    return wb


def to_bytes(
    results: Sequence[GradeResult],
    rubric: Rubric,
    *,
    class_name: str = "",
    mock: bool = False,
    exported_at: datetime | None = None,
) -> bytes:
    wb = build_workbook(results, rubric, class_name=class_name, mock=mock, exported_at=exported_at)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def save(
    results: Iterable[GradeResult],
    rubric: Rubric,
    path: str | Path,
    *,
    class_name: str = "",
    mock: bool = False,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    wb = build_workbook(list(results), rubric, class_name=class_name, mock=mock)
    wb.save(target)
    return target


def suggested_filename(rubric: Rubric, class_name: str = "", *, when: datetime | None = None) -> str:
    """生成表单友好的文件名：`课堂心得成绩_计科2201_20260520_1430.xlsx`。"""
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M")
    parts = ["课堂心得成绩"]
    if class_name.strip():
        parts.append(_safe(class_name.strip()))
    parts.append(f"{stamp}（{_safe(rubric.name)}）")
    return "_".join(parts) + ".xlsx"


_BAD_CHARS = '<>:"/\\|?*'


def _safe(text: str, limit: int = 24) -> str:
    cleaned = "".join(ch for ch in text if ch not in _BAD_CHARS and ch >= " ")
    return cleaned.strip()[:limit] or "未命名"
