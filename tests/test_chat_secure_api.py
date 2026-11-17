
"""受保护聊天路由的 HTTP 回归测试。

所有数据库都建立在临时目录中；模型调用一律 mock。
绝不访问项目的真实数据库，也不会消费模型额度。
"""

import unittest
import uuid

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.helper import create_auth_user, isolate_paths
from src import api
from src.auth_api import get_auth_store
from src.core.auth_store import AuthStore
from src.main import app
from src.sessions import SessionStore


class TestSecureChatHttp(unittest.TestCase):
    PASSWORD = "TestPassword2026!"

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)

        # 所有数据库路径都指向临时目录，绝不接触真实数据
        isolate_paths(self, self.temp_dir)

        # 每项测试使用独立的临时账户数据库
        self.store = AuthStore(root / "test_auth.db")

        owner = self.store.bootstrap_owner(
            "testowner",
            self.PASSWORD,
        )

        create_auth_user(self.store, "testmember", self.PASSWORD, "member")

        # 正式 App 的认证依赖使用临时账户库
        self.old_overrides = app.dependency_overrides.copy()

        app.dependency_overrides[get_auth_store] = (
            lambda: self.store
        )

        # 触发正式启动流程及非破坏性迁移
        self.client = TestClient(app)
        self.client.__enter__()

        self.owner_token = self.login("testowner")
        self.member_token = self.login("testmember")

    def tearDown(self):
        self.client.__exit__(None, None, None)

        app.dependency_overrides.clear()
        app.dependency_overrides.update(
            self.old_overrides
        )

        self.temp_dir.cleanup()

    def login(self, username):
        response = self.client.post(
            "/auth/login",
            json={
                "username": username,
                "password": self.PASSWORD,
            },
        )

        self.assertEqual(response.status_code, 200)
        return response.json()["token"]

    @staticmethod
    def auth(token):
        return {
            "Authorization": f"Bearer {token}"
        }

    @staticmethod
    def request_id():
        return "secure_" + uuid.uuid4().hex

    def post_chat(
        self,
        token,
        request_id,
        message="测试消息",
        *,
        session_id=None,
    ):
        payload = {
            "request_id": request_id,
            "message": message,
        }
        if session_id is not None:
            payload["session_id"] = session_id
        return self.client.post(
            "/chat",
            headers=self.auth(token),
            json=payload,
        )

    def get_status(self, token, request_id):
        return self.client.get(
            f"/chat/requests/{request_id}",
            headers=self.auth(token),
        )

    # ======================================
    # 1. 未登录不能发送，也不能读取
    # ======================================

    def test_no_token_cannot_send_or_read(self):
        request_id = self.request_id()

        with patch.object(api, "chat") as model:
            sent = self.client.post(
                "/chat",
                json={
                    "request_id": request_id,
                    "message": "测试",
                },
            )

            read = self.client.get(
                f"/chat/requests/{request_id}"
            )

        self.assertEqual(sent.status_code, 401)
        self.assertEqual(read.status_code, 401)

        model.assert_not_called()

    # ======================================
    # 2. 伪造 Token 无法使用
    # ======================================

    def test_invalid_token_returns_401(self):
        with patch.object(api, "chat") as model:
            sent = self.post_chat(
                "not-a-valid-token",
                self.request_id(),
            )

            read = self.get_status(
                "not-a-valid-token",
                self.request_id(),
            )

        self.assertEqual(sent.status_code, 401)
        self.assertEqual(read.status_code, 401)

        model.assert_not_called()

    # ======================================
    # 3. 本人发送、查询与缓存去重
    # ======================================

    def test_owner_can_send_query_and_reuse_cached_reply(
        self,
    ):
        request_id = self.request_id()

        with patch.object(
            api,
            "chat",
            return_value="模拟 AI 回复",
        ) as model:

            first = self.post_chat(
                self.owner_token,
                request_id,
            )

            second = self.post_chat(
                self.owner_token,
                request_id,
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        self.assertEqual(
            first.json(),
            second.json(),
        )

        session_id = first.json()["session_id"]
        self.assertTrue(session_id)
        model.assert_called_once_with("测试消息", session_id=session_id)

        result = self.get_status(
            self.owner_token,
            request_id,
        )

        self.assertEqual(result.status_code, 200)

        self.assertEqual(
            result.json()["reply"],
            "模拟 AI 回复",
        )

    # ======================================
    # 4. 相同 ID 不能换消息内容
    # ======================================

    def test_same_request_id_different_message_returns_409(
        self,
    ):
        request_id = self.request_id()

        with patch.object(
            api,
            "chat",
            return_value="模拟回复",
        ) as model:

            first = self.post_chat(
                self.owner_token,
                request_id,
                "第一条",
            )

            second = self.post_chat(
                self.owner_token,
                request_id,
                "第二条",
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

        model.assert_called_once()

    # ======================================
    # 5. 其他用户不能读取或冒领
    # ======================================

    def test_other_user_cannot_read_or_claim_request(
        self,
    ):
        request_id = self.request_id()

        with patch.object(
            api,
            "chat",
            return_value="站长私有回复",
        ) as model:

            created = self.post_chat(
                self.owner_token,
                request_id,
            )

            foreign_read = self.get_status(
                self.member_token,
                request_id,
            )

            foreign_post = self.post_chat(
                self.member_token,
                request_id,
            )

        self.assertEqual(created.status_code, 200)
        self.assertEqual(foreign_read.status_code, 404)
        self.assertEqual(foreign_post.status_code, 404)

        model.assert_called_once()

    # ======================================
    # 6. 不同用户各自拥有独立请求
    # ======================================

    def test_users_can_create_independent_requests(
        self,
    ):
        first_id = self.request_id()
        second_id = self.request_id()

        with patch.object(
            api,
            "chat",
            side_effect=["回复一", "回复二"],
        ) as model:

            first = self.post_chat(
                self.owner_token,
                first_id,
            )

            second = self.post_chat(
                self.member_token,
                second_id,
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        self.assertEqual(
            self.get_status(
                self.owner_token,
                first_id,
            ).status_code,
            200,
        )

        self.assertEqual(
            self.get_status(
                self.member_token,
                second_id,
            ).status_code,
            200,
        )

        self.assertEqual(
            self.get_status(
                self.owner_token,
                second_id,
            ).status_code,
            404,
        )

        self.assertEqual(
            self.get_status(
                self.member_token,
                first_id,
            ).status_code,
            404,
        )

        self.assertEqual(model.call_count, 2)

    # ======================================
    # 7. 模型结果不明时不能重复计费
    # ======================================

    def test_unknown_model_result_is_not_executed_again(
        self,
    ):
        request_id = self.request_id()

        with patch.object(
            api,
            "chat",
            side_effect=TimeoutError("模拟超时"),
        ) as model:

            first = self.post_chat(
                self.owner_token,
                request_id,
            )

            second = self.post_chat(
                self.owner_token,
                request_id,
            )

        self.assertEqual(first.status_code, 502)
        self.assertEqual(second.status_code, 409)

        result = self.get_status(
            self.owner_token,
            request_id,
        )

        self.assertEqual(
            result.json()["status"],
            "unknown",
        )

        model.assert_called_once()

    # ======================================
    # 8. 旧记录保留，但不能被冒领
    # ======================================

    def test_legacy_unowned_record_is_not_exposed(
        self,
    ):
        request_id = self.request_id()

        with closing(api.connect_db()) as conn, conn:
            conn.execute(
                """
                INSERT INTO chat_requests (
                    request_id,
                    message_hash,
                    status,
                    reply,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?, ?, 'completed',
                    '历史私有回复', 't1', 't1'
                )
                """,
                (
                    request_id,
                    api.message_digest("旧请求"),
                ),
            )

        with patch.object(api, "chat") as model:
            owner_read = self.get_status(
                self.owner_token,
                request_id,
            )

            member_read = self.get_status(
                self.member_token,
                request_id,
            )

            attempted_claim = self.post_chat(
                self.owner_token,
                request_id,
                "旧请求",
            )

        self.assertEqual(owner_read.status_code, 404)
        self.assertEqual(member_read.status_code, 404)

        self.assertEqual(
            attempted_claim.status_code,
            404,
        )

        # 确认旧数据并未被删除或修改
        with closing(api.connect_db()) as conn:
            row = conn.execute(
                """
                SELECT reply, user_id
                FROM chat_requests
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()

        self.assertEqual(
            row["reply"],
            "历史私有回复",
        )

        self.assertIsNone(row["user_id"])
        model.assert_not_called()

    # ======================================
    # 9. 退出登录后令牌立即失效
    # ======================================

    def test_logout_revokes_token_for_chat(self):
        response = self.client.post(
            "/auth/logout",
            headers=self.auth(self.owner_token),
        )

        self.assertEqual(response.status_code, 200)

        with patch.object(api, "chat") as model:
            sent = self.post_chat(
                self.owner_token,
                self.request_id(),
            )

            read = self.get_status(
                self.owner_token,
                self.request_id(),
            )

        self.assertEqual(sent.status_code, 401)
        self.assertEqual(read.status_code, 401)

        model.assert_not_called()

    # ======================================
    # 10. 并发重复请求不能调用两次模型
    # ======================================

    def test_duplicate_while_processing_returns_202(
        self,
    ):
        request_id = self.request_id()

        started = Event()
        release = Event()

        def slow_model(_message, *, session_id=None):
            started.set()

            if not release.wait(timeout=8):
                raise TimeoutError(
                    "模拟模型等待超时"
                )

            return "模拟长任务回复"

        with patch.object(
            api,
            "chat",
            side_effect=slow_model,
        ) as model:

            with ThreadPoolExecutor(
                max_workers=2,
            ) as executor:

                pending = executor.submit(
                    self.post_chat,
                    self.owner_token,
                    request_id,
                )

                try:
                    self.assertTrue(
                        started.wait(timeout=5)
                    )

                    duplicate = self.post_chat(
                        self.owner_token,
                        request_id,
                    )

                    self.assertEqual(
                        duplicate.status_code,
                        202,
                    )

                    self.assertEqual(
                        duplicate.json()["status"],
                        "processing",
                    )
                    self.assertTrue(duplicate.json()["session_id"])

                finally:
                    release.set()

                completed = pending.result(
                    timeout=10
                )

        self.assertEqual(
            completed.status_code,
            200,
        )

        model.assert_called_once()

    def test_streaming_chat_emits_sse_and_saves_completed_reply(self):
        request_id = self.request_id()

        def stream_model(message, *, emit, cancel_token, bot=None, session_id=None):
            emit({"type": "run_started", "run_id": "run-test"})
            emit({"type": "text_delta", "run_id": "run-test", "text": "流式"})
            emit({"type": "done", "run_id": "run-test", "reply": "流式回复"})
            return "流式回复"

        with patch("src.chat_secure_api.run_chat_stream", side_effect=stream_model):
            response = self.client.post(
                "/chat",
                headers=self.auth(self.owner_token),
                json={"request_id": request_id, "message": "测试", "stream": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("event: text_delta", response.text)
        self.assertIn('"text": "流式"', response.text)
        status = self.get_status(self.owner_token, request_id).json()
        self.assertEqual(status["status"], "completed")
        saved = SessionStore().messages(status["session_id"])
        self.assertEqual(
            [(item["role"], item["content"]) for item in saved],
            [("user", "测试"), ("assistant", "流式回复")],
        )

    def test_messages_continue_same_session_and_new_chat_is_isolated(self):
        with patch.object(
            api, "chat", side_effect=["回复一", "回复二", "回复三"]
        ) as model:
            first = self.post_chat(
                self.owner_token, self.request_id(), "项目叫 Atlas"
            )
            session_id = first.json()["session_id"]
            continued = self.post_chat(
                self.owner_token,
                self.request_id(),
                "继续这个话题",
                session_id=session_id,
            )
            fresh = self.post_chat(
                self.owner_token, self.request_id(), "全新对话"
            )

        self.assertEqual(continued.status_code, 200)
        self.assertEqual(continued.json()["session_id"], session_id)
        self.assertEqual(fresh.status_code, 200)
        self.assertNotEqual(fresh.json()["session_id"], session_id)
        self.assertEqual(
            model.call_args_list[1].kwargs["session_id"], session_id
        )

        store = SessionStore()
        old_messages = store.messages(session_id)
        new_messages = store.messages(fresh.json()["session_id"])
        self.assertEqual(
            [item["content"] for item in old_messages],
            ["项目叫 Atlas", "回复一", "继续这个话题", "回复二"],
        )
        self.assertEqual(
            [item["content"] for item in new_messages],
            ["全新对话", "回复三"],
        )

    def test_cancel_endpoint_reaches_owner_active_run(self):
        from src.core.run_control import active_runs

        request_id = self.request_id()
        with patch.object(api, "chat", return_value="原回复"):
            self.assertEqual(
                self.post_chat(self.owner_token, request_id).status_code, 200
            )
        me = self.client.get("/auth/me", headers=self.auth(self.owner_token))
        user_id = me.json()["user"]["id"]
        token = active_runs.start(user_id, request_id)
        self.addCleanup(active_runs.finish, user_id, request_id)

        cancelled = self.client.post(
            f"/chat/requests/{request_id}/cancel",
            headers=self.auth(self.owner_token),
        )
        denied = self.client.post(
            f"/chat/requests/{request_id}/cancel",
            headers=self.auth(self.member_token),
        )
        self.assertEqual(cancelled.status_code, 202)
        self.assertEqual(cancelled.json()["status"], "cancelling")
        self.assertTrue(token.cancelled)
        self.assertEqual(denied.status_code, 404)


if __name__ == "__main__":
    unittest.main()