"""本地 Skill 发现、启停、提示注入与数据工作流引用解析。"""

import json
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src import agent
from src.agent_stream import _initial_messages, _resolve_workflow_reference
from src.skill_system import SkillConfigError, SkillRegistry


VALID = """---
name: data-analysis
description: 分析 CSV 并生成报告
enabled: true
triggers: [数据分析, CSV]
---
先下载，再使用真实产物编号汇总。
"""


def tool_call(name: str, arguments: dict):
    return SimpleNamespace(function=SimpleNamespace(
        name=name, arguments=json.dumps(arguments)
    ))


class SkillSystemTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "skills"
        self.skill = self.root / "data-analysis"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(VALID, encoding="utf-8")

    def test_discovers_local_skill_and_injects_only_relevant_body(self):
        registry = SkillRegistry(self.root)
        self.assertEqual(list(registry.discover()), ["data-analysis"])

        selected = registry.prompt_for("请做数据分析")
        self.assertIn("先下载，再使用真实产物编号汇总", selected)
        unrelated = registry.prompt_for("今天天气如何")
        self.assertIsNone(unrelated)
        catalog = registry.prompt_for("有哪些技能")
        self.assertIn("data-analysis", catalog)
        self.assertNotIn("先下载，再使用真实产物编号汇总", catalog)

    def test_frontmatter_and_environment_can_disable_skill(self):
        path = self.skill / "SKILL.md"
        path.write_text(VALID.replace("enabled: true", "enabled: false"), encoding="utf-8")
        self.assertEqual(SkillRegistry(self.root).discover(), {})

        path.write_text(VALID, encoding="utf-8")
        with patch.dict("os.environ", {
            "PERSONAL_AGENT_DISABLED_SKILLS": "data-analysis",
        }, clear=False):
            self.assertEqual(SkillRegistry(self.root).discover(), {})

    def test_invalid_skill_has_explicit_error(self):
        path = self.skill / "SKILL.md"
        for content in (
            "没有 frontmatter",
            VALID.replace("name: data-analysis", "name: other"),
            VALID.replace("enabled: true", "enabled: yes please"),
            VALID.replace("description: 分析 CSV 并生成报告", "description: 1"),
        ):
            with self.subTest(content=content[:40]):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(SkillConfigError):
                    SkillRegistry(self.root).discover()

    def test_new_skill_is_used_in_chat_without_core_changes(self):
        registry = SkillRegistry(self.root)
        with (
            patch.object(agent, "skill_registry", registry),
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(agent, "_work_memory_context", return_value=None),
        ):
            messages, _ = _initial_messages("使用数据分析技能处理 CSV", None)

        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-2]["role"], "system")
        self.assertIn("技能：data-analysis", messages[-2]["content"])
        self.assertIn("先下载，再使用真实产物编号汇总", messages[-2]["content"])

    def test_workflow_reference_uses_real_prior_tool_result(self):
        download = tool_call("csv_summarize", {
            "artifact_id": "$http_download.artifact_id",
        })
        history = [{
            "name": "http_download",
            "result": {"success": True, "artifact_id": "art_real_download"},
        }]
        _resolve_workflow_reference(download, history)
        self.assertEqual(
            json.loads(download.function.arguments)["artifact_id"],
            "art_real_download",
        )

        preview = tool_call("browser_preview", {
            "artifact_id": "$csv_summarize.report_artifact_id",
        })
        history.append({
            "name": "csv_summarize",
            "result": {"success": True, "report_artifact_id": "art_real_report"},
        })
        _resolve_workflow_reference(preview, history)
        self.assertEqual(
            json.loads(preview.function.arguments)["artifact_id"],
            "art_real_report",
        )

    def test_missing_workflow_dependency_fails_before_execution(self):
        call = tool_call("csv_summarize", {
            "artifact_id": "$http_download.artifact_id",
        })
        with self.assertRaisesRegex(ValueError, "尚未执行 http_download"):
            _resolve_workflow_reference(call, [])


if __name__ == "__main__":
    unittest.main()
