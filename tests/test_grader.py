"""打分与防抄袭/防 AI 代写（core/grader.py）的测试。

**全部离线**：联网路径用 unittest.mock 顶替，保证测试不依赖网络和 API Key。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from core.config import AppConfig
from core.grader import (
    LLMError,
    ai_flavor_signals,
    grade_batch,
    grade_one,
    pairwise_similarity,
    parse_model_json,
    recalibrate,
)
from core.rules import DEFAULT_RUBRIC

AI_FLAVORED = (
    "通过本次课程的学习，我深刻认识到团队合作具有重要意义。首先，我们要不断学习新知识。"
    "其次，我们要勇于创新。最后，我们要勇于承担责任。总而言之，这次课程让我受益匪浅，"
    "为我们今后的发展奠定基础，是不可或缺的宝贵财富。"
)

CONCRETE = (
    "老师提到 TCP 三次握手的时候举了个例子，说这就像打电话先确认对方在不在。"
    "我上次做实验抓包看到 SYN、ACK 那几个包，当时完全没看懂，这节课突然就明白了。"
    "不过我还是有点懵：为什么不能像 UDP 那样直接发？"
)

UNRELATED = "今天食堂的饭特别好吃，我和室友打了一下午球，挺开心的。"


def live_cfg() -> AppConfig:
    return AppConfig(api_key="sk-test-1234567890abcdef", model="test-model")


def mock_cfg() -> AppConfig:
    return AppConfig(api_key="")


def model_reply(**overrides) -> str:
    payload = {
        "criteria": [
            {"key": "understanding", "score": 4.5, "comment": "准确抓住了三次握手这个重点。"},
            {"key": "thinking", "score": 2.0, "comment": "结合了自己的抓包经历。"},
            {"key": "expression", "score": 1.6, "comment": "语句通顺，个别地方口语化。"},
        ],
        "overall_comment": "内容扎实，有自己的观察。",
        "risk_level": "none",
        "risk_reasons": [],
        "evidence": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# AI 味启发式
# --------------------------------------------------------------------------- #

class TestAiFlavorSignals(unittest.TestCase):
    def test_detects_cliche(self):
        signals = ai_flavor_signals(AI_FLAVORED)
        self.assertTrue(signals["cliche_hits"])
        self.assertIn("通过本次", signals["cliche_hits"])
        self.assertTrue(any("套话" in r for r in signals["reasons"]))

    def test_concrete_text_scores_lower_than_cliche_text(self):
        heavy = ai_flavor_signals(AI_FLAVORED)
        light = ai_flavor_signals(CONCRETE)
        self.assertGreater(heavy["score"], light["score"])
        self.assertEqual(light["level"], "none")

    def test_concrete_text_gets_credit(self):
        signals = ai_flavor_signals(CONCRETE)
        self.assertTrue(signals["concrete_hits"])
        self.assertTrue(signals["oral_hits"])
        self.assertTrue(any("具体课堂细节" in r for r in signals["reasons"]))

    def test_signals_are_explainable(self):
        signals = ai_flavor_signals(AI_FLAVORED)
        for key in ("score", "level", "reasons", "sentence_count", "avg_sentence_len",
                    "sentence_len_cv", "unique_char_ratio"):
            self.assertIn(key, signals)
        self.assertTrue(0 <= signals["score"] <= 100)
        self.assertIn(signals["level"], ("none", "low", "medium", "high"))

    def test_empty_text_does_not_crash(self):
        signals = ai_flavor_signals("")
        self.assertEqual(signals["score"], 0)
        self.assertEqual(signals["level"], "none")


# --------------------------------------------------------------------------- #
# 同批次交叉比对
# --------------------------------------------------------------------------- #

class TestPairwiseSimilarity(unittest.TestCase):
    def test_identical_texts_are_flagged(self):
        hits = pairwise_similarity([("张三", CONCRETE), ("李四", CONCRETE)])
        self.assertTrue(hits["张三"])
        self.assertEqual(hits["张三"][0].name, "李四")
        self.assertGreater(hits["张三"][0].ratio, 0.95)
        self.assertTrue(hits["张三"][0].snippet)          # 要给出重合片段
        self.assertEqual(hits["李四"][0].name, "张三")

    def test_near_copy_is_flagged(self):
        near = CONCRETE.replace("完全没看懂", "根本没搞明白").replace("有点懵", "有点困惑")
        hits = pairwise_similarity([("张三", CONCRETE), ("李四", near)], threshold=0.55)
        self.assertTrue(hits["张三"])
        self.assertGreater(hits["张三"][0].ratio, 0.7)

    def test_unrelated_texts_are_not_flagged(self):
        hits = pairwise_similarity([("张三", CONCRETE), ("李四", UNRELATED)])
        self.assertEqual(hits["张三"], [])
        self.assertEqual(hits["李四"], [])

    def test_punctuation_and_whitespace_do_not_hide_copies(self):
        messy = "  今天 的课，让我理解了三次握手的必要性！！！ 以前只知道背概念…… "
        clean = "今天的课让我理解了三次握手的必要性。以前只知道背概念。"
        hits = pairwise_similarity([("A", messy), ("B", clean)], threshold=0.7)
        self.assertTrue(hits["A"])

    def test_threshold_controls_sensitivity(self):
        # 完全相同：阈值再严也要命中
        strict = pairwise_similarity([("A", CONCRETE), ("B", CONCRETE)], threshold=0.99)
        self.assertTrue(strict["A"])
        # 完全无关：默认阈值下不该命中
        self.assertEqual(pairwise_similarity([("A", CONCRETE), ("B", UNRELATED)])["A"], [])
        # 阈值放到极低：什么都能配上，但相似度应该很低（说明确实是无关的两篇）
        loose = pairwise_similarity([("A", CONCRETE), ("B", UNRELATED)], threshold=0.01)
        self.assertTrue(loose["A"])
        self.assertLess(loose["A"][0].ratio, 0.2)

    def test_empty_content_is_skipped(self):
        hits = pairwise_similarity([("A", ""), ("B", CONCRETE)])
        self.assertEqual(hits["A"], [])

    def test_duplicate_names_do_not_crash(self):
        hits = pairwise_similarity([("同名", CONCRETE), ("同名", CONCRETE)])
        self.assertIn("同名", hits)


# --------------------------------------------------------------------------- #
# 模型输出解析
# --------------------------------------------------------------------------- #

class TestParseModelJson(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(parse_model_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(parse_model_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_with_surrounding_prose(self):
        self.assertEqual(parse_model_json('好的，结果如下：\n{"a": 1}\n以上。'), {"a": 1})

    def test_chinese_punctuation_fallback(self):
        self.assertEqual(parse_model_json('{"a"：“1”}'), {"a": "1"})

    def test_garbage_raises(self):
        with self.assertRaises(LLMError):
            parse_model_json("我不知道该怎么回答")
        with self.assertRaises(LLMError):
            parse_model_json("")


# --------------------------------------------------------------------------- #
# 单份打分
# --------------------------------------------------------------------------- #

class TestGradeOne(unittest.TestCase):
    def test_live_mode_happy_path(self):
        with patch("core.grader.chat_completion", return_value=model_reply()):
            result = grade_one(rubric=DEFAULT_RUBRIC, cfg=live_cfg(),
                               name="张三", content=CONCRETE, student_id="2023123456")
        self.assertIsNone(result.error)
        self.assertEqual(result.source, "llm")
        self.assertEqual([c.score for c in result.criteria], [4.5, 2.0, 1.6])
        self.assertEqual(result.total, 8.1)
        self.assertEqual(result.max_total, 10)
        self.assertEqual(result.criteria[0].band, "优秀")
        self.assertIn("三次握手", result.criteria[0].comment)

    def test_model_scores_are_clamped_to_max(self):
        reply = model_reply(criteria=[
            {"key": "understanding", "score": 99, "comment": "超了"},
            {"key": "thinking", "score": -5, "comment": "负分"},
            {"key": "expression", "score": 1, "comment": "ok"},
        ])
        with patch("core.grader.chat_completion", return_value=reply):
            result = grade_one(rubric=DEFAULT_RUBRIC, cfg=live_cfg(), name="张三", content=CONCRETE)
        self.assertEqual([c.score for c in result.criteria], [5.0, 0.0, 1.0])
        self.assertEqual(result.total, 6.0)

    def test_missing_criterion_is_reported(self):
        reply = model_reply(criteria=[{"key": "understanding", "score": 4, "comment": "ok"}])
        with patch("core.grader.chat_completion", return_value=reply):
            result = grade_one(rubric=DEFAULT_RUBRIC, cfg=live_cfg(), name="张三", content=CONCRETE)
        self.assertIn("漏掉", result.error or "")
        self.assertEqual(len(result.criteria), 3)
        self.assertEqual(result.criteria[1].score, 0)

    def test_matching_by_name_when_key_is_wrong(self):
        reply = model_reply(criteria=[
            {"key": "维度1", "name": "观点理解", "score": 4, "comment": "ok"},
            {"key": "维度2", "name": "个人思考", "score": 2, "comment": "ok"},
            {"key": "维度3", "name": "文字表达", "score": 1.5, "comment": "ok"},
        ])
        with patch("core.grader.chat_completion", return_value=reply):
            result = grade_one(rubric=DEFAULT_RUBRIC, cfg=live_cfg(), name="张三", content=CONCRETE)
        self.assertEqual([c.score for c in result.criteria], [4.0, 2.0, 1.5])

    def test_risk_fields_are_parsed(self):
        reply = model_reply(risk_level="high",
                            risk_reasons=["与李四高度雷同"],
                            evidence=["老师提到 TCP 三次握手的时候举了个例子"])
        with patch("core.grader.chat_completion", return_value=reply):
            result = grade_one(rubric=DEFAULT_RUBRIC, cfg=live_cfg(), name="张三", content=CONCRETE)
        self.assertEqual(result.risk_level, "high")
        self.assertIn("与李四高度雷同", result.risk_reasons)
        self.assertTrue(result.evidence)
        self.assertTrue(result.flagged)

    def test_chinese_risk_level_is_normalized(self):
        reply = model_reply(risk_level="高")
        with patch("core.grader.chat_completion", return_value=reply):
            result = grade_one(rubric=DEFAULT_RUBRIC, cfg=live_cfg(), name="张三", content=CONCRETE)
        self.assertEqual(result.risk_level, "high")

    def test_llm_error_is_captured_not_raised(self):
        with patch("core.grader.chat_completion", side_effect=LLMError("API Key 被拒绝")):
            result = grade_one(rubric=DEFAULT_RUBRIC, cfg=live_cfg(), name="张三", content=CONCRETE)
        self.assertIn("API Key", result.error or "")
        self.assertEqual(result.total, 0)
        self.assertEqual(len(result.criteria), 3)      # 仍然给老师一份可编辑的空壳

    def test_local_signal_upgrades_model_none(self):
        with patch("core.grader.chat_completion", return_value=model_reply(risk_level="none")):
            result = grade_one(rubric=DEFAULT_RUBRIC, cfg=live_cfg(), name="张三",
                               content=AI_FLAVORED * 2)
        self.assertIn(result.risk_level, ("low", "medium"))
        self.assertTrue(any("工具检测" in r for r in result.risk_reasons))

    def test_local_signal_never_exceeds_medium(self):
        with patch("core.grader.chat_completion", return_value=model_reply(risk_level="none")):
            result = grade_one(rubric=DEFAULT_RUBRIC, cfg=live_cfg(), name="张三",
                               content=AI_FLAVORED * 5)
        self.assertIn(result.risk_level, ("low", "medium"))
        self.assertNotEqual(result.risk_level, "high")

    def test_mock_mode_does_not_call_network(self):
        with patch("core.grader.chat_completion", side_effect=AssertionError("不该联网")):
            result = grade_one(rubric=DEFAULT_RUBRIC, cfg=mock_cfg(), name="张三", content=CONCRETE)
        self.assertEqual(result.source, "mock")
        self.assertIsNone(result.error)
        self.assertEqual(len(result.criteria), 3)
        self.assertGreater(result.total, 0)
        self.assertLessEqual(result.total, 10)
        self.assertIn("演示", result.overall_comment)

    def test_mock_mode_is_deterministic(self):
        first = grade_one(rubric=DEFAULT_RUBRIC, cfg=mock_cfg(), name="A", content=CONCRETE)
        second = grade_one(rubric=DEFAULT_RUBRIC, cfg=mock_cfg(), name="B", content=CONCRETE)
        self.assertEqual(first.total, second.total)

    def test_mock_mode_rewards_richer_content(self):
        poor = grade_one(rubric=DEFAULT_RUBRIC, cfg=mock_cfg(), name="A", content="还行。")
        rich = grade_one(rubric=DEFAULT_RUBRIC, cfg=mock_cfg(), name="B", content=CONCRETE)
        self.assertGreater(rich.total, poor.total)


# --------------------------------------------------------------------------- #
# 批量打分
# --------------------------------------------------------------------------- #

class TestGradeBatch(unittest.TestCase):
    def setUp(self):
        self.submissions = [
            {"name": "张三", "content": CONCRETE, "student_id": "2023001"},
            {"name": "李四", "content": CONCRETE, "student_id": "2023002"},   # 与张三雷同
            {"name": "王五", "content": UNRELATED, "student_id": "2023003"},
        ]

    def test_mock_batch_keeps_order_and_reports_progress(self):
        seen: list[tuple[int, int]] = []
        results = grade_batch(self.submissions, DEFAULT_RUBRIC, mock_cfg(),
                              progress=lambda d, t: seen.append((d, t)))
        self.assertEqual([r.name for r in results], ["张三", "李四", "王五"])
        self.assertEqual(seen[-1], (3, 3))
        self.assertTrue(all(r.source == "mock" for r in results))

    def test_similarity_is_attached_to_results(self):
        results = grade_batch(self.submissions, DEFAULT_RUBRIC, mock_cfg())
        by_name = {r.name: r for r in results}
        self.assertTrue(by_name["张三"].similarity)
        self.assertEqual(by_name["张三"].similarity[0].name, "李四")
        self.assertEqual(by_name["王五"].similarity, [])

    def test_empty_batch(self):
        self.assertEqual(grade_batch([], DEFAULT_RUBRIC, mock_cfg()), [])

    def test_batch_uses_edited_names(self):
        """老师在前端改过的姓名要原样生效（不重新解析）。"""
        edited = [{"name": "张三（已改名）", "content": CONCRETE}]
        results = grade_batch(edited, DEFAULT_RUBRIC, mock_cfg())
        self.assertEqual(results[0].name, "张三（已改名）")

    def test_live_batch_uses_thread_pool(self):
        with patch("core.grader.chat_completion", return_value=model_reply()) as fake:
            results = grade_batch(self.submissions, DEFAULT_RUBRIC, live_cfg())
        self.assertEqual(fake.call_count, 3)
        self.assertTrue(all(r.source == "llm" for r in results))
        self.assertEqual(len(results), 3)


# --------------------------------------------------------------------------- #
# 改规则后换算
# --------------------------------------------------------------------------- #

class TestRecalibrate(unittest.TestCase):
    def test_recalibrate_after_score_change(self):
        results = grade_batch([{"name": "张三", "content": CONCRETE}], DEFAULT_RUBRIC, mock_cfg())
        before = results[0].total

        bigger = DEFAULT_RUBRIC.from_dict(DEFAULT_RUBRIC.to_dict())
        for c in bigger.criteria:
            c.max_score *= 2
        recalibrate(bigger, results)

        self.assertEqual(results[0].max_total, 20)
        self.assertAlmostEqual(results[0].total, before * 2, places=1)

    def test_recalibrate_ignores_removed_criteria(self):
        results = grade_batch([{"name": "张三", "content": CONCRETE}], DEFAULT_RUBRIC, mock_cfg())
        smaller = DEFAULT_RUBRIC.from_dict(DEFAULT_RUBRIC.to_dict())
        smaller.criteria = [c for c in smaller.criteria if c.key != "expression"]
        recalibrate(smaller, results)
        self.assertEqual(results[0].max_total, 8)
        self.assertEqual(len(results[0].criteria), 3)   # 旧数据不丢


if __name__ == "__main__":
    unittest.main(verbosity=2)
