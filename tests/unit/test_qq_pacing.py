"""QQ 多段发送节奏：分档随机停顿、配置回退与发送边界。全 mock，不真实等待。"""

import os
import tempfile
import unittest

from pathlib import Path
from unittest.mock import call, patch

from src.qq.inbound import QQInbound
from src.qq.transport import QQTransportError
from src.qq.outbox import QQOutbox
from src.qq.worker import DELAY_DEFAULTS, DELAY_ENV_PREFIXES, TaskWorker, chunk_pause


class DelayConfigCase(unittest.TestCase):
    def test_default_ranges_for_all_kinds(self):
        for kind, bounds in DELAY_DEFAULTS.items():
            with self.subTest(kind=kind), patch.dict(os.environ, {}, clear=True):
                with patch("src.qq.worker.random.uniform", return_value=bounds[0]) as uniform:
                    with patch("src.qq.worker.time.sleep") as sleep:
                        delay = chunk_pause(kind)

                uniform.assert_called_once_with(*bounds)
                sleep.assert_called_once_with(bounds[0])
                self.assertEqual(delay, bounds[0])

    def test_environment_overrides_for_all_kinds(self):
        for kind, prefix in DELAY_ENV_PREFIXES.items():
            environment = {prefix + "_MIN": "1.2", prefix + "_MAX": "3.4"}
            with self.subTest(kind=kind), patch.dict(os.environ, environment, clear=True):
                with patch("src.qq.worker.random.uniform", return_value=2.0) as uniform:
                    with patch("src.qq.worker.time.sleep"):
                        chunk_pause(kind)

                uniform.assert_called_once_with(1.2, 3.4)

    def test_invalid_chunk_values_fall_back_to_default(self):
        invalid_pairs = [
            ("abc", "8"), ("-1", "8"), ("9", "8"),
            ("nan", "8"), ("2", "inf"), ("2", "31"),
        ]

        for minimum, maximum in invalid_pairs:
            environment = {
                "PA_QQ_CHUNK_DELAY_MIN": minimum,
                "PA_QQ_CHUNK_DELAY_MAX": maximum,
            }
            with self.subTest(minimum=minimum, maximum=maximum):
                with patch.dict(os.environ, environment, clear=True):
                    with patch("src.qq.worker.random.uniform", return_value=2.0) as uniform:
                        with patch("src.qq.worker.time.sleep"):
                            chunk_pause()

                uniform.assert_called_once_with(2.0, 8.0)

    def test_explicit_zero_disables_sleep(self):
        environment = {"PA_QQ_CHUNK_DELAY_MIN": "0", "PA_QQ_CHUNK_DELAY_MAX": "0"}

        with patch.dict(os.environ, environment, clear=True):
            with patch("src.qq.worker.time.sleep") as sleep:
                self.assertEqual(chunk_pause(), 0.0)

        sleep.assert_not_called()

    def test_deterministic_random_sequence(self):
        values = [2.1, 3.2, 4.3]

        with patch.dict(os.environ, {}, clear=True):
            with patch("src.qq.worker.random.uniform", side_effect=values):
                with patch("src.qq.worker.time.sleep") as sleep:
                    actual = [chunk_pause() for _ in range(3)]

        self.assertEqual(actual, values)
        self.assertEqual(sleep.call_args_list, [call(value) for value in values])


class FakeClient:
    def __init__(self, fail_on=None):
        self.fail_on = dict(fail_on or {})
        self.calls = []
        self.events = []

    def send_private_msg(self, user_id, text, **kwargs):
        self.calls.append(text)
        self.events.append(("text", text))
        remaining = int(self.fail_on.get(text) or 0)

        if remaining:
            self.fail_on[text] = remaining - 1
            raise QQTransportError("temporary disconnect")

        return {"message_id": 1}

    def send_private_image(self, user_id, file_path, caption='', **kwargs):
        self.events.append(("image", file_path))

        if caption:
            self.events.append(("text", caption))

        return {"message_id": 2}

    def send_rich_items(self, user_id, items, **kwargs):
        """模拟逐条发送客户端：失败时带上已发送条数。"""

        sent = 0

        try:
            for offset, item in enumerate(list(items or [])):
                if offset and kwargs.get("pause"):
                    kwargs["pause"](offset)

                if str(item[0]) == "image":
                    self.send_private_image(
                        user_id, item[1], item[2] if len(item) > 2 else ""
                    )
                else:
                    self.send_private_msg(user_id, item[1])

                sent += 1
        except QQTransportError as error:
            error.sent_count = sent

            raise


class WorkerPacingCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self._environment = patch.dict(
            os.environ,
            {"PERSONAL_AGENT_DATA_DIR": str(self.root), "PA_QQ_AUDIT": "0"},
            clear=False,
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)
        self.outbox = QQOutbox(self.root / "outbox.db")

    def worker(self, client):
        handler = QQInbound(owner_id=7, secret="s", client=client)
        return TaskWorker(outbox=self.outbox, client=client, handler=handler), handler

    def running_job(self, message_id="m-1"):
        self.outbox.enqueue(
            message_id=message_id, sender=7, raw_text="/ask 问题", queue="task"
        )
        self.outbox.set_routing(
            message_id,
            intent="ask",
            params={"question": "问题"},
            params_hash="h",
            decision_source="command",
            state="running",
        )
        return {"message_id": message_id, "sender": 7, "command": "ask"}

    def test_single_chunk_does_not_pause(self):
        client = FakeClient()
        worker, _handler = self.worker(client)

        with patch("src.qq.worker.chunk_pause", return_value=0.0) as pause:
            self.assertTrue(worker.reply_many(self.running_job(), ["唯一一段"]))

        pause.assert_not_called()
        self.assertEqual(client.calls, ["唯一一段"])

    def test_reply_many_preserves_order_and_audits_pause(self):
        client = FakeClient()
        worker, handler = self.worker(client)

        with patch("src.qq.worker.chunk_pause", return_value=1.25) as pause:
            self.assertTrue(worker.reply_many(self.running_job(), ["第一段", "第二段", "第三段"]))

        self.assertEqual(client.calls, ["第一段", "第二段", "第三段"])
        self.assertEqual(pause.call_args_list, [call("chunk"), call("chunk")])
        audits = [row for row in handler.audit if row["action"] == "send-pause"]
        self.assertEqual(len(audits), 2)
        self.assertEqual(audits[0]["error"], "kind=chunk index=1 seconds=1.250")
        self.assertNotIn("第一段", str(audits))
        self.assertNotIn("第二段", str(audits))

    def test_rich_media_uses_caption_then_item_pause(self):
        client = FakeClient()
        worker, _handler = self.worker(client)
        items = [("image", "page.png", "本页配文"), ("text", "下一项")]

        with patch("src.qq.worker.chunk_pause", return_value=0.0) as pause:
            self.assertTrue(worker.reply_rich(self.running_job(), items))

        # 改造后配文随图片合并下发，只保留 item 级停顿（官方通道图文混排不需要 caption 停顿）。
        self.assertEqual(pause.call_args_list, [call("item")])
        self.assertEqual(
            client.events,
            [("image", "page.png"), ("text", "本页配文"), ("text", "下一项")],
        )

    def test_retry_does_not_add_another_chunk_pause(self):
        client = FakeClient(fail_on={"第二段": 1})
        worker, _handler = self.worker(client)

        with patch("src.qq.worker.SEND_RETRY_DELAY", 0):
            with patch("src.qq.worker.chunk_pause", return_value=0.0) as pause:
                self.assertTrue(worker.reply_many(self.running_job(), ["第一段", "第二段"]))

        pause.assert_called_once_with("chunk")
        self.assertEqual(client.calls, ["第一段", "第二段", "第二段"])

    def test_terminal_failure_stops_before_another_pause(self):
        client = FakeClient(fail_on={"第二段": 2})
        worker, _handler = self.worker(client)

        with patch("src.qq.worker.SEND_RETRY_DELAY", 0):
            with patch("src.qq.worker.chunk_pause", return_value=0.0) as pause:
                self.assertFalse(worker.reply_many(self.running_job(), ["第一段", "第二段", "第三段"]))

        pause.assert_called_once_with("chunk")
        self.assertEqual(client.calls, ["第一段", "第二段", "第二段"])
        self.assertEqual(self.outbox.get("m-1")["state"], "failed")


if __name__ == "__main__":
    unittest.main()
