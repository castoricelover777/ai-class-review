"""大模型打分 + 防抄袭 / 防 AI 代写。

三段式判定（这是本工具的核心价值，不是"调一次 API 拿个分"）：

1. **同批次交叉比对**（本地、确定性、零成本）
   把本批学生的正文两两做 4-gram Jaccard + 最长公共片段，找出雷同对，
   并把结果**喂进**每个学生的 Prompt —— 模型自己看不到别的学生，必须由我们提供。
2. **本地 AI 味启发式**（本地、确定性）
   套话密度、句长均匀度、标点规范度、是否含只有上过课才知道的具体细节、
   口语标记 …… 产出一组可解释的信号，作为模型的第二意见。
3. **大模型判定**（联网）
   Prompt 里明确要求：必须引用原文原句作为证据；证据不足就判 none/low。
   **只输出"疑似"，最终由老师裁定**——工具不做判罚。

网络调用只用标准库 ``urllib``，不依赖 requests / openai，
这样打包成 exe 时依赖面最小。
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable, Sequence

from .config import AppConfig
from .rules import Rubric

__all__ = [
    "CriterionScore",
    "SimilarityHit",
    "GradeResult",
    "ai_flavor_signals",
    "pairwise_similarity",
    "grade_batch",
    "grade_one",
    "chat_completion",
    "LLMError",
]

RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


class LLMError(RuntimeError):
    """调用大模型失败（网络 / 鉴权 / 返回格式）。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class CriterionScore:
    key: str
    name: str
    score: float
    max_score: float
    band: str = ""
    comment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "score": self.score,
            "max_score": self.max_score,
            "band": self.band,
            "comment": self.comment,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CriterionScore:
        return cls(
            key=str(data.get("key", "")),
            name=str(data.get("name", "")),
            score=_as_float(data.get("score")),
            max_score=_as_float(data.get("max_score")),
            band=str(data.get("band", "")),
            comment=str(data.get("comment", "")),
        )


@dataclass
class SimilarityHit:
    name: str
    ratio: float
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ratio": round(self.ratio, 3), "snippet": self.snippet}


@dataclass
class GradeResult:
    name: str
    content: str = ""
    student_id: str | None = None
    raw_name: str = ""
    criteria: list[CriterionScore] = field(default_factory=list)
    total: float = 0.0
    max_total: float = 0.0
    overall_comment: str = ""
    risk_level: str = "none"
    risk_reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    ai_signals: dict[str, Any] = field(default_factory=dict)
    similarity: list[SimilarityHit] = field(default_factory=list)
    error: str | None = None
    source: str = "llm"                # llm / mock
    reviewed: bool = False             # 老师是否已复核
    manual_comment: str = ""           # 老师手填的补充评语

    @property
    def ratio(self) -> float:
        return round(self.total / self.max_total, 3) if self.max_total else 0.0

    @property
    def flagged(self) -> bool:
        return self.risk_level in ("medium", "high")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "raw_name": self.raw_name,
            "student_id": self.student_id,
            "content": self.content,
            "criteria": [c.to_dict() for c in self.criteria],
            "total": self.total,
            "max_total": self.max_total,
            "ratio": self.ratio,
            "overall_comment": self.overall_comment,
            "risk_level": self.risk_level,
            "risk_reasons": list(self.risk_reasons),
            "evidence": list(self.evidence),
            "ai_signals": self.ai_signals,
            "similarity": [h.to_dict() for h in self.similarity],
            "error": self.error,
            "source": self.source,
            "reviewed": self.reviewed,
            "manual_comment": self.manual_comment,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GradeResult:
        """把界面回传的结果（老师可能已经改过分数/评语）还原成对象。

        总分**按各维度分重算**，不信任前端传上来的 total——
        老师改了某个维度分而前端忘了同步总分时，导出的成绩表仍然是自洽的。
        """
        criteria = [
            CriterionScore.from_dict(c)
            for c in (data.get("criteria") or [])
            if isinstance(c, dict)
        ]
        result = cls(
            name=str(data.get("name") or "（未识别）"),
            raw_name=str(data.get("raw_name") or data.get("name") or ""),
            student_id=(str(data["student_id"]) if data.get("student_id") else None),
            content=str(data.get("content") or ""),
            criteria=criteria,
            max_total=_as_float(data.get("max_total")) or round(sum(c.max_score for c in criteria), 2),
            overall_comment=str(data.get("overall_comment") or ""),
            risk_level=_normalize_risk(data.get("risk_level")),
            risk_reasons=_as_str_list(data.get("risk_reasons")),
            evidence=_as_str_list(data.get("evidence")),
            ai_signals=data.get("ai_signals") if isinstance(data.get("ai_signals"), dict) else {},
            similarity=[
                SimilarityHit(str(h.get("name", "")), _as_float(h.get("ratio")),
                              str(h.get("snippet", "")))
                for h in (data.get("similarity") or [])
                if isinstance(h, dict)
            ],
            error=(str(data["error"]) if data.get("error") else None),
            source=str(data.get("source") or "llm"),
            reviewed=bool(data.get("reviewed")),
            manual_comment=str(data.get("manual_comment") or ""),
        )
        result.total = round(sum(c.score for c in criteria), 2)
        result.max_total = result.max_total or round(sum(c.max_score for c in criteria), 2)
        return result


# --------------------------------------------------------------------------- #
# 一、同批次交叉比对（本地，不用模型）
# --------------------------------------------------------------------------- #

_KEEP_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]+")
_NGRAM = 4


def _normalize_for_similarity(text: str) -> str:
    """只保留中英文和数字，便于比对（标点、空格、表情都不算差异）。"""
    return "".join(_KEEP_RE.findall(text or "")).lower()


def _ngrams(text: str, n: int = _NGRAM) -> set[str]:
    if len(text) < n:
        return {text} if text else set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def _longest_common_snippet(a: str, b: str, min_len: int = 12) -> str:
    """找两段文本最长的公共片段（用于给老师看"重合在哪"）。"""
    if not a or not b:
        return ""
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    match = matcher.find_longest_match(0, len(a), 0, len(b))
    if match.size < min_len:
        return ""
    return a[match.a : match.a + match.size]


def pairwise_similarity(
    items: Sequence[tuple[str, str]],
    *,
    threshold: float = 0.55,
    top_k: int = 3,
) -> dict[str, list[SimilarityHit]]:
    """``items`` = [(姓名, 正文), ...]，返回 {姓名: [SimilarityHit, ...]}。

    先算 4-gram Jaccard 粗筛（快），只对疑似雷同的对再算精确比率和重合片段。
    """
    prepared: list[tuple[str, str, set[str]]] = []
    for idx, (name, content) in enumerate(items):
        norm = _normalize_for_similarity(content)
        prepared.append((f"{name}", norm, _ngrams(norm)))

    hits: dict[str, list[SimilarityHit]] = {}
    # 姓名可能重复，先用下标累积，最后按姓名合并
    pair_notes: dict[int, list[SimilarityHit]] = {i: [] for i in range(len(prepared))}

    coarse = max(0.0, threshold - 0.15)   # 粗筛阈值放宽一点，避免漏判
    for i in range(len(prepared)):
        for j in range(i + 1, len(prepared)):
            name_i, norm_i, gram_i = prepared[i]
            name_j, norm_j, gram_j = prepared[j]
            if not norm_i or not norm_j:
                continue
            jac = _jaccard(gram_i, gram_j)
            if jac < coarse:
                continue
            ratio = SequenceMatcher(None, norm_i, norm_j, autojunk=False).ratio()
            score = max(jac, ratio)
            if score < threshold:
                continue
            snippet = _longest_common_snippet(norm_i, norm_j)
            pair_notes[i].append(SimilarityHit(name_j, score, snippet))
            pair_notes[j].append(SimilarityHit(name_i, score, snippet))

    by_name: dict[str, list[int]] = {}
    for idx, (name, _, _) in enumerate(prepared):
        by_name.setdefault(name, []).append(idx)

    for name, indices in by_name.items():
        merged: list[SimilarityHit] = []
        for idx in indices:
            merged.extend(pair_notes[idx])
        merged.sort(key=lambda h: h.ratio, reverse=True)
        hits[name] = merged[:top_k]
    return hits


# --------------------------------------------------------------------------- #
# 二、本地 AI 味启发式信号
# --------------------------------------------------------------------------- #

# 典型的"说了等于没说"的套话
_CLICHE_PHRASES = (
    "通过本次", "通过这节课", "通过这堂课", "通过今天", "深刻认识到", "深刻理解到",
    "具有重要意义", "意义重大", "受益匪浅", "收获颇丰", "总而言之", "综上所述",
    "在当今社会", "随着科技", "随着社会", "不可否认", "值得我们深思", "值得深思",
    "不仅", "而且", "从而", "更加", "有效地", "更好地", "在一定程度上",
    "让我明白了许多道理", "为我们今后", "奠定基础", "开启新的", "新的认识",
    "是我人生", "宝贵的财富", "不可或缺", "举足轻重", "相辅相成",
)

# 只有真的上过这堂课才写得出来的细节
_CONCRETE_MARKERS = (
    "老师提到", "老师说的", "老师举", "老师问", "老师强调", "课上", "课堂上",
    "举个例子", "比如", "我当时", "我上次", "我做作业", "我做过", "我们组",
    "同学说", "有同学", "我注意到", "我记得", "第一次", "实验", "课本第",
    "第一节课", "点名", "提问", "板书", "ppt", "PPT", "演示",
)

# 口语/个人痕迹（AI 代写通常很干净，学生手写常有）
_ORAL_MARKERS = (
    "哈哈", "呃", "嗯", "其实", "感觉", "挺", "有点", "反正", "说白了",
    "不太懂", "没听懂", "懵", "真的", "好像", "我觉得吧", "大概", "差不多",
)

_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")
_END_PUNCT_RE = re.compile(r"[。！？!?…]")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def ai_flavor_signals(text: str) -> dict[str, Any]:
    """产出一组**可解释**的 AI 味信号。刻意做成"给出证据"而不是"给个黑箱分"。"""
    raw = (text or "").strip()
    length = len(raw)
    sentences = _sentences(raw)
    reasons: list[str] = []

    cliche_hits = [p for p in _CLICHE_PHRASES if p in raw]
    cliche_density = round(len(cliche_hits) * 100 / length, 2) if length else 0.0
    concrete_hits = [p for p in _CONCRETE_MARKERS if p.lower() in raw.lower()]
    oral_hits = [p for p in _ORAL_MARKERS if p in raw]

    lens = [len(s) for s in sentences] or [length]
    mean_len = sum(lens) / len(lens)
    if len(lens) > 1 and mean_len > 0:
        std = math.sqrt(sum((x - mean_len) ** 2 for x in lens) / len(lens))
        cv = round(std / mean_len, 3)
    else:
        cv = 0.0

    unique_ratio = round(len(set(raw)) / length, 3) if length else 0.0
    end_punct = len(_END_PUNCT_RE.findall(raw))
    punct_regular = bool(sentences) and end_punct / max(1, len(sentences)) >= 0.8

    score = 0
    if cliche_hits:
        score += min(45, 12 * len(cliche_hits))
        reasons.append(f"套话命中 {len(cliche_hits)} 处：{'、'.join(cliche_hits[:5])}")
    if cv and cv < 0.28 and mean_len > 18 and len(sentences) >= 3:
        score += 15
        reasons.append(f"句子长度过于均匀（变异系数 {cv}），像模板生成")
    if concrete_hits:
        score -= min(30, 10 * len(concrete_hits))
        reasons.append(f"含具体课堂细节 {len(concrete_hits)} 处：{'、'.join(concrete_hits[:5])}")
    if oral_hits:
        score -= min(15, 5 * len(oral_hits))
    if punct_regular and not oral_hits and length >= 150:
        score += 10
        reasons.append("标点规范、无口语痕迹，篇幅较长")
    if length < 80:
        score -= 10
    if unique_ratio and unique_ratio < 0.45 and length > 200:
        score += 8
        reasons.append("用字重复度高，内容展开不足")

    score = int(max(0, min(100, score)))
    if score < 20:
        level = "none"
    elif score < 40:
        level = "low"
    elif score < 65:
        level = "medium"
    else:
        level = "high"

    return {
        "score": score,
        "level": level,
        "cliche_hits": cliche_hits,
        "cliche_density": cliche_density,
        "concrete_hits": concrete_hits,
        "oral_hits": oral_hits,
        "sentence_count": len(sentences),
        "avg_sentence_len": round(mean_len, 1),
        "sentence_len_cv": cv,
        "unique_char_ratio": unique_ratio,
        "punctuation_regular": punct_regular,
        "reasons": reasons,
    }


# --------------------------------------------------------------------------- #
# 三、大模型调用（标准库 urllib，不引第三方 HTTP 库）
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = (
    "你是一位严谨、公正的课程助教，负责评阅学生的课堂心得。\n"
    "你的评分必须严格依据给定的评分规则和档位描述，不得凭个人喜好随意加减分。\n"
    "你只输出一个 JSON 对象，不要输出任何解释文字、前后缀或 Markdown 代码块标记。"
)

_RISK_VALUES = ("none", "low", "medium", "high")


def _build_user_prompt(
    *,
    rubric: Rubric,
    name: str,
    student_id: str | None,
    content: str,
    similarity: Sequence[SimilarityHit],
    signals: dict[str, Any],
) -> str:
    criteria_json = ",\n".join(
        f'    {{"key":"{c.key}","score":<0~{c.max_score:g} 的数字>,"comment":"<1~2句，引用原文片段>"}}'
        for c in rubric.criteria
    )

    if similarity:
        sim_lines = [
            f"- 与「{h.name}」相似度 {h.ratio * 100:.0f}%"
            + (f"，最长重合片段：「{h.snippet}」" if h.snippet else "")
            for h in similarity
        ]
        sim_block = "\n".join(sim_lines)
    else:
        sim_block = "（本批次中没有发现明显雷同的同学）"

    hint_lines = [f"- {r}" for r in signals.get("reasons", [])] or ["（无）"]

    ai_rule = ""
    if rubric.ai_policy == "forbidden":
        ai_rule = "本次作业明确禁止使用 AI 代写，请从严判断。"
    elif rubric.ai_policy == "allowed":
        ai_rule = "本次允许使用 AI 辅助，重点看是否仍有自己的思考。"

    return f"""{rubric.prompt_block()}

【任务】按上面的评分规则评阅下面这一份课堂心得。

【评分方法】
1. 对每个维度，先逐条对照档位描述选出最贴近的档位，再在档位分值上下浮动不超过 10% 做微调，不要机械套档位。
2. 每个维度的评语要具体：好在哪、差在哪，并**引用学生原文的片段**。禁止写"写得不错""内容充实"这类空话。
3. 总分即各维度得分之和，不需要你输出总分，但要自己核对一遍。

【防抄袭要求】
A. 复述式抄袭：若通篇只是复述课件、教材或老师原话，请指出，并把"个人思考"类维度打到最低档。
B. 学生之间互相抄袭：下面是本批次内该生与其他同学的**相似度比对结果**（由工具计算，仅供参考，可能存在引用同一课件导致的正常重合）。请你结合它判断是否雷同；若判定雷同，必须在 risk_reasons 里说明与谁雷同、重合在哪里。
C. 改写式抄袭：注意同义替换、语序调整但逻辑链完全一致的段落。

【防 AI 代写要求】{ai_rule}
请重点检查以下特征，并在判断为 AI 代写时**必须引用原文原句**作为证据：
- 空话套话密度高：例如"通过本次课程的学习，我深刻认识到……具有重要意义"，说了一大段却没有具体信息；
- 缺少只有上过这堂课才写得出的细节：没有老师举的例子、课堂提问、同学发言、具体概念或数据；
- 结构过度工整：整齐的"首先/其次/最后"、各段篇幅接近、像模板填空；
- 表达水平与身份不符：用词过度书面化、术语堆砌，但逻辑跳跃，或对术语的理解经不起推敲；
- 内容与课堂的对应非常"泛"：把任意一节课的主题替换进去都成立。
注意：**不要仅因为"写得通顺、没有错别字"就判定为 AI 代写**。证据不足时，risk_level 必须给 "none" 或 "low"。

【工具计算的同批次相似度】
{sim_block}

【工具检测到的语言特征（仅供参考，不能直接作为判罚依据）】
{chr(10).join(hint_lines)}

【学生】姓名：{name}　学号：{student_id or "未提供"}　正文字数：{len(content)}

【心得正文】
\"\"\"
{content}
\"\"\"

【输出格式】只输出下面这个 JSON，不要有任何多余文字：
{{
  "criteria": [
{criteria_json}
  ],
  "overall_comment": "<对该生的总评，2~3 句，直接对老师汇报>",
  "risk_level": "none|low|medium|high",
  "risk_reasons": ["<判定依据，没有就给空数组>"],
  "evidence": ["<学生原文中支持你判断的句子，没有就给空数组>"]
}}"""


def chat_completion(
    cfg: AppConfig,
    messages: list[dict[str, str]],
    *,
    timeout: int | None = None,
    json_mode: bool = True,
) -> str:
    """调用 OpenAI 兼容的 /chat/completions。只用标准库。"""
    if not cfg.has_api_key:
        raise LLMError("还没有配置 API Key")

    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    body = _post_json(url, payload, cfg.api_key, timeout or cfg.timeout)
    return _extract_content(body)


def _post_json(url: str, payload: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # pragma: no cover - 尽力而为
            pass
        # 有些服务商不支持 response_format，去掉再试一次
        if exc.code == 400 and "response_format" in detail and "response_format" in payload:
            retry = dict(payload)
            retry.pop("response_format", None)
            return _post_json(url, retry, api_key, timeout)
        if exc.code in (401, 403):
            raise LLMError(f"API Key 被拒绝（HTTP {exc.code}），请在设置里检查 Key 是否填对") from exc
        if exc.code == 404:
            raise LLMError(f"接口地址不对（HTTP 404）：{url}，请检查请求地址是否需要带 /v1") from exc
        if exc.code == 429:
            raise LLMError("请求太频繁或额度不足（HTTP 429），请稍后再试") from exc
        raise LLMError(f"接口返回错误 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"连不上接口（{exc.reason}），请检查网络或请求地址") from exc
    except TimeoutError as exc:
        raise LLMError("等待接口响应超时，请稍后重试或调大超时时间") from exc
    except json.JSONDecodeError as exc:
        raise LLMError(f"接口返回的不是合法 JSON：{exc}") from exc


def _extract_content(body: dict[str, Any]) -> str:
    try:
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"接口返回结构不符合预期：{json.dumps(body, ensure_ascii=False)[:300]}") from exc


# --------------------------------------------------------------------------- #
# 四、解析模型输出
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_model_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON。容错：代码块、前后废话、中文引号。"""
    raw = (text or "").strip()
    if not raw:
        raise LLMError("模型返回了空内容")

    fence = _FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidate = raw[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            cleaned = (
                candidate.replace("“", '"').replace("”", '"')
                .replace("，", ",").replace("：", ":")
            )
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as exc:
                raise LLMError(f"模型返回的内容不是合法 JSON：{exc}") from exc
    raise LLMError("模型返回的内容里找不到 JSON")


def _match_criterion(rubric: Rubric, item: dict[str, Any]):
    key = str(item.get("key", "")).strip()
    if key:
        found = rubric.find(key)
        if found:
            return found
    name = str(item.get("name", "")).strip()
    for c in rubric.criteria:
        if c.name == name or c.key == key:
            return c
    return None


def _normalize_risk(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in _RISK_VALUES:
        return text
    mapping = {"无": "none", "低": "low", "中": "medium", "高": "high",
               "低风险": "low", "中风险": "medium", "高风险": "high", "没有": "none"}
    return mapping.get(text, "none")


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 五、单份 / 批量打分
# --------------------------------------------------------------------------- #

def grade_one(
    *,
    rubric: Rubric,
    cfg: AppConfig,
    name: str,
    content: str,
    student_id: str | None = None,
    raw_name: str = "",
    similarity: Sequence[SimilarityHit] = (),
    mode: str | None = None,
) -> GradeResult:
    """给一份心得打分。``mode="mock"`` 时走本地演示打分，不联网。"""
    effective_mode = mode or (cfg.mode if cfg else "mock")
    signals = ai_flavor_signals(content)
    result = GradeResult(
        name=name,
        raw_name=raw_name or name,
        content=content,
        student_id=student_id,
        max_total=rubric.total,
        similarity=list(similarity),
        ai_signals=signals,
        source="mock" if effective_mode == "mock" else "llm",
    )

    if effective_mode == "mock":
        _fill_mock(result, rubric, content, signals)
        _merge_risk(result, signals)
        return result

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_user_prompt(
                rubric=rubric,
                name=name,
                student_id=student_id,
                content=content,
                similarity=similarity,
                signals=signals,
            ),
        },
    ]

    try:
        reply = chat_completion(cfg, messages)
        payload = parse_model_json(reply)
    except LLMError as exc:
        result.error = str(exc)
        # 出错也要给老师一份可编辑的空壳，而不是把这条丢掉
        _fill_empty(result, rubric)
        return result

    _fill_from_model(result, rubric, payload)
    _merge_risk(result, signals)
    return result


def _fill_from_model(result: GradeResult, rubric: Rubric, payload: dict[str, Any]) -> None:
    raw_items = payload.get("criteria")
    if not isinstance(raw_items, list):
        result.error = "模型没有按格式返回各维度分数"
        raw_items = []

    by_key: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        matched = _match_criterion(rubric, item)
        if matched:
            by_key[matched.key] = item

    result.criteria = []
    missing: list[str] = []
    for c in rubric.criteria:
        item = by_key.get(c.key)
        if item is None:
            missing.append(c.name)
            result.criteria.append(
                CriterionScore(c.key, c.name, 0.0, c.max_score, rubric.band_of(c.key, 0), "")
            )
            continue
        score = rubric.clamp_score(c.key, item.get("score"))
        result.criteria.append(
            CriterionScore(
                key=c.key,
                name=c.name,
                score=score,
                max_score=c.max_score,
                band=rubric.band_of(c.key, score),
                comment=str(item.get("comment", "")).strip(),
            )
        )
    if missing and not result.error:
        result.error = "模型漏掉了这些维度：" + "、".join(missing)

    result.total = round(sum(c.score for c in result.criteria), 2)
    result.overall_comment = str(payload.get("overall_comment", "")).strip()
    result.risk_level = _normalize_risk(payload.get("risk_level"))
    result.risk_reasons = _as_str_list(payload.get("risk_reasons"))
    result.evidence = _as_str_list(payload.get("evidence"))


def _fill_empty(result: GradeResult, rubric: Rubric) -> None:
    result.criteria = [
        CriterionScore(c.key, c.name, 0.0, c.max_score, rubric.band_of(c.key, 0), "")
        for c in rubric.criteria
    ]
    result.total = 0.0


def _fill_mock(result: GradeResult, rubric: Rubric, content: str, signals: dict[str, Any]) -> None:
    """演示模式：用本地启发式给出**确定性**的占位分数（不联网）。

    目的只是让老师在没有 Key 的情况下先把整个流程走通，分数本身没有评阅价值，
    界面上会显著标注"演示分数"。
    """
    length = len(content.strip())
    length_factor = min(1.0, length / 300)                 # >=300 字算充分展开
    detail_factor = min(1.0, len(signals.get("concrete_hits", [])) / 3)
    cliche_penalty = min(0.35, 0.12 * len(signals.get("cliche_hits", [])))
    oral_bonus = min(0.15, 0.05 * len(signals.get("oral_hits", [])))

    base = 0.45 + 0.30 * length_factor + 0.20 * detail_factor + oral_bonus - cliche_penalty
    base = max(0.25, min(0.98, base))

    weights = {
        "understanding": 0.0,
        "thinking": -0.05,
        "expression": 0.05,
    }

    result.criteria = []
    for idx, c in enumerate(rubric.criteria):
        offset = weights.get(c.key, (idx - 1) * 0.03)
        ratio = max(0.2, min(1.0, base + offset))
        score = round(c.max_score * ratio, 2)
        band = rubric.band_of(c.key, score)
        result.criteria.append(
            CriterionScore(
                key=c.key,
                name=c.name,
                score=score,
                max_score=c.max_score,
                band=band,
                comment=f"【演示数据】按字数、具体细节、套话密度估算，落在「{band}」档。配置 API Key 后这里会换成真实评语。",
            )
        )
    result.total = round(sum(c.score for c in result.criteria), 2)
    result.overall_comment = (
        "【演示模式】这是根据字数与语言特征给出的占位分数，用于验证流程；"
        "真实的评阅意见需要先配置 API Key 再重新打分。"
    )
    result.evidence = []
    result.risk_level = "none"
    result.risk_reasons = []


def _merge_risk(result: GradeResult, signals: dict[str, Any]) -> None:
    """把本地启发式信号和模型判断合起来。

    **本地信号只能上调到 medium，且必须注明来源**——工具负责提示，不负责定罪。
    """
    local_level = signals.get("level", "none")
    if local_level == "none":
        return
    local_reasons = [f"工具检测：{r}" for r in signals.get("reasons", []) if r]
    note_reasons = [r for r in local_reasons if not any(r[5:] == e for e in result.risk_reasons)]

    if RISK_ORDER.get(local_level, 0) > RISK_ORDER.get(result.risk_level, 0):
        upgraded = local_level if local_level in ("low", "medium") else "medium"
        if RISK_ORDER[upgraded] > RISK_ORDER.get(result.risk_level, 0):
            if not result.risk_reasons:
                result.risk_reasons = note_reasons
            else:
                result.risk_reasons = result.risk_reasons + note_reasons
            result.risk_level = upgraded


def grade_batch(
    submissions: Sequence[dict[str, Any]],
    rubric: Rubric,
    cfg: AppConfig,
    *,
    mode: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[GradeResult]:
    """批量打分。

    ``submissions`` 每项至少要有 ``name`` 与 ``content``（可以来自解析结果，也可以是
    老师在前端手工改过的行）。先做同批次交叉比对，再并发调用大模型。
    """
    items = [
        (str(s.get("name") or "（未识别）"), str(s.get("content") or ""))
        for s in submissions
    ]
    sim_map = pairwise_similarity(items, threshold=rubric.similarity_threshold)

    results: list[GradeResult | None] = [None] * len(submissions)
    total = len(submissions)

    def work(index: int) -> tuple[int, GradeResult]:
        item = submissions[index]
        name = str(item.get("name") or "（未识别）")
        return index, grade_one(
            rubric=rubric,
            cfg=cfg,
            name=name,
            content=str(item.get("content") or ""),
            student_id=(str(item["student_id"]) if item.get("student_id") else None),
            raw_name=str(item.get("raw_name") or name),
            similarity=sim_map.get(name, []),
            mode=mode,
        )

    if total == 0:
        return []

    done = 0
    workers = max(1, min(cfg.concurrency if cfg else 4, total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, i) for i in range(total)]
        for future in as_completed(futures):
            index, result = future.result()
            results[index] = result
            done += 1
            if progress:
                progress(done, total)

    fallback = GradeResult(name="", max_total=rubric.total)
    return [r if r is not None else fallback for r in results]


def recalibrate(rubric: Rubric, results: Iterable[GradeResult]) -> None:
    """老师改了规则分值后，按比例重算已有分数（界面上的"按新规则换算"）。"""
    for result in results:
        new_total = 0.0
        for c in result.criteria:
            fresh = rubric.find(c.key)
            if fresh is None:
                new_total += c.score
                continue
            ratio = (c.score / c.max_score) if c.max_score else 0.0
            c.max_score = fresh.max_score
            c.name = fresh.name
            c.score = round(fresh.max_score * ratio, 2)
            c.band = rubric.band_of(c.key, c.score)
            new_total += c.score
        result.total = round(new_total, 2)
        result.max_total = rubric.total
