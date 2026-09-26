"""浏览器版（Pyodide）用的接口层：JSON 进、JSON 出。

为什么要有这一层
----------------
在线版把**同一份 Python 核心**跑在浏览器里（Pyodide / WebAssembly），
不重写解析器和打分逻辑——否则那 600 行调好的正则和 187 个单元测试就得维护两份，
迟早跑偏。

浏览器里能跑 Python，但有两个限制：

1. **没有 urllib**（不能直接发 HTTP）。所以拆成两步：
   这里负责「拼 Prompt」和「解析模型回复」，中间那一次网络请求交给 JS 的 fetch。
2. **没有真正的多线程**。所以并发由 JS 那边用 Promise 控制，
   这里所有函数都是同步的、纯计算。

所有函数只接受/返回字符串（JSON），避免 Pyodide 代理对象的生命周期问题。
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

from .exporter import OPENPYXL_AVAILABLE, suggested_filename, to_bytes
from .grader import (
    GradeResult,
    SimilarityHit,
    fail_result,
    finish_from_reply,
    mock_result,
    new_result,
    parse_model_json,  # noqa: F401  (重新导出，方便前端排查)
    pairwise_similarity,
    build_messages,
)
from .parser import parse_wechat_text
from .rules import DEFAULT_RUBRIC, Rubric

__all__ = [
    "bootstrap",
    "parse_chat",
    "prepare_grade",
    "finish_grade",
    "fail_grade",
    "mock_grade",
    "export_b64",
    "export_available",
    "export_fallback_csv",
    "VERSION",
]

VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #

def bootstrap() -> str:
    """界面启动时拿一次：默认评分规则 + 版本号。"""
    return json.dumps({
        "ok": True,
        "version": VERSION,
        "rubric": DEFAULT_RUBRIC.to_dict(),
        "export_available": export_available(),
    }, ensure_ascii=False)


def export_available() -> bool:
    """浏览器里能不能生成真正的 .xlsx（openpyxl 是否装上了）。"""
    return bool(OPENPYXL_AVAILABLE)


def _rubric(data_json: str | None) -> Rubric:
    if not data_json:
        return Rubric.from_dict(DEFAULT_RUBRIC.to_dict())
    try:
        return Rubric.from_dict(json.loads(data_json))
    except (json.JSONDecodeError, TypeError, ValueError):
        return Rubric.from_dict(DEFAULT_RUBRIC.to_dict())


# --------------------------------------------------------------------------- #
# 解析群聊记录
# --------------------------------------------------------------------------- #

def parse_chat(payload_json: str) -> str:
    try:
        payload: dict[str, Any] = json.loads(payload_json or "{}")
    except json.JSONDecodeError as exc:
        return json.dumps({"ok": False, "error": f"参数不是合法 JSON：{exc}"}, ensure_ascii=False)

    chat_text = str(payload.get("chat_text") or "")
    if not chat_text.strip():
        return json.dumps({"ok": False, "error": "请先粘贴群聊记录"}, ensure_ascii=False)

    raw_exclude = str(payload.get("exclude_names") or "")
    exclude = [x.strip() for x in raw_exclude.replace("\n", ",").replace("、", ",").split(",") if x.strip()]

    try:
        min_chars = int(payload.get("min_chars") or 15)
    except (TypeError, ValueError):
        min_chars = 15

    try:
        result = parse_wechat_text(
            chat_text,
            roster=str(payload.get("roster_text") or "") or None,
            exclude_names=exclude,
            strict_names=bool(payload.get("strict_names")),
            min_chars=max(0, min(100000, min_chars)),
        )
    except Exception as exc:                       # pragma: no cover - 兜底
        return json.dumps({"ok": False, "error": f"解析失败：{exc}"}, ensure_ascii=False)

    return json.dumps({
        "ok": True,
        "submissions": [s.to_dict() for s in result.submissions],
        "stats": result.stats,
        "warnings": result.warnings,
    }, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 打分：拆成「拼 Prompt」和「解析回复」两步，中间那一次网络请求交给 JS
# --------------------------------------------------------------------------- #

def _submissions(raw_json: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw_json or "[]")
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def prepare_grade(submissions_json: str, rubric_json: str) -> str:
    """把批改任务准备好：算好同批次相似度、本地信号，并拼出完整的 Prompt。

    返回的每一项都自带 ``messages``，JS 只需要拿去发给大模型；
    发完再调 :func:`finish_grade` 把回复交回来解析。
    """
    rubric = _rubric(rubric_json)
    items = _submissions(submissions_json)
    pairs = [(str(s.get("name") or "（未识别）"), str(s.get("content") or "")) for s in items]
    sim_map = pairwise_similarity(pairs, threshold=rubric.similarity_threshold)

    prepared: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        name = str(item.get("name") or "（未识别）")
        content = str(item.get("content") or "")
        student_id = str(item["student_id"]) if item.get("student_id") else None
        similarity = sim_map.get(name, [])

        shell = new_result(
            rubric=rubric, name=name, content=content, student_id=student_id,
            raw_name=str(item.get("raw_name") or name), similarity=similarity,
        )
        messages = build_messages(
            rubric, name=name, content=content, student_id=student_id,
            similarity=similarity, signals=shell.ai_signals,
        )
        prepared.append({
            "index": index,
            "name": name,
            "raw_name": str(item.get("raw_name") or name),
            "student_id": student_id,
            "content": content,
            "max_total": rubric.total,
            "signals": shell.ai_signals,
            "similarity": [h.to_dict() for h in similarity],
            "messages": messages,
        })

    return json.dumps({"ok": True, "items": prepared, "total": len(prepared)}, ensure_ascii=False)


def _shell_from_item(item: dict[str, Any], rubric: Rubric) -> GradeResult:
    return GradeResult(
        name=str(item.get("name") or "（未识别）"),
        raw_name=str(item.get("raw_name") or item.get("name") or ""),
        student_id=item.get("student_id") or None,
        content=str(item.get("content") or ""),
        max_total=float(item.get("max_total") or rubric.total),
        ai_signals=item.get("signals") if isinstance(item.get("signals"), dict) else {},
        similarity=[
            SimilarityHit(str(h.get("name", "")), float(h.get("ratio") or 0), str(h.get("snippet", "")))
            for h in (item.get("similarity") or [])
            if isinstance(h, dict)
        ],
        source="llm",
    )


def finish_grade(item_json: str, reply: str, rubric_json: str) -> str:
    """模型回复 → 结构化成绩。"""
    try:
        item = json.loads(item_json or "{}")
    except json.JSONDecodeError:
        item = {}
    rubric = _rubric(rubric_json)
    result = finish_from_reply(rubric, _shell_from_item(item, rubric), reply or "")
    return json.dumps({"ok": True, "result": result.to_dict()}, ensure_ascii=False)


def fail_grade(item_json: str, message: str, rubric_json: str) -> str:
    """网络/鉴权失败：给老师一份可编辑的空壳，而不是把这条丢掉。"""
    try:
        item = json.loads(item_json or "{}")
    except json.JSONDecodeError:
        item = {}
    rubric = _rubric(rubric_json)
    result = fail_result(rubric, _shell_from_item(item, rubric), message or "调用失败")
    return json.dumps({"ok": True, "result": result.to_dict()}, ensure_ascii=False)


def mock_grade(submissions_json: str, rubric_json: str) -> str:
    """演示模式（不联网）：给界面在没有 API Key 时也能走通全流程。"""
    rubric = _rubric(rubric_json)
    items = _submissions(submissions_json)
    pairs = [(str(s.get("name") or "（未识别）"), str(s.get("content") or "")) for s in items]
    sim_map = pairwise_similarity(pairs, threshold=rubric.similarity_threshold)

    results: list[dict[str, Any]] = []
    for item in items:
        name = str(item.get("name") or "（未识别）")
        shell = new_result(
            rubric=rubric,
            name=name,
            content=str(item.get("content") or ""),
            student_id=(str(item["student_id"]) if item.get("student_id") else None),
            raw_name=str(item.get("raw_name") or name),
            similarity=sim_map.get(name, []),
            source="mock",
        )
        results.append(mock_result(rubric, shell).to_dict())
    return json.dumps({"ok": True, "results": results}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #

def export_b64(results_json: str, rubric_json: str, class_name: str = "",
               mock: bool = False) -> str:
    """生成 .xlsx，返回 base64（浏览器那边解成 Blob 下载）。"""
    rubric = _rubric(rubric_json)
    results = _results(results_json)
    if not results:
        raise ValueError("还没有可导出的成绩")
    data = to_bytes(results, rubric, class_name=class_name or "", mock=bool(mock))
    return base64.b64encode(data).decode("ascii")


def export_fallback_csv(results_json: str, rubric_json: str, mock: bool = False) -> str:
    """退路：openpyxl 没装上时导出 CSV（Excel 能直接打开）。

    只有单张表，没有样式——但绝不至于"点了导出没反应"。
    """
    import csv

    rubric = _rubric(rubric_json)
    results = _results(results_json)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    header = ["序号", "学号", "姓名", "群昵称"]
    header += [f"{c.name}(满分{c.max_score:g})" for c in rubric.criteria]
    header += ["总分", "满分", "风险", "风险说明", "总评", "老师补充", "字数"]
    writer.writerow(header)
    if mock:
        writer.writerow(["【演示分数：未配置 API Key，不能作为成绩依据】"])

    for index, item in enumerate(results, start=1):
        by_key = {c.key: c for c in item.criteria}
        row: list[Any] = [index, item.student_id or "", item.name, item.raw_name or ""]
        row += [by_key[c.key].score if c.key in by_key else 0 for c in rubric.criteria]
        row += [item.total, item.max_total, item.risk_level,
                "；".join(item.risk_reasons), item.overall_comment,
                item.manual_comment or "", len(item.content.strip())]
        writer.writerow(row)
    return buffer.getvalue()


def export_name(rubric_json: str, class_name: str = "") -> str:
    return suggested_filename(_rubric(rubric_json), class_name or "")


def _results(raw_json: str) -> list[GradeResult]:
    try:
        data = json.loads(raw_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [GradeResult.from_dict(r) for r in data if isinstance(r, dict)]
