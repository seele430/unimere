"""QQ 官方 WS 网关：用假连接验证鉴权帧、心跳与 C2C 分发，不联网。"""

import json
import unittest

from src.qq.gateway import INTENT_GROUP_AND_C2C, OfficialGateway


class FakeClient:
    def __init__(self):
        self.bound = []

    def gateway_url(self):
        return "wss://example.invalid/ws"

    def access_token(self):
        return "tok"

    def bind(self, openid):
        self.bound.append(openid)

        return 1


class ScriptedWS:
    """按脚本吐帧；耗尽后抛 ConnectionError 让连接层退出。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout=None):
        if not self.frames:
            raise ConnectionError("closed")

        return self.frames.pop(0)


class TimeoutThenCloseWS(ScriptedWS):
    """第一次 recv 超时（触发心跳），第二次断开。"""

    def __init__(self):
        super().__init__([])
        self._timed_out = False

    def recv(self, timeout=None):
        if not self._timed_out:
            self._timed_out = True

            raise TimeoutError

        raise ConnectionError("closed")


def run_once(gateway, ws):
    """只跑一轮连接：断开后退避时直接置停止，避免测试里无限重连。"""

    gateway._connector = lambda url: ws
    gateway._sleep = lambda seconds: gateway._stop.set()
    gateway.run_forever()


def c2c_frame(**overrides):
    data = {
        "id": "msg-1",
        "content": "现在几点",
        "author": {"user_openid": "OPENID_A"},
    }
    data.update(overrides)

    return json.dumps({"op": 0, "s": 1, "t": "C2C_MESSAGE_CREATE", "d": data})


class GatewayTests(unittest.TestCase):
    def test_auth_frame_uses_qqbot_token_and_c2c_intent(self):
        ws = ScriptedWS([json.dumps({"op": 0, "s": 0, "t": "READY", "d": {"session_id": "s"}})])
        gateway = OfficialGateway(FakeClient(), on_message=lambda *a: None)

        run_once(gateway, ws)

        self.assertEqual(ws.sent[0]["op"], 2)
        self.assertEqual(ws.sent[0]["d"]["token"], "QQBot tok")
        self.assertEqual(ws.sent[0]["d"]["intents"], INTENT_GROUP_AND_C2C)
        self.assertEqual(ws.sent[0]["d"]["shard"], [0, 1])

    def test_c2c_message_dispatched_and_openid_bound(self):
        received = []
        client = FakeClient()
        ws = ScriptedWS([c2c_frame()])
        gateway = OfficialGateway(
            client, on_message=lambda openid, mid, text: received.append((openid, mid, text))
        )

        run_once(gateway, ws)

        self.assertEqual(received, [("OPENID_A", "msg-1", "现在几点")])
        self.assertEqual(client.bound, ["OPENID_A"])
        self.assertEqual(gateway.stats["c2c"], 1)

    def test_group_event_is_ignored(self):
        ws = ScriptedWS(
            [json.dumps({"op": 0, "s": 1, "t": "GROUP_AT_MESSAGE_CREATE", "d": {}})]
        )
        gateway = OfficialGateway(FakeClient(), on_message=lambda *a: None)

        run_once(gateway, ws)

        self.assertEqual(gateway.stats["ignored"], 1)
        self.assertEqual(gateway.stats["c2c"], 0)

    def test_heartbeat_sent_when_recv_times_out(self):
        ws = TimeoutThenCloseWS()
        gateway = OfficialGateway(FakeClient(), on_message=lambda *a: None, heartbeat=30.0)

        run_once(gateway, ws)

        self.assertTrue(
            any(frame.get("op") == 1 for frame in ws.sent), "超时后应发 op=1 心跳"
        )

    def test_server_reconnect_request_raises(self):
        gateway = OfficialGateway(FakeClient(), on_message=lambda *a: None)

        with self.assertRaises(ConnectionError):
            gateway._dispatch({"op": 7})

    def test_callback_exception_does_not_break_stream(self):
        calls = []

        def boom(*args):
            calls.append(args)

            raise RuntimeError("handler failed")

        ws = ScriptedWS([c2c_frame(id="m-1"), c2c_frame(id="m-2")])
        gateway = OfficialGateway(FakeClient(), on_message=boom)

        run_once(gateway, ws)

        self.assertEqual(len(calls), 2, "单条处理失败不应打断收流")


if __name__ == "__main__":
    unittest.main()
