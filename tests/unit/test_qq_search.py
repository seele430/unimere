"""QQ 问答联网检索注入与行为准则测试：全 mock，不联网。"""

import os
import unittest

from unittest.mock import patch

from src.qq.inbound import QQ_ASK_RULES, QQInbound
from src.qq.search import SearchError, needs_search


class FakeClient:
    def send_private_msg(self, user_id, text, **kwargs):
        return {"message_id": 1}


class FakeProvider:
    configured = True
    model = "test-model"

    def __init__(self):
        self.prompts = []
        self.response = "答案"

    def submit(self, prompt, *, artifact_name, out_dir):
        from pathlib import Path

        self.prompts.append(prompt)
        (Path(out_dir) / artifact_name).write_text(self.response, encoding="utf-8")

        return {"task_id": "t"}


class NeedsSearchCase(unittest.TestCase):
    def test_time_sensitive_keywords(self):
        self.assertTrue(needs_search("雷峰塔门票多少钱"))
        self.assertTrue(needs_search("杭州明天天气如何"))

    def test_explicit_realtime_keywords(self):
        for keyword in ("联网", "实时", "今天", "今日", "当前", "近期", "最近"):
            with self.subTest(keyword=keyword):
                self.assertTrue(needs_search(f"请{keyword}查一下成都情况"))

    def test_relative_date_ranges(self):
        for question in (
            "查一下近七天的哈尔滨天气",
            "浙江省近七年高考分数线",
            "未来 7 天天气",
            "过去两周的汇率",
            "近几年旅游数据",
        ):
            with self.subTest(question=question):
                self.assertTrue(needs_search(question))

    def test_plain_question_does_not_need_search(self):
        self.assertFalse(needs_search("1+1 等于几"))


class AskResearchCase(unittest.TestCase):
    def setUp(self):
        self.handler = QQInbound(owner_id=7, secret="s", client=FakeClient())
        self.provider = FakeProvider()

    def ask(self, question):
        with patch(
            "src.collab.providers.build_provider", return_value=self.provider
        ), patch("src.collab.providers.provider_name", return_value="ark"):
            return self.handler.reply_for("ask", question)

    def test_research_injected_before_question(self):
        with patch.dict(os.environ, {"PA_SEARCH_API_KEY": "k"}, clear=False):
            with patch(
                "src.qq.search.search",
                return_value="[检索资料]雷峰塔门票 40 元｜来源：example.com",
            ) as mocked:
                answer = self.ask("雷峰塔门票多少钱")

        self.assertEqual(answer, "答案")
        mocked.assert_called_once()
        prompt = self.provider.prompts[0]
        self.assertIn("雷峰塔门票 40 元", prompt)
        self.assertLess(prompt.index("雷峰塔门票 40 元"), prompt.index("用户消息："))

    def test_no_key_has_visible_notice_and_does_not_call_search(self):
        with patch.dict(os.environ, {"PA_SEARCH_API_KEY": ""}, clear=False):
            with patch("src.qq.search.search") as mocked:
                answer = self.ask("联网查询雷峰塔门票多少钱")

        self.assertTrue(answer.startswith("实时检索未启用"))
        self.assertIn("PA_SEARCH_API_KEY", answer)
        self.assertTrue(answer.endswith("答案"))
        self.assertIn("正文不要重复检索状态", self.provider.prompts[0])
        mocked.assert_not_called()

    def test_capability_answer_reflects_missing_key_without_model(self):
        with patch.dict(os.environ, {"PA_SEARCH_API_KEY": ""}, clear=False):
            answer = self.ask("你不能联网吗")

        self.assertIn("当前未启用实时检索", answer)
        self.assertNotIn("能联网，但", answer)
        self.assertEqual(self.provider.prompts, [])

    def test_capability_answer_reflects_enabled_search(self):
        with patch.dict(os.environ, {"PA_SEARCH_API_KEY": "k"}, clear=False):
            answer = self.ask("你能联网吗")

        self.assertIn("实时检索已启用", answer)
        self.assertEqual(self.provider.prompts, [])

    def test_repeated_notice_is_removed(self):
        notice = "实时检索未启用：未配置 PA_SEARCH_API_KEY。"
        self.provider.response = notice + "\n答案"

        with patch.dict(os.environ, {"PA_SEARCH_API_KEY": ""}, clear=False):
            answer = self.ask("成都今天天气")

        self.assertEqual(answer, notice + "\n答案")
        self.assertEqual(answer.count(notice), 1)

    def test_non_time_sensitive_question_skips_search(self):
        with patch.dict(os.environ, {"PA_SEARCH_API_KEY": "k"}, clear=False):
            with patch("src.qq.search.search") as mocked:
                answer = self.ask("1+1 等于几")

        self.assertEqual(answer, "答案")
        mocked.assert_not_called()

    def test_search_failure_has_visible_notice_and_does_not_block_answer(self):
        with patch.dict(os.environ, {"PA_SEARCH_API_KEY": "k"}, clear=False):
            with patch(
                "src.qq.search.search", side_effect=SearchError("连不上检索服务")
            ):
                answer = self.ask("雷峰塔门票多少钱")

        self.assertTrue(answer.startswith("实时检索失败：连不上检索服务"))
        self.assertTrue(answer.endswith("答案"))

    def test_empty_search_result_has_visible_notice(self):
        with patch.dict(os.environ, {"PA_SEARCH_API_KEY": "k"}, clear=False):
            with patch("src.qq.search.search", return_value=""):
                answer = self.ask("成都今天天气")

        self.assertTrue(answer.startswith("实时检索未返回可用资料"))
        self.assertTrue(answer.endswith("答案"))


class AskRulesCase(unittest.TestCase):
    """截图暴露的行为问题必须被提示词明确禁止，防止回滚。"""

    def test_rules_forbid_claiming_no_image_capability(self):
        self.assertIn("不得声称自己不能发图片", QQ_ASK_RULES)

    def test_rules_forbid_rhetorical_confirmation(self):
        self.assertIn("等反问收尾", QQ_ASK_RULES)

    def test_rules_limit_disclaimer_to_once(self):
        self.assertIn("全文最多说一次", QQ_ASK_RULES)

    def test_rules_allow_long_form_guides(self):
        self.assertIn("不受 350 字限制", QQ_ASK_RULES)


if __name__ == "__main__":
    unittest.main()
