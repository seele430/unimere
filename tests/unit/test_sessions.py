"""会话存储、旧库迁移与按用户隔离的 API 测试。"""

import sqlite3
import tempfile
import unittest

from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src import sessions_api
from src.auth_api import get_current_user
from src.sessions import SessionError, SessionStore


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.now = [1000.0]
        self.store = SessionStore(
            Path(self._temporary.name) / "sessions.db",
            clock=lambda: self.now[0],
        )

    def tick(self, step=10.0):
        self.now[0] += step

    def test_create_and_get(self):
        session = self.store.create("web", "测试会话", owner_id="alice")

        self.assertEqual(session["source"], "web")
        self.assertEqual(session["title"], "测试会话")
        self.assertEqual(session["owner_id"], "alice")
        self.assertIsNotNone(self.store.get(session["id"]))
        self.assertIsNotNone(self.store.get_owned(session["id"], "alice"))
        self.assertIsNone(self.store.get_owned(session["id"], "bob"))

    def test_list_orders_pinned_then_recent(self):
        first = self.store.create("web", "A")
        self.tick()
        second = self.store.create("web", "B")
        self.tick()
        self.store.set_pinned(first["id"], True)

        rows = self.store.list()

        self.assertEqual(rows[0]["id"], first["id"])
        self.assertEqual(rows[1]["id"], second["id"])

    def test_owned_list_filters_user_source_and_deleted(self):
        alice = self.store.create("web", "Alice", owner_id="alice")
        self.store.create("web", "Bob", owner_id="bob")
        self.store.create("qq", "QQ", owner_id="alice")
        deleted = self.store.create("web", "Deleted", owner_id="alice")
        self.store.soft_delete(deleted["id"])

        self.assertEqual(
            [item["id"] for item in self.store.list_owned("alice")],
            [alice["id"]],
        )

    def test_message_idempotent(self):
        session = self.store.create("web")
        self.assertTrue(self.store.add_message(session["id"], "user", "hi", "m1"))
        self.assertFalse(self.store.add_message(session["id"], "user", "hi", "m1"))

        self.assertEqual(self.store.get(session["id"])["message_count"], 1)

    def test_messages_after_cursor(self):
        session = self.store.create("web")
        self.store.add_message(session["id"], "user", "one", "m1")
        self.store.add_message(session["id"], "assistant", "two", "m1")

        rows = self.store.messages(session["id"], after=1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "two")

    def test_context_trims_to_budget(self):
        session = self.store.create("web")

        for index in range(20):
            self.store.add_message(session["id"], "user", f"消息{index}", f"m{index}")

        context = self.store.context(session["id"], max_messages=5, max_chars=100)

        self.assertIn("消息19", context)
        self.assertLessEqual(len(context), 120)

    def test_context_excludes_current_request(self):
        session = self.store.create("web", owner_id="alice")
        self.store.add_message(session["id"], "user", "旧消息", "old")
        self.store.add_message(session["id"], "user", "当前消息", "current")

        context = self.store.context(session["id"], exclude_message_id="current")

        self.assertIn("旧消息", context)
        self.assertNotIn("当前消息", context)

    def test_request_id_recovers_only_owned_web_session(self):
        session = self.store.create("web", owner_id="alice")
        self.store.add_message(session["id"], "user", "你好", "request-1")

        self.assertEqual(
            self.store.web_session_for_message("request-1", "alice"),
            session["id"],
        )
        self.assertIsNone(self.store.web_session_for_message("request-1", "bob"))

    def test_rename_pin_soft_delete(self):
        session = self.store.create("web")
        self.assertTrue(self.store.rename(session["id"], "新名字"))
        self.assertEqual(self.store.get(session["id"])["title_source"], "user")
        self.assertTrue(self.store.set_pinned(session["id"], True))
        self.assertTrue(self.store.soft_delete(session["id"]))
        self.assertFalse(self.store.soft_delete("no-such"))
        self.assertEqual(self.store.list(), [])
        self.assertEqual(len(self.store.list(include_deleted=True)), 1)

    def test_find_by_prefix_rules(self):
        session = self.store.create("web", "A")
        prefix = session["id"][:8]

        self.assertEqual(self.store.find_by_prefix(prefix)[0]["id"], session["id"])

        with self.assertRaises(SessionError):
            self.store.find_by_prefix(session["id"][:3])

    def test_source_filters_keep_qq_commands_inside_qq_channel(self):
        web = self.store.create("web", "网页")
        qq = self.store.create("qq", "QQ")

        self.assertEqual([item["id"] for item in self.store.list(source="qq")], [qq["id"]])
        self.assertEqual([item["id"] for item in self.store.list(source="web")], [web["id"]])
        self.assertEqual(self.store.find_by_prefix(web["id"][:8], source="qq"), [])
        self.assertEqual(
            self.store.find_by_prefix(qq["id"][:8], source="qq")[0]["id"],
            qq["id"],
        )

    def test_ensure_qq_launch_idempotent(self):
        first = self.store.ensure_qq_launch("launch-1", owner_id=7)
        second = self.store.ensure_qq_launch("launch-1", owner_id=7)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.active_session(7)["id"], first["id"])
        self.assertEqual(len(self.store.list()), 1)

    def test_ensure_active_creates_when_missing(self):
        session = self.store.ensure_active(7)

        self.assertEqual(session["source"], "qq")
        self.assertEqual(self.store.active_session(7)["id"], session["id"])

    def test_legacy_database_gains_owner_without_losing_session(self):
        path = Path(self._temporary.name) / "legacy.db"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    title_source TEXT NOT NULL DEFAULT 'auto',
                    bot_launch_id TEXT,
                    pinned_at REAL,
                    deleted_at REAL,
                    last_message_at REAL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                INSERT INTO sessions (
                    id, source, title, title_source, message_count, created_at, updated_at
                ) VALUES ('legacy', 'web', '旧对话', 'auto', 0, 1, 1);
                """
            )

        migrated = SessionStore(path)
        session = migrated.get("legacy")

        self.assertEqual(session["title"], "旧对话")
        self.assertEqual(session["owner_id"], "")
        self.assertEqual(migrated.list_owned("alice"), [])


class ApiCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = SessionStore(Path(self._temporary.name) / "sessions.db")
        self.app = FastAPI()
        self.app.include_router(sessions_api.router)

        def fake_user(request: Request):
            token = request.headers.get("authorization")
            if token == "Bearer alice":
                return {"id": "alice", "role": "member"}
            if token == "Bearer bob":
                return {"id": "bob", "role": "member"}
            if token == "Bearer root":
                return {"id": "root", "role": "owner"}
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="请先登录")

        self.app.dependency_overrides[get_current_user] = fake_user
        self.client = TestClient(self.app)
        patcher = patch.object(sessions_api, "_store", lambda: self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def auth(user="alice"):
        return {"Authorization": f"Bearer {user}"}

    def create(self, user="alice", title=""):
        response = self.client.post(
            "/sessions", json={"title": title}, headers=self.auth(user)
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_create_list_and_read(self):
        created = self.create(title="网页会话")
        listed = self.client.get("/sessions", headers=self.auth()).json()["sessions"]

        self.assertEqual(listed[0]["id"], created["id"])

        self.store.add_message(created["id"], "user", "你好", "m1")
        payload = self.client.get(
            f"/sessions/{created['id']}", headers=self.auth()
        ).json()

        self.assertEqual(payload["messages"][0]["content"], "你好")

    def test_patch_pin_and_delete(self):
        created = self.create()

        patched = self.client.patch(
            f"/sessions/{created['id']}",
            json={"pinned": True, "title": "改名"},
            headers=self.auth(),
        ).json()

        self.assertTrue(patched["pinned"])
        self.assertEqual(patched["title"], "改名")

        self.assertEqual(
            self.client.delete(
                f"/sessions/{created['id']}", headers=self.auth()
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/sessions", headers=self.auth()).json()["sessions"],
            [],
        )

    def test_users_cannot_list_read_change_or_delete_each_others_sessions(self):
        alice = self.create("alice", "Alice 私有对话")
        bob = self.create("bob", "Bob 私有对话")

        alice_list = self.client.get("/sessions", headers=self.auth("alice")).json()
        bob_list = self.client.get("/sessions", headers=self.auth("bob")).json()
        self.assertEqual([item["id"] for item in alice_list["sessions"]], [alice["id"]])
        self.assertEqual([item["id"] for item in bob_list["sessions"]], [bob["id"]])

        foreign_read = self.client.get(
            f"/sessions/{alice['id']}", headers=self.auth("bob")
        )
        foreign_patch = self.client.patch(
            f"/sessions/{alice['id']}",
            json={"title": "越权改名"},
            headers=self.auth("bob"),
        )
        foreign_delete = self.client.delete(
            f"/sessions/{alice['id']}", headers=self.auth("bob")
        )

        self.assertEqual(foreign_read.status_code, 404)
        self.assertEqual(foreign_patch.status_code, 404)
        self.assertEqual(foreign_delete.status_code, 404)
        self.assertEqual(self.store.get(alice["id"])["title"], "Alice 私有对话")
        self.assertFalse(self.store.get(alice["id"])["deleted"])

    def test_qq_records_are_separate_owner_only_and_read_only(self):
        web = self.create("alice", "网页私有对话")
        qq = self.store.create("qq", "QQ 会话", owner_id="alice")
        self.store.add_message(qq["id"], "user", "QQ 问题", "qq-1")
        self.store.add_message(qq["id"], "assistant", "QQ 回复", "reply:qq-1")

        member_list = self.client.get("/sessions", headers=self.auth("alice")).json()
        owner_list = self.client.get("/sessions", headers=self.auth("root")).json()

        self.assertEqual(member_list["web_sessions"], [web])
        self.assertEqual(member_list["qq_sessions"], [])
        self.assertEqual(owner_list["web_sessions"], [])
        self.assertEqual([item["id"] for item in owner_list["qq_sessions"]], [qq["id"]])
        self.assertEqual(
            self.client.get(f"/sessions/{qq['id']}", headers=self.auth("alice")).status_code,
            404,
        )

        detail = self.client.get(f"/sessions/{qq['id']}", headers=self.auth("root"))
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.json()["read_only"])
        self.assertEqual(
            [item["content"] for item in detail.json()["messages"]],
            ["QQ 问题", "QQ 回复"],
        )
        self.assertEqual(
            self.client.patch(
                f"/sessions/{qq['id']}", json={"title": "越权改名"},
                headers=self.auth("root"),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(f"/sessions/{qq['id']}", headers=self.auth("root")).status_code,
            404,
        )
        self.assertEqual(self.store.get(qq["id"])["title"], "QQ 会话")
        self.assertFalse(self.store.get(qq["id"])["deleted"])

    def test_missing_session_404(self):
        self.assertEqual(
            self.client.get("/sessions/nope", headers=self.auth()).status_code,
            404,
        )

    def test_missing_or_invalid_token_401(self):
        self.assertEqual(self.client.get("/sessions").status_code, 401)
        self.assertEqual(
            self.client.get("/sessions", headers=self.auth("invalid")).status_code,
            401,
        )


if __name__ == "__main__":
    unittest.main()
