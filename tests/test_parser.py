"""第一步交付物的测试：微信群聊文本解析。

运行方式（不需要安装任何第三方库）::

    python -m unittest discover -s tests -t . -v
    # 如果装了 pytest：pytest tests -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core.parser import (
    extract_submissions,
    looks_like_name,
    match_roster,
    name_key,
    normalize_name,
    parse_messages,
    parse_wechat_text,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def names_of(result) -> list[str]:
    return [s.name for s in result.submissions]


# --------------------------------------------------------------------------- #
# 1. 姓名与文本归一化
# --------------------------------------------------------------------------- #

class TestNormalizeName(unittest.TestCase):
    def test_strip_wxid_suffix(self):
        self.assertEqual(normalize_name("张三(wxid_abc123def)"), "张三")
        self.assertEqual(normalize_name("李四 <wxid_xy12zw>"), "李四")

    def test_strip_emoji_and_blank(self):
        self.assertEqual(normalize_name("  王五😀  "), "王五")
        self.assertEqual(normalize_name("赵敏："), "赵敏")

    def test_keep_group_alias_by_default_drop_on_demand(self):
        self.assertEqual(normalize_name("张三-计科2201"), "张三-计科2201")
        self.assertEqual(normalize_name("张三-计科2201", drop_alias=True), "张三")

    def test_name_key_unifies_same_person(self):
        keys = {
            name_key("张三"),
            name_key("张三(wxid_abc123def)"),
            name_key("张三-计科2201"),
            name_key(" 张三 "),
        }
        self.assertEqual(len(keys), 1, f"应当归一到同一个 key，实际 {keys}")

    def test_name_key_keeps_different_people_apart(self):
        self.assertNotEqual(name_key("张三"), name_key("李四"))

    def test_roster_matching(self):
        roster = ["张三", "李四"]
        self.assertEqual(match_roster("张三-计科2201", roster), "张三")
        self.assertEqual(match_roster("张三(wxid_abc)", roster), "张三")
        self.assertIsNone(match_roster("王五", roster))


class TestLooksLikeName(unittest.TestCase):
    def test_accepts_real_nicknames(self):
        for ok in ["张三", "李四", "王五-计科2201", "LiMing", "小明"]:
            self.assertTrue(looks_like_name(ok), f"{ok} 应该被判定为昵称")

    def test_rejects_body_sentences(self):
        for bad in [
            "我觉得",
            "我的理解是",
            "今天上课很有收获",
            "（1）第一点",
            "2024-05-20",
            "所以我认为这样做是对的",
            "老师，我有问题",
            "@张伟 说得对",
            "1234567890",
            "这段文字没有任何昵称和时间戳。",
            "第一行无昵称。",
        ]:
            self.assertFalse(looks_like_name(bad), f"{bad} 不应该被判定为昵称")


class TestTextNormalize(unittest.TestCase):
    def test_crlf_and_zero_width(self):
        text = "张三：今天学到很多，收获很大。\r\n李\ufeff四：我也学到了很多东西。\r\n"
        msgs, _, _ = parse_messages(text)
        self.assertEqual([m.name for m in msgs], ["张三", "李四"])

    def test_ideographic_space_line_counts_as_blank(self):
        text = "\u3000\n张三：今天学到很多，收获很大。"
        msgs, stats, _ = parse_messages(text)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(stats["unnamed"], 0)


# --------------------------------------------------------------------------- #
# 2. 各种头格式
# --------------------------------------------------------------------------- #

class TestHeaderFormats(unittest.TestCase):
    def test_name_colon_halfwidth_and_fullwidth(self):
        msgs, _, _ = parse_messages("张三: 今天学到很多。\n李四：我也学到了很多。")
        self.assertEqual([(m.name, m.content) for m in msgs],
                         [("张三", "今天学到很多。"), ("李四", "我也学到了很多。")])
        self.assertEqual(msgs[0].fmt, "name_colon")

    def test_header_without_content_takes_next_line(self):
        msgs, _, _ = parse_messages("张三：\n今天学到很多。\n李四：我也学到了很多。")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].content, "今天学到很多。")
        self.assertEqual(msgs[0].fmt, "name_colon")
        self.assertEqual(msgs[1].name, "李四")

    def test_bracket_timestamp(self):
        msgs, _, _ = parse_messages("[2024-05-20 14:30] 张伟：今天理解了三次握手。")
        self.assertEqual(msgs[0].name, "张伟")
        self.assertEqual(msgs[0].timestamp, "2024-05-20 14:30")
        self.assertEqual(msgs[0].fmt, "bracket_ts_name")

    def test_timestamp_before_name(self):
        msgs, _, _ = parse_messages("2024-05-20 14:32 王强：课程让我意识到差距。")
        self.assertEqual((msgs[0].name, msgs[0].timestamp), ("王强", "2024-05-20 14:32"))

    def test_timestamp_before_name_chinese_date(self):
        msgs, _, _ = parse_messages("2024年5月20日 14:33 赵敏：学会了分层思考。")
        self.assertEqual((msgs[0].name, msgs[0].timestamp), ("赵敏", "2024年5月20日 14:33"))

    def test_pc_export_name_then_timestamp(self):
        text = "张伟 2024-05-20 14:30:12\n第一行正文。\n第二行正文。\n李娜 2024-05-21 09:12:00\n她的正文。"
        msgs, _, _ = parse_messages(text)
        self.assertEqual([m.name for m in msgs], ["张伟", "李娜"])
        self.assertEqual(msgs[0].content, "第一行正文。\n第二行正文。")
        self.assertEqual(msgs[0].fmt, "name_ts")

    def test_bare_name_block_separated_by_blank_line(self):
        text = "张三\n\n今天收获很大。\n\n李四\n\n我也很有收获。"
        msgs, _, _ = parse_messages(text)
        self.assertEqual([(m.name, m.fmt) for m in msgs],
                         [("张三", "bare_name"), ("李四", "bare_name")])
        self.assertEqual(msgs[0].content, "今天收获很大。")

    def test_multiline_body_merged_into_one_message(self):
        text = "张三：第一句。\n第二句。\n第三句。\n李四：他的第一句。"
        msgs, _, _ = parse_messages(text)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].content.splitlines(), ["第一句。", "第二句。", "第三句。"])

    def test_keep_raw_keeps_original_block(self):
        msgs, _, _ = parse_messages("张三：第一句。\n第二句。", keep_raw=True)
        self.assertIn("张三：第一句。", msgs[0].raw_block)
        self.assertIn("第二句。", msgs[0].raw_block)

    def test_line_no_points_back_to_source(self):
        msgs, _, _ = parse_messages("第一行无昵称。\n张三：第二行。")
        self.assertEqual(msgs[1].line_no, 2)


# --------------------------------------------------------------------------- #
# 2b. 微信手机端"复制多条消息"：昵称行 → 时间行 → 正文行
# --------------------------------------------------------------------------- #

class TestWechatMobileCopy(unittest.TestCase):
    def test_standalone_timestamp_attaches_to_previous_header(self):
        msgs, _, _ = parse_messages("张三\n2026年09月26日 20:15\n今天学到很多，收获很大。")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].name, "张三")
        self.assertEqual(msgs[0].timestamp, "2026年09月26日 20:15")
        self.assertEqual(msgs[0].content, "今天学到很多，收获很大。")

    def test_standalone_timestamp_variants(self):
        for ts in ["2024-05-20 14:30:12", "2024/5/20 14:30", "2024.05.20 14:30", "2024年5月20日"]:
            msgs, _, _ = parse_messages(f"张三\n{ts}\n今天学到很多，收获很大。")
            self.assertEqual(msgs[0].timestamp, ts, f"{ts} 应当被识别为时间戳")
            self.assertEqual(msgs[0].content, "今天学到很多，收获很大。", f"{ts} 不应污染正文")

    def test_two_messages_same_speaker_with_timestamps(self):
        text = "李四\n2026年09月26日 20:20\n第一段心得，学到了很多。\n\n李四\n2026年09月26日 20:21\n第二段心得，想再补充一点。"
        result = parse_wechat_text(text)
        self.assertEqual(len(result.messages), 2)
        self.assertEqual([m.timestamp for m in result.messages],
                         ["2026年09月26日 20:20", "2026年09月26日 20:21"])
        self.assertEqual(len(result.submissions), 1)
        self.assertEqual(result.submissions[0].message_count, 2)

    def test_real_world_group_sample(self):
        """真实群里复制的原样样本（含口语、英文昵称、连续同一人发言）。"""
        result = parse_wechat_text(load("wechat_mobile_copy.txt"))
        self.assertEqual(len(result.messages), 13)
        self.assertEqual(names_of(result), ["赢赢", "logic"])
        self.assertTrue(all(m.timestamp for m in result.messages))
        self.assertEqual(result.messages[0].content, "因为我想着文科，然后我当时就买平板了")
        self.assertEqual(result.messages[0].timestamp, "2026年09月26日 13:53")
        self.assertEqual(result.messages[5].content,
                         "不过api调用是个问题，万一这老师天天用这个乐子工具就是烧的我的钱")

        ying = next(s for s in result.submissions if s.name == "赢赢")
        logic = next(s for s in result.submissions if s.name == "logic")
        self.assertEqual(ying.message_count, 8)
        self.assertEqual(logic.message_count, 5)
        for sub in result.submissions:
            self.assertNotIn("2026年09月26日", sub.content)   # 时间行不能污染正文

    def test_essay_sample_in_mobile_format(self):
        result = parse_wechat_text(load("wechat_mobile_copy_essay.txt"))
        self.assertEqual(names_of(result), ["张三", "李四", "王五"])
        self.assertEqual(len(result.messages), 5)

        zhang = next(s for s in result.submissions if s.name == "张三")
        self.assertEqual(zhang.message_count, 2)
        self.assertIn("补充一点：可靠性之外", zhang.content)

        li = next(s for s in result.submissions if s.name == "李四")
        self.assertEqual(li.student_id, "2023123456")
        # 李四另外还发了一张图，但他交了文字，不该因此被告警
        self.assertEqual(li.flags, ["merged"])

    def test_timestamp_line_is_not_treated_as_body_when_speaker_unknown(self):
        msgs, _, _ = parse_messages("2026年09月26日 20:15\n今天学到很多，收获很大。")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].fmt, "unnamed")
        self.assertIn("no_speaker", msgs[0].flags)


# --------------------------------------------------------------------------- #
# 3. 噪声过滤
# --------------------------------------------------------------------------- #

class TestNoiseFiltering(unittest.TestCase):
    def test_system_lines_dropped(self):
        text = (
            '"王五"撤回了一条消息\n'
            "你邀请赵六加入了群聊\n"
            "张三 拍了拍 李四\n"
            "张三：今天学到很多，收获很大。\n"
        )
        msgs, stats, _ = parse_messages(text)
        self.assertEqual([m.name for m in msgs], ["张三"])
        self.assertEqual(stats["system_dropped"], 3)

    def test_system_word_inside_long_text_is_kept(self):
        long_text = (
            "张三：老师在课上提到，平台上有人撤回了一条消息之后就再也找不到记录，"
            "这件事让我意识到可追溯性对协作系统有多重要，也让我重新思考了日志设计的意义。"
        )
        msgs, stats, _ = parse_messages(long_text)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(stats["system_dropped"], 0)

    def test_standalone_placeholder_line_dropped(self):
        text = "张三：今天学到很多，收获很大。\n[图片]\n[动画表情]\n李四：我也学到了很多。"
        msgs, stats, _ = parse_messages(text)
        self.assertEqual([m.name for m in msgs], ["张三", "李四"])
        self.assertEqual(stats["placeholder_dropped"], 2)
        self.assertNotIn("[图片]", msgs[0].content)

    def test_placeholder_only_message_flagged_not_graded_as_text(self):
        result = parse_wechat_text("王强：[图片]\n张三：今天学到很多，收获很大。")
        wang = next(s for s in result.submissions if s.name == "王强")
        self.assertIn("placeholder", wang.flags)
        self.assertIn("no_content", wang.flags)
        self.assertEqual(wang.content, "")

    def test_body_can_keep_inline_placeholder_removed(self):
        result = parse_wechat_text("张三：[图片]今天学到很多，收获很大，尤其是日志设计。")
        self.assertEqual(result.submissions[0].content, "今天学到很多，收获很大，尤其是日志设计。")


# --------------------------------------------------------------------------- #
# 4. 聚合成待评阅心得
# --------------------------------------------------------------------------- #

class TestExtractSubmissions(unittest.TestCase):
    def test_same_student_multiple_messages_merged(self):
        text = "张三：第一段心得，学到了很多。\n张三：第二段心得，还想再补充一点。"
        result = parse_wechat_text(text)
        self.assertEqual(len(result.submissions), 1)
        sub = result.submissions[0]
        self.assertEqual(sub.message_count, 2)
        self.assertIn("merged", sub.flags)
        self.assertIn("第一段心得", sub.content)
        self.assertIn("第二段心得", sub.content)

    def test_resubmission_keeps_last_version(self):
        result = parse_wechat_text(load("resubmit.txt"))
        self.assertEqual(len(result.submissions), 1)
        sub = result.submissions[0]
        self.assertEqual(sub.name, "陈磊")
        self.assertEqual(sub.message_count, 2)
        self.assertIn("resubmitted", sub.flags)
        self.assertIn("写入开销", sub.content)          # 保留的是改过的那一版
        self.assertEqual(sub.content.count("我认识到索引"), 1)  # 没有把两版拼在一起

    def test_student_id_extracted_from_body(self):
        result = parse_wechat_text("李四：我是李四，学号 2023123456。课程让我收获很大。")
        self.assertEqual(result.submissions[0].student_id, "2023123456")

    def test_student_id_from_leading_pair(self):
        result = parse_wechat_text("张三：2023123456 张三，这节课我学到了分层设计。")
        self.assertEqual(result.submissions[0].student_id, "2023123456")

    def test_unnamed_message_excluded_by_default_and_flagged_when_included(self):
        text = "同学们好，本次心得请在今晚 24 点前提交，字数不少于 50 字。\n张三：收到，我的心得是这节课让我理解了分层设计的重要性。"
        result = parse_wechat_text(text)
        self.assertEqual(names_of(result), ["张三"])
        self.assertEqual(result.stats["unnamed"], 1)

        result2 = parse_wechat_text(text, include_unnamed=True)
        self.assertEqual(len(result2.submissions), 2)
        ghost = result2.submissions[0]
        self.assertEqual(ghost.name, "（未识别）")
        self.assertIn("no_speaker", ghost.flags)

    def test_exclude_names_filters_teacher(self):
        text = "老师：本次心得要求至少 200 字。\n张三：老师好，我的心得是这节课让我理解了分层设计的重要性。"
        result = parse_wechat_text(text, exclude_names=["老师"])
        self.assertEqual(names_of(result), ["张三"])

    def test_short_submission_flagged(self):
        result = parse_wechat_text("张三：太短了。")
        self.assertIn("too_short", result.submissions[0].flags)

    def test_min_chars_is_configurable(self):
        result = parse_wechat_text("张三：太短了。", min_chars=2)
        self.assertNotIn("too_short", result.submissions[0].flags)

    def test_roster_gives_canonical_name(self):
        text = "张三-计科2201：这节课我理解了分层设计的重要性，收获很大。"
        result = parse_wechat_text(text, known_names=["张三", "李四"])
        sub = result.submissions[0]
        self.assertEqual(sub.name, "张三")                 # 用名单里的正式写法
        self.assertEqual(sub.raw_name, "张三-计科2201")     # 原昵称保留给老师核对

    def test_strict_names_demotes_unknown_speaker_to_body(self):
        text = "张三：今天收获很大，学到了很多新东西。\n王五：我也学到了很多东西，很有收获。"
        loose = parse_wechat_text(text)
        self.assertEqual(names_of(loose), ["张三", "王五"])

        strict = parse_wechat_text(text, known_names=["张三", "李四"], strict_names=True)
        self.assertEqual(names_of(strict), ["张三"])
        self.assertIn("王五：我也学到了很多东西", strict.submissions[0].content)

    def test_extract_submissions_accepts_message_list(self):
        msgs, _, _ = parse_messages("张三：今天学到很多，收获很大。")
        subs = extract_submissions(msgs, min_chars=1)
        self.assertEqual([s.name for s in subs], ["张三"])


# --------------------------------------------------------------------------- #
# 5. 端到端：真实粘贴样本
# --------------------------------------------------------------------------- #

class TestRealisticFixtures(unittest.TestCase):
    def test_paste_colon(self):
        result = parse_wechat_text(load("paste_colon.txt"))
        self.assertEqual(len(result.messages), 6)
        self.assertEqual(names_of(result), ["张伟", "李娜", "王强", "赵敏", "周涛"])

        li = next(s for s in result.submissions if s.name == "李娜")
        self.assertEqual(li.message_count, 2)
        self.assertEqual(li.student_id, "2023123456")

        # 正文里带冒号的句子不能被当成新发言
        zhou = next(s for s in result.submissions if s.name == "周涛")
        self.assertIn("补充一点：可靠性之外", zhou.content)
        self.assertNotIn("我觉得", names_of(result))

    def test_pc_export(self):
        result = parse_wechat_text(load("pc_export.txt"))
        self.assertEqual([m.name for m in result.messages], ["张伟", "李娜", "李娜"])
        self.assertEqual(names_of(result), ["张伟", "李娜"])
        self.assertEqual(result.messages[0].content, "今天的课让我对 TCP 三次握手有了新的认识。\n以前只知道背概念，现在理解了为什么需要三次而不是两次。")

    def test_block_blank(self):
        result = parse_wechat_text(load("block_blank.txt"))
        self.assertEqual(names_of(result), ["张三", "李四"])
        self.assertEqual(result.stats["system_dropped"], 2)
        self.assertEqual(result.stats["placeholder_dropped"], 1)
        for sub in result.submissions:
            self.assertNotIn("撤回", sub.content)
            self.assertNotIn("群聊", sub.content)

    def test_bracket_ts(self):
        result = parse_wechat_text(load("bracket_ts.txt"))
        self.assertEqual(names_of(result), ["张伟", "李娜", "王强", "赵敏"])
        self.assertEqual([m.timestamp for m in result.messages],
                         ["2024-05-20 14:30", "2024-05-20 14:31",
                          "2024-05-20 14:32", "2024年5月20日 14:33"])
        self.assertEqual(result.messages[1].content, "三权分立的核心在于制衡。")

    def test_messy(self):
        result = parse_wechat_text(load("messy.txt"))
        self.assertEqual(len(result.messages), 5)
        self.assertEqual(result.stats["unnamed"], 1)
        self.assertEqual(result.stats["placeholder_dropped"], 1)
        self.assertEqual(names_of(result), ["张三", "李四", "王五"])

        # "我觉得：……" 是正文，不是昵称
        zhang = next(s for s in result.submissions if s.name == "张三")
        self.assertIn("我觉得：可靠性是设计的第一目标。", zhang.content)

        # "（1）第一点：……" 也是正文
        li = next(s for s in result.submissions if s.name == "李四")
        self.assertIn("（1）第一点：分层设计", li.content)
        self.assertEqual(li.student_id, "2023123456")
        self.assertEqual(li.message_count, 2)


# --------------------------------------------------------------------------- #
# 6. 边界与健壮性
# --------------------------------------------------------------------------- #

class TestEdgeCases(unittest.TestCase):
    def test_none_and_empty_input(self):
        for text in (None, "", "   ", "\n\n\n", "\u3000\u3000"):
            result = parse_wechat_text(text)
            self.assertEqual(result.messages, [])
            self.assertEqual(result.submissions, [])
            self.assertEqual(result.stats["messages"], 0)

    def test_no_header_at_all_becomes_single_unnamed_message(self):
        msgs, stats, warnings = parse_messages("这段文字没有任何昵称和时间戳。")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].fmt, "unnamed")
        self.assertIn("no_speaker", msgs[0].flags)
        self.assertTrue(warnings)

    def test_warning_when_nothing_parsed(self):
        result = parse_wechat_text("纯文字但没有昵称。")
        self.assertTrue(result.warnings)

    def test_huge_input_is_linear_and_ok(self):
        text = "\n".join(f"学生{i}：这是第 {i} 位同学的心得正文，内容足够长以便通过最小字数校验。"
                         for i in range(300))
        result = parse_wechat_text(text)
        self.assertEqual(len(result.submissions), 300)

    def test_to_dict_shapes(self):
        result = parse_wechat_text("张三：今天学到很多，收获很大。")
        m = result.messages[0].to_dict()
        s = result.submissions[0].to_dict()
        for key in ("index", "name", "content", "fmt", "flags", "line_no", "timestamp"):
            self.assertIn(key, m)
        for key in ("name", "content", "student_id", "message_count", "char_count", "flags"):
            self.assertIn(key, s)
        self.assertEqual(s["char_count"], 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
