"""评分规则：维度 / 分值 / 档位描述，可存 JSON 复用。

设计要点
--------
* 老师的操作粒度是"**维度**"（观点理解、个人思考…），每个维度有满分和几档描述。
* 档位用**比例**而不是绝对分：老师改满分（5 → 20）时，档位描述不用重写。
* ``prompt_block()`` 把规则渲染成给大模型看的文本——**这是打分稳定性的关键**，
  档位描述写得越具体，模型给分越不走样。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Level", "Criterion", "Rubric", "DEFAULT_RUBRIC", "RUBRIC_VERSION"]

RUBRIC_VERSION = 1

# 满分/良好/待改进 三档。老师改 ratio 或描述即可，不需要懂代码。
_DEFAULT_LEVELS = (
    ("优秀", 1.0),
    ("良好", 0.7),
    ("待改进", 0.4),
)


@dataclass
class Level:
    """一个评分档位。``ratio`` 是满分比例（0~1]，实际分值 = 满分 × ratio。"""

    label: str
    ratio: float
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "ratio": self.ratio, "description": self.description}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Level:
        return cls(
            label=str(data.get("label", "")).strip() or "档位",
            ratio=_clamp_ratio(data.get("ratio", 1.0)),
            description=str(data.get("description", "")).strip(),
        )


@dataclass
class Criterion:
    """一个评分维度。"""

    key: str
    name: str
    max_score: float
    description: str = ""
    levels: list[Level] = field(default_factory=list)

    def level_score(self, ratio: float) -> float:
        return round(self.max_score * _clamp_ratio(ratio), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "max_score": self.max_score,
            "description": self.description,
            "levels": [lv.to_dict() for lv in self.levels],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Criterion:
        raw_levels = data.get("levels") or []
        levels = [Level.from_dict(lv) for lv in raw_levels if isinstance(lv, dict)]
        levels.sort(key=lambda lv: lv.ratio, reverse=True)
        return cls(
            key=str(data.get("key", "")).strip() or _slug(str(data.get("name", "criterion"))),
            name=str(data.get("name", "")).strip() or "未命名维度",
            max_score=_clamp_score(data.get("max_score", 0)),
            description=str(data.get("description", "")).strip(),
            levels=levels,
        )


@dataclass
class Rubric:
    """一整套评分规则。"""

    name: str = "课堂心得评分规则"
    criteria: list[Criterion] = field(default_factory=list)
    context: str = ""                 # 本次心得的任务说明（会进 Prompt，直接影响准确度）
    min_chars: int = 15
    ai_policy: str = "unknown"        # forbidden / allowed / unknown
    extra_instructions: str = ""
    similarity_threshold: float = 0.55

    # ------------------------------------------------------------------ #
    # 基本属性
    # ------------------------------------------------------------------ #

    @property
    def total(self) -> float:
        return round(sum(c.max_score for c in self.criteria), 2)

    def find(self, key: str) -> Criterion | None:
        for c in self.criteria:
            if c.key == key:
                return c
        return None

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": RUBRIC_VERSION,
            "name": self.name,
            "context": self.context,
            "min_chars": self.min_chars,
            "ai_policy": self.ai_policy,
            "extra_instructions": self.extra_instructions,
            "similarity_threshold": self.similarity_threshold,
            "total": self.total,
            "criteria": [c.to_dict() for c in self.criteria],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rubric:
        raw = data.get("criteria") or []
        criteria = [Criterion.from_dict(c) for c in raw if isinstance(c, dict)]
        if not criteria:
            criteria = [c for c in DEFAULT_RUBRIC.criteria]
        return cls(
            name=str(data.get("name", "")).strip() or "课堂心得评分规则",
            criteria=criteria,
            context=str(data.get("context", "")).strip(),
            min_chars=_as_int(data.get("min_chars"), 15, 0, 100000),
            ai_policy=str(data.get("ai_policy", "unknown")).strip() or "unknown",
            extra_instructions=str(data.get("extra_instructions", "")).strip(),
            similarity_threshold=_clamp_ratio(data.get("similarity_threshold", 0.55)) or 0.55,
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> Rubric:
        return cls.from_dict(json.loads(text))

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #

    def validate(self) -> list[str]:
        """返回中文错误列表；空列表 = 合法。给界面直接显示。"""
        errors: list[str] = []
        if not self.name.strip():
            errors.append("规则名称不能为空")
        if not self.criteria:
            errors.append("至少要有一个评分维度")
        if self.total <= 0:
            errors.append("总分必须大于 0")

        seen: set[str] = set()
        for idx, c in enumerate(self.criteria, start=1):
            prefix = f"第 {idx} 个维度（{c.name or '未命名'}）"
            if not c.name.strip():
                errors.append(f"{prefix}：名称不能为空")
            if c.key in seen:
                errors.append(f"{prefix}：标识 key「{c.key}」重复了")
            seen.add(c.key)
            if c.max_score <= 0:
                errors.append(f"{prefix}：满分必须大于 0")
            for lv in c.levels:
                if not (0 < lv.ratio <= 1):
                    errors.append(f"{prefix}：档位「{lv.label}」的比例必须在 0~1 之间")
        if self.min_chars < 0:
            errors.append("最少字数不能是负数")
        return errors

    # ------------------------------------------------------------------ #
    # 给大模型看的规则文本
    # ------------------------------------------------------------------ #

    def prompt_block(self) -> str:
        lines: list[str] = [f"【评分规则：{self.name}】", f"总分 {_fmt(self.total)} 分。"]
        if self.context:
            lines.append("")
            lines.append("【本次心得的要求 / 课堂背景】（评分时以此为准）")
            lines.append(self.context.strip())

        lines.append("")
        lines.append("【评分维度】")
        for idx, c in enumerate(self.criteria, start=1):
            head = f"{idx}. {c.name}（满分 {_fmt(c.max_score)} 分）"
            if c.description:
                head += f"—— {c.description}"
            lines.append(head)
            for lv in c.levels:
                lines.append(f"   - {lv.label}（{_fmt(c.level_score(lv.ratio))} 分）：{lv.description}")

        if self.ai_policy == "forbidden":
            lines.append("")
            lines.append("【AI 使用规定】本次明确要求学生独立完成，禁止使用 AI 代写。")
        elif self.ai_policy == "allowed":
            lines.append("")
            lines.append("【AI 使用规定】本次允许使用 AI 辅助，但仍然要求有自己的思考。")

        if self.extra_instructions:
            lines.append("")
            lines.append("【老师的额外要求】")
            lines.append(self.extra_instructions.strip())

        return "\n".join(lines)

    def clamp_score(self, key: str, value: Any) -> float:
        """把模型给的分数夹到 [0, 满分] 并保留两位。"""
        criterion = self.find(key)
        if criterion is None:
            return _clamp_score(value)
        try:
            num = float(value)
        except (TypeError, ValueError):
            num = 0.0
        return round(max(0.0, min(criterion.max_score, num)), 2)

    def ratio_of(self, key: str, value: Any) -> float:
        criterion = self.find(key)
        if criterion is None or criterion.max_score <= 0:
            return 0.0
        return round(self.clamp_score(key, value) / criterion.max_score, 3)

    def band_of(self, key: str, value: Any) -> str:
        """按档位锚点反查这个分数落在哪一档（给界面/Excel 显示）。

        档位里给的是**锚点分**（优秀 = 满分、良好 = 70%、待改进 = 40%），
        所以分界取**相邻锚点的中点**——离哪个锚点近就算哪一档。
        直接用锚点当门槛会得到"4.5/5 算良好"这种反直觉结果。
        """
        criterion = self.find(key)
        if criterion is None or not criterion.levels:
            return ""
        levels = sorted(criterion.levels, key=lambda lv: lv.ratio, reverse=True)
        ratio = self.ratio_of(key, value)
        for idx, level in enumerate(levels):
            upper = (levels[idx - 1].ratio + level.ratio) / 2 if idx > 0 else None
            lower = (level.ratio + levels[idx + 1].ratio) / 2 if idx + 1 < len(levels) else 0.0
            if ratio >= lower - 1e-9 and (upper is None or ratio <= upper + 1e-9):
                return level.label
        return levels[-1].label


# --------------------------------------------------------------------------- #
# 默认规则：观点理解 5 / 个人思考 3 / 文字表达 2，满分 10
# --------------------------------------------------------------------------- #

DEFAULT_RUBRIC = Rubric(
    name="课堂心得评分规则（默认）",
    context=(
        "学生上完一节课后写的课堂心得，要求写出对课堂内容的理解和自己的思考，"
        "不是复述课件，也不是泛泛而谈的感想。"
    ),
    min_chars=15,
    ai_policy="unknown",
    criteria=[
        Criterion(
            key="understanding",
            name="观点理解",
            max_score=5,
            description="是否准确抓住了这堂课的核心观点，而不是零散复述细节",
            levels=[
                Level("优秀", 1.0, "准确概括课堂核心观点，能指出老师强调的重点以及重点之间的关系。"),
                Level("良好", 0.7, "基本复述出主要内容，但对重点的取舍不够准确，或有个别理解偏差。"),
                Level("待改进", 0.4, "只是零散复述细节或误读了课堂内容，看不出对核心观点的把握。"),
            ],
        ),
        Criterion(
            key="thinking",
            name="个人思考",
            max_score=3,
            description="是否有自己的分析、质疑、联想或应用，而不是复述课件",
            levels=[
                Level("优秀", 1.0, "提出自己的判断、疑问或反驳，并结合自身经历、其他课程或现实场景展开。"),
                Level("良好", 0.7, "有个人感受，但停留在「我觉得很有用/很受启发」，缺少进一步展开。"),
                Level("待改进", 0.4, "通篇复述课堂内容，没有个人观点，或观点与内容无关。"),
            ],
        ),
        Criterion(
            key="expression",
            name="文字表达",
            max_score=2,
            description="结构是否清楚、语句是否通顺、有无明显语病或错别字",
            levels=[
                Level("优秀", 1.0, "结构清楚、语句通顺、用词准确，几乎没有语病和错别字。"),
                Level("良好", 0.7, "表达基本清楚，个别地方啰嗦、口语化或不通顺。"),
                Level("待改进", 0.4, "结构混乱、语句不通顺，或错别字/病句较多，影响理解。"),
            ],
        ),
    ],
)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _clamp_ratio(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, num))


def _clamp_score(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, round(num, 2))


def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _fmt(num: float) -> str:
    """5.0 → 5；3.5 → 3.5。提示词里数字好看一点。"""
    return f"{num:g}"


def _slug(text: str) -> str:
    import re

    ascii_only = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return ascii_only or "criterion"
