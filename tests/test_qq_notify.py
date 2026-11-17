"""QQ 官方通知渠道：发送模块与渠道判定测试（全 mock）。"""

import os
import unittest

from unittest.mock import patch

from src.core import notification_delivery as delivery
from src.core import qq_notify
from src.qq.official import OfficialError


class FakeClient:
    def __init__(self):
        self.calls = []

    def send_private_msg(self, user_id, text, **kwargs):
        self.calls.append((user_id, text, kwargs))

        return {"message_id": 1}


class QqNotifyCase(unittest.TestCase):
    def env(self, **extra):
        base = {
            "PA_QQ_OWNER_OPENID": "owner-openid",
            "PA_QQ_APP_ID": "app-id",
            "PA_QQ_APP_SECRET": "app-secret",
        }
        base.update(extra)

        return patch.dict(os.environ, base, clear=True)

    def test_empty_message_rejected(self):
        with self.env(), self.assertRaises(ValueError):
            qq_notify.send_qq_notification("  ", client=FakeClient())

    def test_truncate_1000_chars(self):
        client = FakeClient()

        with self.env():
            qq_notify.send_qq_notification("x" * 1001, client=client)

        self.assertEqual(len(client.calls[0][1]), 1000)

    def test_missing_owner(self):
        with self.env(PA_QQ_OWNER_OPENID=""), self.assertRaises(ValueError):
            qq_notify.send_qq_notification("hello", client=FakeClient())

    def test_missing_credentials(self):
        for name in ("PA_QQ_APP_ID", "PA_QQ_APP_SECRET"):
            with self.subTest(name=name):
                with self.env(**{name: ""}), self.assertRaises(ValueError):
                    qq_notify.send_qq_notification("hello", client=None)

    def test_owner_send_is_active_message_without_msg_id(self):
        client = FakeClient()

        with self.env():
            qq_notify.send_qq_notification("hello", client=client)

        self.assertEqual(
            client.calls, [("owner-openid", "hello", {})]
        )

    def test_builds_official_client(self):
        with self.env(), patch("src.core.qq_notify.OfficialBotClient") as client_class:
            qq_notify.send_qq_notification("hello", client=None)

        client_class.assert_called_once_with(
            app_id="app-id", app_secret="app-secret", owner_openid="owner-openid"
        )
        client_class.return_value.send_private_msg.assert_called_once_with(
            "owner-openid", "hello"
        )

    def test_network_error_propagates(self):
        client = FakeClient()
        client.send_private_msg = lambda *args: (_ for _ in ()).throw(OfficialError("down"))

        with self.env(), self.assertRaises(OfficialError):
            qq_notify.send_qq_notification("hello", client=client)


class ChannelCase(unittest.TestCase):
    def test_channel_matrix(self):
        with patch.object(delivery, "role_of_user", return_value="owner"), patch.dict(
            os.environ, {"PA_QQ_NOTIFY": "1"}
        ):
            self.assertEqual(delivery.channel_for_owner("auth-uuid"), delivery.QQ)

        with patch.object(delivery, "role_of_user", return_value="owner"), patch.dict(
            os.environ, {"PA_QQ_NOTIFY": "0"}
        ):
            self.assertEqual(delivery.channel_for_owner("auth-uuid"), delivery.NTFY)

        with patch.object(delivery, "role_of_user", return_value="admin"), patch.dict(
            os.environ, {"PA_QQ_NOTIFY": "1"}
        ):
            self.assertEqual(delivery.channel_for_owner("auth-uuid"), delivery.IN_APP)

        with patch.object(delivery, "role_of_user", return_value=None), patch.dict(
            os.environ, {"PA_QQ_NOTIFY": "1"}
        ):
            self.assertEqual(delivery.channel_for_owner("auth-uuid"), delivery.IN_APP)

        self.assertEqual(delivery.channel_for_owner(None), delivery.NTFY)


if __name__ == "__main__":
    unittest.main()
