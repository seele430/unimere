"""P2 浏览器预览：参数边界、真实产物登记与失败清理。"""

import json
import os
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.artifacts.registry import ArtifactRegistry
from src.core.reminder_identity import bind_authenticated_reminder
from src.tools import browser_tools
from tests.helper import isolate_paths


PNG = b"\x89PNG\r\n\x1a\n" + b"fake-browser-image"


def call(arguments: dict):
    return SimpleNamespace(
        function=SimpleNamespace(arguments=json.dumps(arguments))
    )


class BrowserPreviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        previous = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, previous)

    def launcher(self, text="Rendered page"):
        page = MagicMock()
        page.url = "https://example.test/final"
        page.title.return_value = "Example title"
        page.locator.return_value.inner_text.return_value = text
        page.screenshot.side_effect = lambda path, **kwargs: Path(path).write_bytes(PNG)

        browser = MagicMock()
        browser.new_page.return_value = page
        playwright = MagicMock()
        playwright.chromium.launch.return_value = browser
        manager = MagicMock()
        manager.__enter__.return_value = playwright
        manager.__exit__.return_value = False
        launcher = MagicMock(return_value=manager)
        return launcher, page, browser

    def invoke(self, arguments: dict):
        with bind_authenticated_reminder(
            user_id="alice", request_id="p2_browser", role="member"
        ):
            return browser_tools.browser_preview(call(arguments))

    def test_url_capture_registers_owned_image_artifact(self):
        launcher, page, browser = self.launcher()

        with patch.object(browser_tools, "sync_playwright", launcher):
            result = self.invoke({
                "url": "https://example.test/start",
                "width": 1024,
                "height": 768,
                "wait_ms": 10,
            })

        self.assertTrue(result["success"])
        self.assertEqual(result["title"], "Example title")
        self.assertEqual(result["text"], "Rendered page")
        self.assertTrue(result["artifact_id"].startswith("art_"))
        self.assertIn("?inline=1", result["image_url"])
        page.goto.assert_called_once_with(
            "https://example.test/start",
            wait_until="domcontentloaded",
            timeout=browser_tools.NAVIGATION_TIMEOUT_MS,
        )
        page.wait_for_timeout.assert_called_once_with(10)
        browser.close.assert_called_once_with()

        registry = ArtifactRegistry()
        artifact = registry.get(result["artifact_id"], owner_id="alice")
        self.assertEqual(artifact.kind, "image")
        self.assertEqual(artifact.media_type, "image/png")
        self.assertEqual(registry.absolute_path(
            artifact.id, owner_id="alice"
        ).read_bytes(), PNG)

    def test_workspace_html_uses_file_uri_without_accepting_output_path(self):
        (self.workspace / "report.html").write_text(
            "<html><body>local report</body></html>", encoding="utf-8"
        )
        launcher, page, _ = self.launcher("local report")

        with patch.object(browser_tools, "sync_playwright", launcher):
            result = self.invoke({"path": "report.html", "full_page": True})

        self.assertEqual(result["source"], "report.html")
        self.assertFalse(result["clicked"])
        self.assertTrue(page.goto.call_args.args[0].startswith("file:///"))
        self.assertTrue(page.screenshot.call_args.kwargs["full_page"])

    def test_click_selector_changes_page_before_capture(self):
        (self.workspace / "report.html").write_text(
            '<html><body><button id="run">运行</button></body></html>',
            encoding="utf-8",
        )
        launcher, page, _ = self.launcher("已运行 1 次")

        with patch.object(browser_tools, "sync_playwright", launcher):
            result = self.invoke({
                "path": "report.html", "click_selector": "  #run  ",
            })

        self.assertTrue(result["success"])
        self.assertTrue(result["clicked"])
        self.assertEqual(result["text"], "已运行 1 次")
        page.locator.assert_any_call("#run")
        page.locator.return_value.click.assert_called_once_with(timeout=3000)
        page.locator.assert_any_call("body")

    def test_invalid_click_selector_is_rejected_before_browser_launch(self):
        (self.workspace / "report.html").write_text(
            "<html><body>local report</body></html>", encoding="utf-8"
        )
        launcher, _, _ = self.launcher()

        with patch.object(browser_tools, "sync_playwright", launcher):
            for selector in ("", "   ", "x" * 501, 123):
                with self.subTest(selector=selector):
                    with self.assertRaises(ValueError):
                        self.invoke({
                            "path": "report.html", "click_selector": selector,
                        })

        launcher.assert_not_called()

    def test_url_and_path_are_mutually_exclusive(self):
        launcher, _, _ = self.launcher()

        with patch.object(browser_tools, "sync_playwright", launcher):
            for arguments in ({}, {"url": "https://example.test", "path": "a.html"}):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        self.invoke(arguments)

        launcher.assert_not_called()

    def test_local_path_must_be_workspace_html(self):
        (self.workspace / "notes.txt").write_text("not html", encoding="utf-8")
        launcher, _, _ = self.launcher()

        with patch.object(browser_tools, "sync_playwright", launcher):
            for arguments in ({"path": "../outside.html"}, {"path": "notes.txt"}):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        self.invoke(arguments)

        launcher.assert_not_called()

    def test_non_http_url_is_rejected_before_browser_launch(self):
        launcher, _, _ = self.launcher()

        with patch.object(browser_tools, "sync_playwright", launcher):
            with self.assertRaises(ValueError):
                self.invoke({"url": "file:///C:/Windows/win.ini"})

        launcher.assert_not_called()

    def test_html_artifact_can_be_rendered_with_owner_check(self):
        registry = ArtifactRegistry()
        path = registry.root / "report.html"
        path.write_text("<html><body>artifact report</body></html>", encoding="utf-8")
        artifact = registry.register(
            "alice", path, title="报告", media_type="text/html"
        )
        launcher, page, _ = self.launcher("artifact report")

        with patch.object(browser_tools, "sync_playwright", launcher):
            result = self.invoke({"artifact_id": artifact.id})

        self.assertEqual(result["source"], f"artifact:{artifact.id}")
        self.assertTrue(page.goto.call_args.args[0].startswith("file:///"))
        self.assertIn("artifact report", result["text"])

    def test_other_users_html_artifact_is_rejected_before_browser_launch(self):
        registry = ArtifactRegistry()
        path = registry.root / "private.html"
        path.write_text("<html><body>private</body></html>", encoding="utf-8")
        artifact = registry.register(
            "bob", path, title="私有报告", media_type="text/html"
        )
        launcher, _, _ = self.launcher()

        with patch.object(browser_tools, "sync_playwright", launcher):
            with self.assertRaises(Exception):
                self.invoke({"artifact_id": artifact.id})

        launcher.assert_not_called()

    def test_authenticated_identity_is_required(self):
        launcher, _, _ = self.launcher()

        with patch.object(browser_tools, "sync_playwright", launcher):
            with self.assertRaises(PermissionError):
                browser_tools.browser_preview(call({"url": "https://example.test"}))

        launcher.assert_not_called()

    def test_missing_playwright_has_actionable_error(self):
        with patch.object(browser_tools, "sync_playwright", None):
            with self.assertRaisesRegex(ValueError, "playwright install chromium"):
                self.invoke({"url": "https://example.test"})

    def test_browser_failure_leaves_no_image_or_artifact(self):
        launcher, page, _ = self.launcher()
        page.screenshot.side_effect = RuntimeError("simulated screenshot failure")

        with patch.object(browser_tools, "sync_playwright", launcher):
            with self.assertRaisesRegex(ValueError, "simulated screenshot failure"):
                self.invoke({"url": "https://example.test"})

        registry = ArtifactRegistry()
        self.assertEqual(registry.count("alice"), 0)
        self.assertEqual(list(registry.root.glob("browser-*.png")), [])

    def test_browser_path_is_forwarded_without_pythonpath(self):
        from src.platform.exec_service import platform_environment

        with patch.dict("os.environ", {
            "PLAYWRIGHT_BROWSERS_PATH": "C:\\browser-cache",
            "PYTHONPATH": "C:\\must-not-leak",
        }, clear=False):
            environment = platform_environment()

        self.assertEqual(
            environment["PLAYWRIGHT_BROWSERS_PATH"], "C:\\browser-cache"
        )
        self.assertNotIn("PYTHONPATH", environment)


if __name__ == "__main__":
    unittest.main()
