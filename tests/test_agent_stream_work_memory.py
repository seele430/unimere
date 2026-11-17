"""流式对话与工作记忆的集成：run 收尾落记忆，新对话注入相关上下文。"""

import unittest

from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src import agent, config
from src.agent_stream import _initial_messages, run_chat_stream
from src.core.reminder_identity import bind_authenticated_reminder
from src.core.run_control import CancellationToken
from src.memory import work
from src.memory.store import MemoryStore
from src.sessions import SessionStore
from tests.helper import isolate_paths, tool_result_outcome


def response(*, content="", tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls or [])
    )])


def tool_call(name="list_dir", arguments='{"path":"."}'):
    return SimpleNamespace(
        id="call-work-memory",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class AgentStreamWorkMemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)
        self.identity = bind_authenticated_reminder(
            user_id="alice", request_id="work_memory_request", role="owner"
        )
        self.identity.__enter__()
        self.addCleanup(self.identity.__exit__, None, None, None)
        self.events = []

    def run_tool_chat(self):
        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(agent, "_work_memory_context", return_value=None),
            patch.object(agent, "open_run_sandbox", return_value="sbx_test"),
            patch.object(agent, "destroy_run_sandbox"),
            patch.object(
                agent,
                "execute_tool_on_platform",
                return_value=tool_result_outcome({
                    "success": True, "path": ".", "entries": [], "truncated": False,
                }),
            ),
            patch.object(
                agent,
                "_completion",
                side_effect=[
                    response(tool_calls=[tool_call()]),
                    response(content="已查看"),
                ],
            ),
        ):
            return run_chat_stream(
                "请帮我检查一下当前项目",
                emit=self.events.append,
                cancel_token=CancellationToken(),
            )

    def test_run_finish_writes_actual_tool_work(self):
        reply = self.run_tool_chat()
        self.assertEqual(reply, "已查看")
        store = MemoryStore(config.paths.knowledge_db)
        self.assertEqual(store.count_items(), 1)
        item = store.list_items("alice")[0]
        self.assertEqual(item.kind, "episode")
        self.assertEqual(item.trust, "platform_verified")
        self.assertTrue(item.source_ref.startswith("run:"))
        self.assertIn("请帮我检查一下当前项目", item.text)
        self.assertIn("list_dir：成功", item.text)

    def test_memory_failure_does_not_change_chat_result(self):
        with patch("src.agent_stream.work_memory.record_run", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_tool_chat(), "已查看")
        self.assertEqual(self.events[-1]["type"], "done")

    def test_new_chat_injects_relevant_work_before_user_message(self):
        work.record_run(
            "alice",
            "run_previous",
            "创建 todo.html",
            "succeeded",
            [{"name": "write_file", "result": {"success": True, "path": "todo.html"}}],
        )
        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
        ):
            messages, _ = _initial_messages("上次生成了什么", None)

        self.assertEqual(messages[-1], {"role": "user", "content": "上次生成了什么"})
        self.assertEqual(messages[-2]["role"], "system")
        self.assertIn("todo.html", messages[-2]["content"])
        self.assertIn("历史路径不代表文件仍可访问", messages[-2]["content"])

    def initial_messages(self, text, session_id=None):
        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(agent, "_skill_context", return_value=None),
            patch.object(agent, "_memory_context", return_value=None),
            patch.object(agent, "_work_memory_context", return_value=None),
        ):
            return _initial_messages(text, None, session_id=session_id)[0]

    def test_old_session_injects_history_without_duplicating_current_message(self):
        session = SessionStore().create("web", owner_id="alice")
        store = SessionStore()
        store.add_message(session["id"], "user", "项目叫 Atlas", "old-request")
        store.add_message(session["id"], "assistant", "已记住", "old-request")
        store.add_message(
            session["id"], "user", "项目叫什么", "work_memory_request"
        )

        messages = self.initial_messages("项目叫什么", session["id"])
        context = "\n".join(
            item["content"] for item in messages if item["role"] == "system"
        )

        self.assertIn("用户：项目叫 Atlas", context)
        self.assertIn("助手：已记住", context)
        self.assertNotIn("项目叫什么", context)
        self.assertEqual(messages[-1], {"role": "user", "content": "项目叫什么"})

    def test_new_session_does_not_inject_old_chat(self):
        store = SessionStore()
        old = store.create("web", owner_id="alice")
        new = store.create("web", owner_id="alice")
        store.add_message(old["id"], "user", "旧会话机密内容", "old-request")
        store.add_message(old["id"], "assistant", "旧回复", "old-request")

        messages = self.initial_messages("开始新话题", new["id"])
        context = "\n".join(item["content"] for item in messages)

        self.assertNotIn("旧会话机密内容", context)
        self.assertNotIn("旧回复", context)
        self.assertEqual(messages[-1], {"role": "user", "content": "开始新话题"})

    def test_other_users_session_is_rejected(self):
        foreign = SessionStore().create("web", owner_id="bob")

        with self.assertRaises(PermissionError):
            self.initial_messages("读取别人的会话", foreign["id"])


if __name__ == "__main__":
    unittest.main()
