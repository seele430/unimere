"""长期记忆自动捕获、主动注入与聊天故障隔离。"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src import agent
from src.agent_stream import _initial_messages
from src.core.reminder_identity import bind_authenticated_reminder
from src.memory import engine, service
from src.memory.store import MemoryStore


class MemoryEngineCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MemoryStore(Path(self.temporary.name) / "memory.db")
        self.store_patch = patch(
            "src.memory.service.get_memory_store", return_value=self.store
        )
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)

    def test_captures_only_explicit_preferences_constraints_and_facts(self):
        saved = engine.capture_user_message(
            "u1",
            "叫我 Jessica。我喜欢喝红茶。以后请简洁回答。项目使用 PostgreSQL。今天天气怎么样",
            source_ref="chat:req-1",
        )
        self.assertEqual(len(saved), 4)
        self.assertEqual(
            {(item.kind, item.layer) for item in saved},
            {
                ("preference", "identity"),
                ("constraint", "identity"),
                ("project_fact", "project"),
            },
        )
        self.assertTrue(all(item.trust == "user_confirmed" for item in saved))
        self.assertTrue(all(item.source_ref == "chat:req-1" for item in saved))
        self.assertEqual(self.store.count_items("u1"), 4)

        engine.capture_user_message("u1", "今天需要写一份周报")
        self.assertEqual(self.store.count_items("u1"), 4)

    def test_remembered_contextual_preference_is_stable_identity(self):
        saved = engine.capture_user_message(
            "u1", "请记住：我在记忆实测中偏好使用项目符号。"
        )
        self.assertEqual(len(saved), 1)
        self.assertEqual((saved[0].kind, saved[0].layer), ("preference", "identity"))
        context = engine.build_context("u1", "安排一个 unrelated task")
        self.assertIn("我在记忆实测中偏好使用项目符号", context)

    def test_questions_and_do_not_remember_are_not_captured(self):
        for text in ("我喜欢什么？", "不要记住我喜欢红茶", "你能记住我喜欢红茶吗"): 
            engine.capture_user_message("u1", text)
        self.assertEqual(self.store.count_items("u1"), 0)

    def test_duplicate_does_not_create_version(self):
        first = engine.capture_user_message("u1", "我喜欢绿茶")
        second = engine.capture_user_message("u1", "我喜欢绿茶")
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(self.store.count_items("u1"), 1)

    def test_changed_preference_supersedes_old_value(self):
        old = engine.capture_user_message("u1", "我喜欢绿茶")[0]
        new = engine.capture_user_message("u1", "我现在喜欢红茶")[0]
        self.assertEqual(new.key, "preference.drink")
        self.assertEqual(new.supersedes_id, old.id)
        listing = service.list_current("u1")
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["id"], new.id)
        self.assertIn("红茶", listing["items"][0]["text"])

    def test_stable_preference_injected_without_keyword(self):
        engine.capture_user_message("u1", "我喜欢绿茶")
        context = engine.build_context("u1", "帮我安排今天的工作")
        self.assertIn("我喜欢绿茶", context)
        self.assertIn("当前用户消息中的明确要求优先于旧记忆", context)

    def test_edited_preference_is_the_only_value_in_new_chat(self):
        old = engine.capture_user_message("u1", "我喜欢绿茶")[0]
        new = service.update_text("u1", old.id, "我现在喜欢乌龙茶")
        context = engine.build_context("u1", "帮我安排今天的工作")
        self.assertEqual(new.supersedes_id, old.id)
        self.assertIn("乌龙茶", context)
        self.assertNotIn("绿茶", context)
        history = service.history("u1", new.id)
        self.assertEqual([row["text"] for row in history["items"]], ["我喜欢绿茶", "我现在喜欢乌龙茶"])

    def test_deleted_memory_is_not_injected_into_new_chat(self):
        old = engine.capture_user_message("u1", "我喜欢绿茶")[0]
        new = service.update_text("u1", old.id, "我现在喜欢乌龙茶")
        self.assertTrue(service.delete("u1", new.id))
        self.assertIsNone(engine.build_context("u1", "帮我安排今天的工作"))
        self.assertEqual(service.list_current("u1")["total"], 0)

    def test_unrelated_project_fact_is_not_injected(self):
        service.remember(
            "u1", "项目数据库使用 PostgreSQL", kind="project_fact", layer="project"
        )
        self.assertIsNone(engine.build_context("u1", "今天天气怎么样"))

    def test_project_fact_recalled_but_episode_not_duplicated(self):
        service.remember(
            "u1", "项目数据库使用 PostgreSQL", kind="project_fact", layer="project"
        )
        service.remember(
            "u1", "上次运行已经完成", kind="episode", layer="project",
            trust="platform_verified",
        )
        context = engine.build_context("u1", "项目数据库使用什么")
        self.assertIn("PostgreSQL", context)
        self.assertNotIn("上次运行已经完成", context)

    def test_no_memory_means_no_context(self):
        self.assertIsNone(engine.build_context("u1", "普通问题"))

    def test_owner_isolation_for_capture_and_recall(self):
        engine.capture_user_message("u1", "我喜欢绿茶")
        self.assertIsNone(engine.build_context("u2", "我喜欢什么"))
        engine.capture_user_message("u2", "我喜欢绿茶")
        self.assertEqual(self.store.count_items("u1"), 1)
        self.assertEqual(self.store.count_items("u2"), 1)

    def test_context_respects_character_budget(self):
        for index in range(6):
            service.remember(
                "u1", f"以后请保持回答风格第{index}条" + "约束" * 20,
                kind="constraint", layer="identity",
            )
        context = engine.build_context("u1", "随便聊聊", max_chars=240)
        self.assertIsNotNone(context)
        self.assertLessEqual(len(context), 240)

    def test_capture_failure_does_not_block_chat_context(self):
        identity = bind_authenticated_reminder(
            user_id="u1", request_id="req-memory-error", role="owner"
        )
        with identity, patch(
            "src.memory.engine.capture_user_message", side_effect=RuntimeError("boom")
        ), patch(
            "src.memory.engine.build_context", return_value=None
        ):
            self.assertIsNone(agent._memory_context("以后请简洁回答"))


class MemoryChatIntegrationCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MemoryStore(Path(self.temporary.name) / "memory.db")
        self.store_patch = patch(
            "src.memory.service.get_memory_store", return_value=self.store
        )
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.identity = bind_authenticated_reminder(
            user_id="alice", request_id="memory-request", role="owner"
        )
        self.identity.__enter__()
        self.addCleanup(self.identity.__exit__, None, None, None)

    def test_initial_messages_capture_then_recall_in_later_chat(self):
        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(agent, "_skill_context", return_value=None),
            patch.object(agent, "_work_memory_context", return_value=None),
        ):
            first, _ = _initial_messages("我喜欢绿茶", None)
            second, _ = _initial_messages("帮我规划今天", None)

        self.assertIn("我喜欢绿茶", first[-2]["content"])
        self.assertIn("我喜欢绿茶", second[-2]["content"])
        self.assertEqual(second[-1], {"role": "user", "content": "帮我规划今天"})
        self.assertEqual(self.store.count_items("alice"), 1)

    def test_non_stream_chat_also_captures_and_injects(self):
        from types import SimpleNamespace

        captured = []
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="好的", tool_calls=[])
        )])

        def completion(messages, **kwargs):
            captured.append(messages)
            return response

        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(agent, "_skill_context", return_value=None),
            patch.object(agent, "_work_memory_context", return_value=None),
            patch.object(agent, "_completion", side_effect=completion),
        ):
            self.assertEqual(agent.chat("以后请用中文回答"), "好的")
            self.assertEqual(agent.chat("帮我规划今天"), "好的")

        self.assertIn("以后请用中文回答", captured[1][-2]["content"])
        self.assertEqual(self.store.count_items("alice"), 1)


if __name__ == "__main__":
    unittest.main()
