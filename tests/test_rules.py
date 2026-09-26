"""评分规则（core/rules.py）的测试。"""

from __future__ import annotations

import json
import unittest

from core.rules import DEFAULT_RUBRIC, Criterion, Level, Rubric


class TestDefaultRubric(unittest.TestCase):
    def test_default_shape(self):
        self.assertEqual(DEFAULT_RUBRIC.total, 10)
        self.assertEqual([c.name for c in DEFAULT_RUBRIC.criteria],
                         ["观点理解", "个人思考", "文字表达"])
        self.assertEqual([c.max_score for c in DEFAULT_RUBRIC.criteria], [5, 3, 2])

    def test_default_is_valid(self):
        self.assertEqual(DEFAULT_RUBRIC.validate(), [])

    def test_level_scores_match_designed_values(self):
        understanding = DEFAULT_RUBRIC.find("understanding")
        self.assertEqual(understanding.level_score(1.0), 5)
        self.assertEqual(understanding.level_score(0.7), 3.5)
        self.assertEqual(understanding.level_score(0.4), 2)
        thinking = DEFAULT_RUBRIC.find("thinking")
        self.assertEqual(thinking.level_score(0.7), 2.1)
        expression = DEFAULT_RUBRIC.find("expression")
        self.assertEqual(expression.level_score(0.7), 1.4)


class TestPromptBlock(unittest.TestCase):
    def test_prompt_block_carries_rules_to_the_model(self):
        block = DEFAULT_RUBRIC.prompt_block()
        self.assertIn("观点理解（满分 5 分）", block)
        self.assertIn("个人思考（满分 3 分）", block)
        self.assertIn("文字表达（满分 2 分）", block)
        self.assertIn("总分 10 分", block)
        self.assertIn("优秀（5 分）", block)          # 档位要带实际分值
        self.assertIn("良好（3.5 分）", block)
        self.assertIn("课堂心得", block)              # 课程背景要进 Prompt

    def test_prompt_block_mentions_ai_policy(self):
        rubric = Rubric.from_dict(DEFAULT_RUBRIC.to_dict())
        rubric.ai_policy = "forbidden"
        self.assertIn("禁止使用 AI 代写", rubric.prompt_block())
        rubric.ai_policy = "allowed"
        self.assertIn("允许使用 AI 辅助", rubric.prompt_block())

    def test_prompt_block_includes_extra_instructions(self):
        rubric = Rubric.from_dict(DEFAULT_RUBRIC.to_dict())
        rubric.extra_instructions = "本次心得必须提到课堂上的实验环节。"
        self.assertIn("本次心得必须提到课堂上的实验环节。", rubric.prompt_block())

    def test_numbers_are_rendered_without_trailing_zero(self):
        self.assertIn("满分 5 分", DEFAULT_RUBRIC.prompt_block())
        self.assertNotIn("满分 5.0 分", DEFAULT_RUBRIC.prompt_block())


class TestSerialization(unittest.TestCase):
    def test_dict_round_trip(self):
        data = DEFAULT_RUBRIC.to_dict()
        again = Rubric.from_dict(data)
        self.assertEqual(again.to_dict(), data)

    def test_json_round_trip(self):
        text = DEFAULT_RUBRIC.to_json()
        again = Rubric.from_json(text)
        self.assertEqual(again.total, 10)
        self.assertEqual([c.key for c in again.criteria],
                         [c.key for c in DEFAULT_RUBRIC.criteria])

    def test_json_keeps_chinese_readable(self):
        self.assertIn("观点理解", DEFAULT_RUBRIC.to_json())

    def test_empty_criteria_falls_back_to_default(self):
        rubric = Rubric.from_dict({"name": "空规则", "criteria": []})
        self.assertEqual(rubric.total, 10)

    def test_missing_keys_get_generated(self):
        rubric = Rubric.from_dict({
            "name": "自定义",
            "criteria": [{"name": "课堂参与", "max_score": 4}],
        })
        self.assertTrue(rubric.criteria[0].key)

    def test_levels_sorted_desc(self):
        rubric = Rubric.from_dict({
            "criteria": [{
                "key": "a", "name": "A", "max_score": 10,
                "levels": [
                    {"label": "低", "ratio": 0.3},
                    {"label": "高", "ratio": 1.0},
                    {"label": "中", "ratio": 0.6},
                ],
            }],
        })
        self.assertEqual([lv.label for lv in rubric.criteria[0].levels], ["高", "中", "低"])


class TestValidation(unittest.TestCase):
    def test_rejects_empty_criteria(self):
        rubric = Rubric(name="空", criteria=[])
        self.assertTrue(any("至少要有一个" in e for e in rubric.validate()))

    def test_rejects_duplicate_keys(self):
        rubric = Rubric(criteria=[
            Criterion("same", "维度一", 5),
            Criterion("same", "维度二", 5),
        ])
        self.assertTrue(any("重复" in e for e in rubric.validate()))

    def test_rejects_non_positive_max_score(self):
        rubric = Rubric(criteria=[Criterion("a", "维度", 0)])
        errors = rubric.validate()
        self.assertTrue(any("满分必须大于 0" in e for e in errors))
        self.assertTrue(any("总分必须大于 0" in e for e in errors))

    def test_rejects_bad_ratio(self):
        criterion = Criterion("a", "维度", 5, levels=[Level("坏档", 1.5)])
        rubric = Rubric(criteria=[criterion])
        self.assertTrue(any("比例必须在 0~1" in e for e in rubric.validate()))

    def test_rejects_blank_name(self):
        rubric = Rubric(name="   ", criteria=[Criterion("a", "维度", 5)])
        self.assertTrue(any("规则名称不能为空" in e for e in rubric.validate()))


class TestScoreHelpers(unittest.TestCase):
    def test_clamp_score_bounds(self):
        self.assertEqual(DEFAULT_RUBRIC.clamp_score("understanding", 999), 5)
        self.assertEqual(DEFAULT_RUBRIC.clamp_score("understanding", -3), 0)
        self.assertEqual(DEFAULT_RUBRIC.clamp_score("understanding", "4.25"), 4.25)
        self.assertEqual(DEFAULT_RUBRIC.clamp_score("understanding", "abc"), 0)

    def test_clamp_unknown_key_falls_back_to_plain_number(self):
        self.assertEqual(DEFAULT_RUBRIC.clamp_score("nope", 3.7), 3.7)

    def test_ratio_of(self):
        self.assertEqual(DEFAULT_RUBRIC.ratio_of("understanding", 2.5), 0.5)
        self.assertEqual(DEFAULT_RUBRIC.ratio_of("understanding", 5), 1.0)

    def test_band_of(self):
        # 档位锚点：优秀=5(100%)、良好=3.5(70%)、待改进=2(40%)
        # 分界取相邻锚点中点：0.85 和 0.55
        self.assertEqual(DEFAULT_RUBRIC.band_of("understanding", 5), "优秀")
        self.assertEqual(DEFAULT_RUBRIC.band_of("understanding", 4.5), "优秀")
        self.assertEqual(DEFAULT_RUBRIC.band_of("understanding", 4.3), "优秀")
        self.assertEqual(DEFAULT_RUBRIC.band_of("understanding", 4), "良好")
        self.assertEqual(DEFAULT_RUBRIC.band_of("understanding", 3.5), "良好")
        self.assertEqual(DEFAULT_RUBRIC.band_of("understanding", 3), "良好")
        self.assertEqual(DEFAULT_RUBRIC.band_of("understanding", 2.5), "待改进")
        self.assertEqual(DEFAULT_RUBRIC.band_of("understanding", 2), "待改进")
        self.assertEqual(DEFAULT_RUBRIC.band_of("understanding", 0), "待改进")

    def test_band_of_single_level(self):
        rubric = Rubric(criteria=[Criterion("a", "维度", 5, levels=[Level("合格", 1.0)])])
        self.assertEqual(rubric.band_of("a", 5), "合格")
        self.assertEqual(rubric.band_of("a", 0), "合格")

    def test_band_of_without_levels(self):
        rubric = Rubric(criteria=[Criterion("a", "维度", 5)])
        self.assertEqual(rubric.band_of("a", 3), "")

    def test_total_updates_with_criteria(self):
        rubric = Rubric.from_dict(DEFAULT_RUBRIC.to_dict())
        rubric.criteria[0].max_score = 20
        self.assertEqual(rubric.total, 25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
