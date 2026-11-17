"""平台工作记忆：真实执行落库、去重、开关与相关召回。"""

import os
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.memory import service, work
from src.memory.store import MemoryStore


class WorkMemoryCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = MemoryStore(Path(self._temporary.name) / "memory.db")
        self.store_patch = patch(
            "src.memory.service.get_memory_store", return_value=self.store
        )
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.env_patch = patch.dict(
            os.environ, {work.WORK_MEMORY_ENV: "1"}, clear=False
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def record(self, *, run_id="run_1", goal="创建 todo.html"):
        return work.record_run(
            "u1",
            run_id,
            goal,
            "succeeded",
            [{
                "name": "write_file",
                "result": {
                    "success": True,
                    "path": "todo.html",
                    "bytes_written": 123,
                    "content": "不应写入记忆的正文",
                },
            }],
        )

    def test_successful_work_is_platform_verified_episode(self):
        item = self.record()
        self.assertEqual(item.kind, "episode")
        self.assertEqual(item.layer, "project")
        self.assertEqual(item.trust, "platform_verified")
        self.assertEqual(item.source_ref, "run:run_1")
        self.assertEqual(item.key, "run:run_1")
        self.assertIn("todo.html", item.text)
        self.assertIn("不代表文件仍然存在", item.text)
        self.assertNotIn("不应写入记忆的正文", item.text)

    def test_same_run_has_one_current_memory(self):
        first = self.record()
        second = self.record()
        self.assertEqual(second.supersedes_id, first.id)
        listing = service.list_current("u1")
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["id"], second.id)

    def test_plain_chat_and_memory_tools_are_not_recorded(self):
        self.assertIsNone(work.record_run("u1", "run_plain", "你好", "succeeded", []))
        self.assertIsNone(work.record_run(
            "u1", "run_memory", "记住偏好", "succeeded",
            [{"name": "memory_remember", "result": {"status": "remembered"}}],
        ))
        self.assertEqual(service.list_current("u1")["total"], 0)

    def test_keyword_recall_returns_work_context(self):
        self.record()
        context = work.recall_context("u1", "todo.html 在哪里")
        self.assertIn("创建 todo.html", context)
        self.assertIn("历史路径不代表文件仍可访问", context)

    def test_history_question_falls_back_to_recent_work(self):
        self.record(goal="生成季度财报")
        context = work.recall_context("u1", "上次做了什么")
        self.assertIn("生成季度财报", context)

    def test_unrelated_question_does_not_inject_recent_work(self):
        self.record()
        self.assertIsNone(work.recall_context("u1", "今天天气怎么样"))

    def test_owner_isolation(self):
        self.record()
        self.assertIsNone(work.recall_context("u2", "todo.html"))

    def test_durable_artifact_ids_and_links_are_recorded(self):
        item = work.record_run(
            "u1", "run_artifacts", "下载并汇总 CSV", "succeeded",
            [
                {"name": "http_download", "result": {
                    "success": True,
                    "artifact_id": "art_download",
                    "download_url": "/api/artifacts/art_download/content",
                }},
                {"name": "csv_summarize", "result": {
                    "success": True,
                    "summary_artifact_id": "art_summary",
                    "report_artifact_id": "art_report",
                    "report_url": "/api/artifacts/art_report/content",
                }},
                {"name": "browser_preview", "result": {
                    "success": True,
                    "artifact_id": "art_image",
                    "image_url": "/api/artifacts/art_image/content?inline=1",
                }},
            ],
        )

        self.assertIn("art_download", item.text)
        self.assertIn("art_summary", item.text)
        self.assertIn("art_report", item.text)
        self.assertIn("art_image", item.text)
        self.assertIn("截图地址=", item.text)

    def test_disable_switch_stops_write_and_recall(self):
        with patch.dict(os.environ, {work.WORK_MEMORY_ENV: "0"}):
            self.assertIsNone(self.record())
            self.assertIsNone(work.recall_context("u1", "上次做了什么"))
        self.assertEqual(service.list_current("u1")["total"], 0)


if __name__ == "__main__":
    unittest.main()
