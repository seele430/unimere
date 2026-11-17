"""QQ 官方机器人发送端：全 mock，不联网、不需要真实 AppID。"""

import base64
import json
import tempfile
import unittest

from pathlib import Path

from src.qq.official import (
    MAX_IMAGE_BYTES,
    MAX_REPLIES,
    OfficialBotClient,
    OfficialError,
    plan_messages,
)
from src.qq.transport import official_uid


OPENID = "OPENID_TEST_123"
UID = official_uid(OPENID)


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """按 URL 后缀返回固定 JSON；记录每次请求。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        body = json.loads(request.data.decode("utf-8")) if request.data else {}
        self.calls.append(
            {
                "method": request.get_method(),
                "url": url,
                "body": body,
                "headers": {k.lower(): v for k, v in request.header_items()},
            }
        )

        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                return FakeResponse(payload)

        raise AssertionError(f"unexpected url: {url}")


def make_client(opener, *, clock=None):
    kwargs = {"clock": clock} if clock else {}

    return OfficialBotClient(
        app_id="APPID",
        app_secret="SECRET",
        owner_openid=OPENID,
        opener=opener,
        **kwargs,
    )


TOKEN_ROUTE = {
    "/app/getAppAccessToken": {"access_token": "tok-1", "expires_in": 7200},
    "/v2/users/OPENID_TEST_123/messages": {"id": "sent-1"},
    "/v2/users/OPENID_TEST_123/files": {"file_info": "FILEINFO"},
}


class TokenTests(unittest.TestCase):
    def test_token_is_cached_between_sends(self):
        opener = FakeOpener(TOKEN_ROUTE)
        client = make_client(opener)

        client.send_private_msg(UID, "一")
        client.send_private_msg(UID, "二")

        token_calls = [c for c in opener.calls if c["url"].endswith("getAppAccessToken")]
        self.assertEqual(len(token_calls), 1, "token 应缓存，不应每次发送都换")

    def test_token_refreshes_when_near_expiry(self):
        now = {"t": 1000.0}
        opener = FakeOpener(TOKEN_ROUTE)
        client = make_client(opener, clock=lambda: now["t"])

        client.send_private_msg(UID, "一")
        now["t"] = 1000.0 + 7200 - 10  # 进入 60 秒安全期
        client.send_private_msg(UID, "二")

        token_calls = [c for c in opener.calls if c["url"].endswith("getAppAccessToken")]
        self.assertEqual(len(token_calls), 2, "临近过期应重新换 token")

    def test_authorization_header_uses_qqbot_scheme(self):
        opener = FakeOpener(TOKEN_ROUTE)
        client = make_client(opener)
        client.send_private_msg(UID, "hi")

        message_calls = [c for c in opener.calls if c["url"].endswith("/messages")]
        self.assertEqual(message_calls[0]["headers"]["authorization"], "QQBot tok-1")

    def test_missing_credentials_raises(self):
        client = OfficialBotClient(app_id="", app_secret="", owner_openid=OPENID)
        self.assertFalse(client.configured)
        self.assertRaises(OfficialError, client.send_private_msg, UID, "hi")


class SendTests(unittest.TestCase):
    def test_message_carries_msg_id_and_seq(self):
        opener = FakeOpener(TOKEN_ROUTE)
        client = make_client(opener)

        client.send_private_msg(UID, "现在几点", msg_id="m-1", msg_seq=2)

        body = [c for c in opener.calls if c["url"].endswith("/messages")][0]["body"]
        self.assertEqual(body["content"], "现在几点")
        self.assertEqual(body["msg_type"], 0)
        self.assertEqual(body["msg_id"], "m-1")
        self.assertEqual(body["msg_seq"], 2)

    def test_unknown_user_raises_chinese_error(self):
        opener = FakeOpener(TOKEN_ROUTE)
        client = make_client(opener)

        with self.assertRaises(OfficialError) as caught:
            client.send_private_msg(42, "hi")

        self.assertIn("openid", str(caught.exception))

    def test_empty_text_rejected(self):
        opener = FakeOpener(TOKEN_ROUTE)
        client = make_client(opener)
        self.assertRaises(OfficialError, client.send_private_msg, UID, "   ")

    def test_image_upload_uses_base64_and_msg_type_7(self):
        opener = FakeOpener(TOKEN_ROUTE)
        client = make_client(opener)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

            client.send_private_image(UID, str(path), "第一页", msg_id="m-2", msg_seq=1)

        upload = [c for c in opener.calls if c["url"].endswith("/files")][0]["body"]
        self.assertEqual(upload["file_type"], 1)
        self.assertFalse(upload["srv_send_msg"])
        self.assertEqual(base64.b64decode(upload["file_data"]), b"\x89PNG\r\n\x1a\nfake")

        message = [c for c in opener.calls if c["url"].endswith("/messages")][0]["body"]
        self.assertEqual(message["msg_type"], 7)
        self.assertEqual(message["media"], {"file_info": "FILEINFO"})
        self.assertEqual(message["content"], "第一页")

    def test_oversized_image_rejected(self):
        opener = FakeOpener(TOKEN_ROUTE)
        client = make_client(opener)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.png"
            path.write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))

            with self.assertRaises(OfficialError) as caught:
                client.send_private_image(UID, str(path))

        self.assertIn("上限", str(caught.exception))


class PlanTests(unittest.TestCase):
    def test_four_pages_fit_budget(self):
        items = [
            ("text", "杭州三日游总览"),
            ("image", "p1.png", "第 1 天"),
            ("image", "p2.png", "第 2 天"),
            ("image", "p3.png", "第 3 天"),
            ("image", "p4.png", "返程"),
        ]

        messages, dropped = plan_messages(items, budget=MAX_REPLIES)

        self.assertEqual(len(messages), MAX_REPLIES)
        self.assertEqual(dropped, 0)
        # 文本被合并进相邻消息，而不是把图丢掉。
        self.assertEqual(sum(1 for m in messages if m["kind"] == "image"), 4)
        self.assertTrue(any("杭州三日游总览" in (m.get("caption") or "") for m in messages))

    def test_too_many_images_drops_tail_and_counts(self):
        items = [("image", f"p{i}.png", "") for i in range(1, 6)]

        messages, dropped = plan_messages(items, budget=MAX_REPLIES)

        self.assertEqual(len(messages), MAX_REPLIES)
        self.assertEqual(dropped, 1)

    def test_pure_text_keeps_order(self):
        items = [("text", "a"), ("text", "b")]

        messages, dropped = plan_messages(items, budget=MAX_REPLIES)

        self.assertEqual([m["text"] for m in messages], ["a", "b"])
        self.assertEqual(dropped, 0)


class RichSendTests(unittest.TestCase):
    def test_rich_items_send_at_most_budget_with_incrementing_seq(self):
        opener = FakeOpener(TOKEN_ROUTE)
        client = make_client(opener)

        with tempfile.TemporaryDirectory() as tmp:
            paths = []

            for index in range(1, 5):
                path = Path(tmp) / f"p{index}.png"
                path.write_bytes(b"PNG")
                paths.append(str(path))

            items = [("text", "总览")] + [
                ("image", path, f"第 {index} 页") for index, path in enumerate(paths, 1)
            ]

            client.send_rich_items(UID, items, msg_id="m-3")

        message_calls = [c for c in opener.calls if c["url"].endswith("/messages")]
        self.assertEqual(len(message_calls), MAX_REPLIES)
        self.assertEqual(
            [c["body"]["msg_seq"] for c in message_calls], [1, 2, 3, 4]
        )
        self.assertTrue(
            any(c["body"]["msg_type"] == 7 for c in message_calls),
            "图文页应使用 msg_type=7 富媒体",
        )

    def test_rich_items_after_notice_use_remaining_budget_and_seq(self):
        opener = FakeOpener(TOKEN_ROUTE)
        client = make_client(opener)

        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index in range(1, 4):
                path = Path(tmp) / f"p{index}.png"
                path.write_bytes(b"PNG")
                paths.append(str(path))

            items = [("text", "总览")] + [
                ("image", path, f"第 {index} 页") for index, path in enumerate(paths, 1)
            ]
            client.send_rich_items(UID, items, msg_id="m-after-notice", msg_seq=2)

        message_calls = [c for c in opener.calls if c["url"].endswith("/messages")]
        self.assertEqual(len(message_calls), 3)
        self.assertEqual([c["body"]["msg_seq"] for c in message_calls], [2, 3, 4])
        self.assertTrue(all(c["body"]["msg_type"] == 7 for c in message_calls))
        self.assertIn("总览", message_calls[0]["body"]["content"])


if __name__ == "__main__":
    unittest.main()
