"""图文长图（guide）测试：路由、规划渲染管线、Worker 图文发送。全 mock，不联网。"""

import json
import os
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.qq.guide import GuideError, make_guide
from src.qq.inbound import QQInbound
from src.qq.intents import check_contract
from src.qq.transport import QQTransportError
from src.qq.outbox import QQOutbox
from src.qq.router import classify
from src.qq.worker import TaskWorker


PLAN = {
    "title": "杭州三日游",
    "summary": "三天行程摘要",
    "pages": [
        {"title": "第 1 天", "caption": "西湖环湖", "content": "断桥、白堤、雷峰塔"},
        {"title": "第 2 天", "caption": "灵隐西溪", "content": "灵隐寺、西溪湿地"},
    ],
}


class FakeProvider:
    configured = True
    model = "test-model"

    def __init__(self, plan=None):
        self.prompts = []
        self.plan = plan if plan is not None else PLAN

    def submit(self, prompt, *, artifact_name, out_dir):
        self.prompts.append(prompt)
        target = Path(out_dir) / artifact_name

        if artifact_name == "plan.json":
            target.write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")
        else:
            target.write_text(
                "<!DOCTYPE html><html><body>page</body></html>", encoding="utf-8"
            )

        return {"task_id": "t", "artifact": str(target)}


def fake_renderer(html_path, png_path):
    png_path.write_bytes(b"png")

    return png_path


class RouterCase(unittest.TestCase):
    def test_guide_intent_for_rich_media_request(self):
        decision = classify("杭州三日游攻略，图文结合")

        self.assertEqual(decision["intent"], "guide")
        self.assertEqual(decision["action"], "execute")
        self.assertEqual(decision["params"]["topic"], "杭州三日游攻略，图文结合")

    def test_followup_rich_media_request_also_routes_to_guide(self):
        decision = classify("图文结合的形式，可以是多张图片")

        self.assertEqual(decision["intent"], "guide")

    def test_ask_command_with_rich_media_request_routes_to_guide(self):
        decision = classify("/ask 杭州三日游攻略，图文结合")

        self.assertEqual(decision["intent"], "guide")
        self.assertEqual(decision["source"], "command")

    def test_drawing_still_unavailable(self):
        self.assertEqual(classify("画一只猫")["action"], "unavailable")

    def test_poster_takes_precedence(self):
        self.assertEqual(classify("做一个杭州旅游海报")["intent"], "poster")

    def test_plain_question_still_ask(self):
        self.assertEqual(classify("1+1 等于几")["intent"], "ask")

    def test_guide_contract_registered(self):
        _spec, params, error = check_contract("guide", {"topic": "杭州三日游，图文结合"})

        self.assertEqual(error, "")
        self.assertEqual(params["topic"], "杭州三日游，图文结合")


class MakeGuideCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self._environment = patch.dict(os.environ, {"PA_SEARCH_API_KEY": ""}, clear=False)
        self._environment.start()
        self.addCleanup(self._environment.stop)
        self._photo_fetch = patch("src.qq.photos.fetch_photo", return_value=None)
        self.fetch_photo = self._photo_fetch.start()
        self.addCleanup(self._photo_fetch.stop)

    def test_make_guide_renders_each_page(self):
        provider = FakeProvider()

        summary, items = make_guide(
            "杭州三日游攻略，图文结合",
            "m1",
            provider=provider,
            root=self.root,
            renderer=fake_renderer,
        )

        self.assertEqual(summary, "三天行程摘要")
        self.assertEqual(len(items), 2)
        self.assertIn("杭州三日游攻略", provider.prompts[0])
        self.assertIn("photo_query 必填", provider.prompts[0])

        for index, (png_path, caption) in enumerate(items, start=1):
            self.assertTrue(png_path.is_file())
            self.assertEqual(png_path.name, f"page-{index}.png")
            self.assertIn(f"第 {index} 天", caption)

    def test_missing_photo_query_uses_page_and_guide_title(self):
        provider = FakeProvider(
            plan={
                "title": "杭州三日游",
                "summary": "摘要",
                "pages": [
                    {"title": "西湖断桥", "caption": "环湖", "content": "断桥、白堤"}
                ],
            }
        )

        make_guide(
            "杭州三日游攻略，图文结合", "m1-photo-fallback",
            provider=provider, root=self.root, renderer=fake_renderer,
        )

        self.fetch_photo.assert_called_once()
        query = self.fetch_photo.call_args.args[0]
        self.assertEqual(query, "杭州三日游 西湖断桥")

    def test_plan_json_with_code_fence_is_accepted(self):
        provider = FakeProvider()
        original_submit = provider.submit

        def submit(prompt, *, artifact_name, out_dir):
            result = original_submit(prompt, artifact_name=artifact_name, out_dir=out_dir)

            if artifact_name == "plan.json":
                Path(result["artifact"]).write_text(
                    "```json\n" + json.dumps(PLAN, ensure_ascii=False) + "\n```",
                    encoding="utf-8",
                )

            return result

        provider.submit = submit

        _summary, items = make_guide(
            "杭州三日游攻略，图文结合", "m2",
            provider=provider, root=self.root, renderer=fake_renderer,
        )

        self.assertEqual(len(items), 2)

    def test_missing_topic_rejected(self):
        provider = FakeProvider(plan={"error": "missing_topic"})

        with self.assertRaises(GuideError) as raised:
            make_guide(
                "图文结合的形式，可以是多张图片", "m3",
                provider=provider, root=self.root, renderer=fake_renderer,
            )

        self.assertIn("主题", str(raised.exception))

    def test_empty_html_rejected(self):
        provider = FakeProvider()
        original_submit = provider.submit

        def submit(prompt, *, artifact_name, out_dir):
            result = original_submit(prompt, artifact_name=artifact_name, out_dir=out_dir)

            if artifact_name != "plan.json":
                Path(result["artifact"]).write_text("不是 HTML", encoding="utf-8")

            return result

        provider.submit = submit

        with self.assertRaises(GuideError):
            make_guide(
                "杭州三日游攻略，图文结合", "m4",
                provider=provider, root=self.root, renderer=fake_renderer,
            )

    @staticmethod
    def _photo_plan():
        return {
            "title": "杭州",
            "summary": "摘要",
            "pages": [
                {
                    "title": "第 1 天",
                    "caption": "西湖",
                    "content": "断桥、白堤",
                    "photo_query": "West Lake",
                }
            ],
        }

    def test_page_with_photo_uses_local_file_and_credit(self):
        photo = self.root / "photo-1.jpg"
        photo.write_bytes(b"jpg")
        provider = FakeProvider(plan=self._photo_plan())

        with patch(
            "src.qq.photos.fetch_photo",
            return_value=(photo, "Wikimedia Commons／Tester"),
        ) as mocked:
            _summary, items = make_guide(
                "杭州攻略，图文结合", "m5",
                provider=provider, root=self.root, renderer=fake_renderer,
            )

        mocked.assert_called_once()
        prompt = provider.prompts[1]
        self.assertIn('<img src="photo-1.jpg">', prompt)
        self.assertIn("照片：Wikimedia Commons／Tester", prompt)
        self.assertNotIn("虚线框", prompt)
        self.assertEqual(len(items), 1)

    def test_pages_share_photo_deduplication_state(self):
        plan = self._photo_plan()
        plan["pages"].append(dict(plan["pages"][0], title="第 2 天"))
        provider = FakeProvider(plan=plan)
        seen_values = []

        def fetch(query, target, *, seen):
            seen_values.append(seen)
            seen.add(query)
            return None

        with patch("src.qq.photos.fetch_photo", side_effect=fetch):
            make_guide(
                "杭州攻略，图文结合", "m5-dedup",
                provider=provider, root=self.root, renderer=fake_renderer,
            )

        self.assertEqual(len(seen_values), 2)
        self.assertIs(seen_values[0], seen_values[1])
        self.assertEqual(seen_values[0], {"West Lake"})

    def test_photo_failure_falls_back_to_placeholder(self):
        provider = FakeProvider(plan=self._photo_plan())

        with patch("src.qq.photos.fetch_photo", return_value=None):
            make_guide(
                "杭州攻略，图文结合", "m6",
                provider=provider, root=self.root, renderer=fake_renderer,
            )

        prompt = provider.prompts[1]
        self.assertIn("虚线框", prompt)
        self.assertNotIn('<img src="photo-1.jpg">', prompt)

    def test_photos_disabled_skips_fetch(self):
        provider = FakeProvider(plan=self._photo_plan())

        with patch.dict(os.environ, {"PA_GUIDE_PHOTOS": "0"}, clear=False):
            with patch("src.qq.photos.fetch_photo") as mocked:
                make_guide(
                    "杭州攻略，图文结合", "m7",
                    provider=provider, root=self.root, renderer=fake_renderer,
                )

        mocked.assert_not_called()
        self.assertIn("虚线框", provider.prompts[1])


class FakeClient:
    def __init__(self):
        self.messages = []
        self.images = []
        self.message_seqs = []
        self.image_seqs = []

    def send_private_msg(self, user_id, text, **kwargs):
        self.messages.append((user_id, text))
        self.message_seqs.append(kwargs.get("msg_seq"))

        return {"message_id": 1}

    def send_private_image(self, user_id, file_path, caption='', **kwargs):
        self.images.append((user_id, file_path))
        self.image_seqs.append(kwargs.get("msg_seq"))

        if caption:
            self.messages.append((user_id, caption))

        return {"message_id": 2}

    def send_rich_items(self, user_id, items, **kwargs):
        sent = 0
        start_seq = int(kwargs.get("msg_seq") or 1)

        try:
            for offset, item in enumerate(list(items or [])):
                if offset and kwargs.get("pause"):
                    kwargs["pause"](offset)

                if str(item[0]) == "image":
                    self.send_private_image(
                        user_id,
                        item[1],
                        item[2] if len(item) > 2 else "",
                        msg_id=kwargs.get("msg_id") or "",
                        msg_seq=start_seq + offset,
                    )
                else:
                    self.send_private_msg(
                        user_id,
                        item[1],
                        msg_id=kwargs.get("msg_id") or "",
                        msg_seq=start_seq + offset,
                    )

                sent += 1
        except QQTransportError as error:
            error.sent_count = sent

            raise


class WorkerGuideCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self._environment = patch.dict(
            os.environ,
            {"PERSONAL_AGENT_DATA_DIR": str(self.root), "PA_SEARCH_API_KEY": ""},
            clear=False,
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)
        self.outbox = QQOutbox(self.root / "outbox.db")
        self.client = FakeClient()
        self.handler = QQInbound(owner_id=7, secret="s", client=self.client)
        self._pacing = patch("src.qq.worker.chunk_pause", return_value=0.0)
        self._pacing.start()
        self.addCleanup(self._pacing.stop)
        self.outbox.enqueue(
            message_id="g1", sender=7,
            raw_text="杭州三日游攻略，图文结合", queue="task",
        )
        self.outbox.set_routing(
            "g1", intent="guide", params={"topic": "杭州三日游攻略，图文结合"},
            params_hash="h", decision_source="rule", state="pending_task",
        )

    def run_worker(self):
        TaskWorker(
            outbox=self.outbox, client=self.client, handler=self.handler
        ).run_once()

    def test_worker_sends_summary_images_and_captions(self):
        png = self.root / "page-1.png"
        png.write_bytes(b"png")

        with patch(
            "src.qq.guide.make_guide",
            return_value=("三天行程摘要", [(png, "第 1 天\n西湖环湖")]),
        ), patch("src.collab.providers.provider_name", return_value="ark"):
            self.run_worker()

        texts = [text for _user, text in self.client.messages]
        self.assertIn("图文长图生成中，可能需要 1-2 分钟。", texts[0])
        self.assertIn("三天行程摘要", texts)
        self.assertIn("第 1 天\n西湖环湖", texts)
        self.assertEqual(self.client.images, [(7, str(png))])
        self.assertEqual(self.client.message_seqs, [1, 2])
        self.assertEqual(self.client.image_seqs, [3])

        row = self.outbox.get("g1")
        self.assertEqual(row["state"], "sent")
        self.assertIn("[图片]", row["reply"])

    def test_guide_failure_replies_with_reason(self):
        with patch(
            "src.qq.guide.make_guide",
            side_effect=GuideError("还没看出主题"),
        ), patch("src.collab.providers.provider_name", return_value="ark"):
            self.run_worker()

        texts = [text for _user, text in self.client.messages]
        self.assertTrue(any("图文生成失败" in text for text in texts))
        self.assertEqual(self.client.images, [])


if __name__ == "__main__":
    unittest.main()
