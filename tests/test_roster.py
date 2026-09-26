"""学生名单（群昵称 → 真名/学号）的测试，含与解析器的联动。"""

from __future__ import annotations

import unittest
from pathlib import Path

from core.parser import match_roster, parse_wechat_text
from core.roster import Roster, RosterEntry

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestRosterParsing(unittest.TestCase):
    def test_from_text_with_header(self):
        roster = Roster.from_text(load("roster.csv"))
        self.assertEqual(len(roster), 2)
        self.assertEqual(roster.entries[0].to_dict(),
                         {"name": "张三", "student_id": "2023123456", "aliases": ["赢赢"]})
        self.assertEqual(roster.entries[1].to_dict(),
                         {"name": "李四", "student_id": "2023123457", "aliases": ["logic"]})

    def test_header_column_order_does_not_matter(self):
        roster = Roster.from_text("群昵称,姓名,学号\n赢赢,张三,2023123456")
        self.assertEqual(roster.entries[0].name, "张三")
        self.assertEqual(roster.entries[0].aliases, ["赢赢"])

    def test_tsv_pasted_from_excel(self):
        roster = Roster.from_text("姓名\t学号\t群昵称\n张三\t2023123456\t赢赢")
        self.assertEqual(roster.entries[0].student_id, "2023123456")
        self.assertEqual(roster.entries[0].aliases, ["赢赢"])

    def test_index_column_is_ignored(self):
        roster = Roster.from_text("序号,姓名,学号,群昵称\n1,张三,2023123456,赢赢")
        self.assertEqual(roster.entries[0].name, "张三")

    def test_no_header_assumes_name_first(self):
        roster = Roster.from_text("张三 2023123456 赢赢")
        self.assertEqual(roster.entries[0].to_dict(),
                         {"name": "张三", "student_id": "2023123456", "aliases": ["赢赢"]})

    def test_blank_and_comment_lines_skipped(self):
        roster = Roster.from_text("\n# 计科2201班\n\n张三,2023123456,赢赢\n\n")
        self.assertEqual(len(roster), 1)

    def test_from_mapping(self):
        roster = Roster.from_mapping({"赢赢": "张三", "logic": "李四"})
        self.assertEqual(roster.entries[0].name, "张三")
        self.assertEqual(roster.entries[0].aliases, ["赢赢"])

    def test_from_names_only(self):
        roster = Roster.from_names(["张三", "李四"])
        self.assertEqual([e.name for e in roster], ["张三", "李四"])
        self.assertEqual(roster.entries[0].aliases, [])

    def test_coerce_accepts_all_forms(self):
        cases = [
            load("roster.csv"),
            {"赢赢": "张三"},
            ["张三"],
            Roster.from_names(["张三"]),
        ]
        for case in cases:
            self.assertTrue(Roster.coerce(case), f"{case!r} 应当能转成名单")

    def test_coerce_empty_returns_none(self):
        for empty in (None, "", [], {}, "   \n  ", Roster([])):
            self.assertIsNone(Roster.coerce(empty))


class TestRosterResolve(unittest.TestCase):
    def setUp(self):
        self.roster = Roster.from_text(load("roster.csv"))

    def test_exact_alias_match(self):
        entry, how = self.roster.resolve("赢赢")
        self.assertEqual(entry.name, "张三")
        self.assertEqual(how, "alias")

    def test_exact_name_match(self):
        entry, how = self.roster.resolve("张三")
        self.assertEqual(entry.name, "张三")
        self.assertEqual(how, "name")

    def test_alias_with_wxid_suffix_still_matches(self):
        entry, _ = self.roster.resolve("赢赢(wxid_ab12cd34)")
        self.assertEqual(entry.name, "张三")

    def test_prefix_match_needs_review(self):
        entry, how = self.roster.resolve("赢赢小号")
        self.assertEqual(entry.name, "张三")
        self.assertEqual(how, "guess")

    def test_no_match(self):
        self.assertIsNone(self.roster.resolve("王五"))
        self.assertIsNone(self.roster.resolve(""))
        self.assertIsNone(self.roster.resolve(None))

    def test_keys_include_name_and_alias(self):
        self.assertEqual(self.roster.keys(), {"张三", "赢赢", "李四", "logic"})


class TestRosterIntegration(unittest.TestCase):
    def setUp(self):
        self.roster_text = load("roster.csv")

    def test_real_sample_mapped_to_real_names(self):
        result = parse_wechat_text(load("wechat_mobile_copy.txt"), roster=self.roster_text)
        self.assertEqual([s.name for s in result.submissions], ["张三", "李四"])
        self.assertEqual([s.raw_name for s in result.submissions], ["赢赢", "logic"])
        self.assertEqual([s.student_id for s in result.submissions], ["2023123456", "2023123457"])
        for sub in result.submissions:
            self.assertNotIn("not_in_roster", sub.flags)

    def test_roster_fills_missing_student_id(self):
        result = parse_wechat_text(
            "李四：今天学到了很多新东西，收获很大。",
            roster="姓名,学号\n李四,2023123457",
        )
        self.assertEqual(result.submissions[0].student_id, "2023123457")

    def test_roster_student_id_wins_over_body(self):
        result = parse_wechat_text(
            "李四：我是李四，学号 2023123456。今天学到了很多新东西。",
            roster="姓名,学号\n李四,2023999999",
        )
        self.assertEqual(result.submissions[0].student_id, "2023999999")

    def test_body_student_id_used_when_roster_has_none(self):
        result = parse_wechat_text(
            "李四：我是李四，学号 2023123456。今天学到了很多新东西。",
            roster="姓名\n李四",
        )
        self.assertEqual(result.submissions[0].student_id, "2023123456")

    def test_unmatched_speaker_flagged(self):
        result = parse_wechat_text("王五：今天学到了很多新东西，收获很大。", roster=self.roster_text)
        self.assertEqual(result.submissions[0].name, "王五")   # 保留昵称，不丢人
        self.assertIn("not_in_roster", result.submissions[0].flags)

    def test_fuzzy_match_flagged_for_review(self):
        result = parse_wechat_text("赢赢小号：今天学到了很多新东西，收获很大。", roster=self.roster_text)
        sub = result.submissions[0]
        self.assertEqual(sub.name, "张三")
        self.assertIn("alias_guess", sub.flags)

    def test_strict_names_with_text_roster(self):
        text = "赢赢：今天学到了很多新东西。\n王五：我也学到了很多东西。"
        result = parse_wechat_text(text, roster=self.roster_text, strict_names=True)
        self.assertEqual([s.name for s in result.submissions], ["张三"])

    def test_dict_roster(self):
        result = parse_wechat_text(load("wechat_mobile_copy.txt"), roster={"赢赢": "张三", "logic": "李四"})
        self.assertEqual([s.name for s in result.submissions], ["张三", "李四"])

    def test_match_roster_accepts_all_forms(self):
        # 带昵称映射的三种形式都能把"赢赢"解析成"张三"
        for roster in (self.roster_text, {"赢赢": "张三"}, Roster.from_text("姓名,群昵称\n张三,赢赢")):
            self.assertEqual(match_roster("赢赢", roster), "张三", f"{roster!r}")
        # 只给真名的名单：真名能对上，昵称对不上（没有映射就是没有）
        self.assertEqual(match_roster("张三", ["张三"]), "张三")
        self.assertIsNone(match_roster("赢赢", ["张三"]))
        self.assertIsNone(match_roster("王五", ["张三"]))

    def test_empty_roster_behaves_like_no_roster(self):
        result = parse_wechat_text(load("wechat_mobile_copy.txt"), roster="")
        self.assertEqual([s.name for s in result.submissions], ["赢赢", "logic"])
        self.assertEqual(result.submissions[0].flags, ["merged"])


class TestRosterEntry(unittest.TestCase):
    def test_entry_shape(self):
        entry = RosterEntry(name="张三", student_id="2023123456", aliases=["赢赢"])
        self.assertEqual(entry.to_dict(),
                         {"name": "张三", "student_id": "2023123456", "aliases": ["赢赢"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
