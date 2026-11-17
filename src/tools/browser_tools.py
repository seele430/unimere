"""P2：经审批使用 Playwright 打开页面并生成可预览的 PNG 产物。

handler 只在 tier1 runner 中执行。网页 URL 只接受 HTTP(S)；本地预览只接受
当前沙箱工作区内的 HTML 文件。截图写入平台显式授予的产物目录，并按当前
认证用户登记，不接受模型传入产物路径。
"""

from __future__ import annotations

import json
import uuid

from pathlib import Path
from urllib.parse import urlsplit

from src.artifacts.registry import ArtifactRegistry
from src.core.reminder_identity import current_identity
from src.tools.sandbox_tools import resolve_in_workspace

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # 未安装可选浏览器依赖时，其他工具仍可使用。
    sync_playwright = None


MAX_URL_CHARS = 2048
MAX_TEXT_CHARS = 4000
NAVIGATION_TIMEOUT_MS = 15_000
SCREENSHOT_TIMEOUT_MS = 15_000
MAX_WAIT_MS = 5000


BROWSER_PREVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "browser_preview",
        "description": (
            "用 Playwright 打开一个 HTTP(S) 网页、当前用户的 HTML 产物，"
            "或工作区内的 HTML 文件，返回标题、正文摘录和可预览的 PNG 截图产物。"
            "url、path 与 artifact_id 三选一。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "HTTP(S) 网页地址"},
                "path": {"type": "string", "description": "工作区内 HTML 文件的相对路径"},
                "artifact_id": {"type": "string", "description": "当前用户的 HTML 产物编号"},
                "click_selector": {"type": "string", "description": "截图前可选点击的 CSS 选择器，最多 500 字符"},
                "full_page": {"type": "boolean", "description": "是否截取完整页面，默认 false"},
                "width": {"type": "integer", "description": "视口宽度，320..2560，默认 1280"},
                "height": {"type": "integer", "description": "视口高度，240..2160，默认 720"},
                "wait_ms": {"type": "integer", "description": "加载后额外等待毫秒数，0..5000"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}


def _arguments(tool_call) -> dict:
    raw = getattr(tool_call.function, "arguments", "{}") or "{}"
    arguments = json.loads(raw)

    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是对象")

    return arguments


def _integer(args: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    value = args.get(name, default)

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")

    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum}..{maximum}")

    return value


def _source(args: dict) -> tuple[str, str]:
    raw_url = args.get("url")
    raw_path = args.get("path")
    raw_artifact = args.get("artifact_id")
    sources = (raw_url, raw_path, raw_artifact)

    if sum(value is not None for value in sources) != 1:
        raise ValueError("url、path 与 artifact_id 必须且只能提供一个")

    if raw_url is not None:
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise ValueError("url 不能为空")

        url = raw_url.strip()

        if len(url) > MAX_URL_CHARS:
            raise ValueError("url 太长")

        parsed = urlsplit(url)

        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("只支持 http / https URL")

        return url, url

    if raw_artifact is not None:
        if not isinstance(raw_artifact, str) or not raw_artifact.strip():
            raise ValueError("artifact_id 不能为空")
        identity = current_identity()
        if identity is None:
            raise PermissionError("没有经过认证的用户身份，拒绝读取 HTML 产物")
        registry = ArtifactRegistry()
        artifact = registry.get(raw_artifact.strip(), owner_id=identity.user_id)
        if artifact.media_type != "text/html":
            raise ValueError("产物预览只支持 HTML 文件")
        path = registry.absolute_path(artifact.id, owner_id=identity.user_id)
        return path.as_uri(), f"artifact:{artifact.id}"

    path = resolve_in_workspace(raw_path)

    if path.suffix.lower() not in (".html", ".htm"):
        raise ValueError("本地预览只支持 HTML 文件")

    if not path.is_file():
        raise ValueError("HTML 文件不存在")

    relative = path.relative_to(resolve_in_workspace(".")).as_posix()
    return path.as_uri(), relative


def _error_message(error: Exception) -> str:
    text = " ".join(str(error).split())
    return text[:500] or type(error).__name__


def browser_preview(tool_call, cancel_token=None) -> dict:
    args = _arguments(tool_call)
    target, source = _source(args)
    width = _integer(args, "width", 1280, 320, 2560)
    height = _integer(args, "height", 720, 240, 2160)
    wait_ms = _integer(args, "wait_ms", 0, 0, MAX_WAIT_MS)
    full_page = args.get("full_page", False)
    click_selector = args.get("click_selector")

    if not isinstance(full_page, bool):
        raise ValueError("full_page 必须是布尔值")
    if click_selector is not None:
        if not isinstance(click_selector, str) or not click_selector.strip():
            raise ValueError("click_selector 必须是非空文本")
        click_selector = click_selector.strip()
        if len(click_selector) > 500:
            raise ValueError("click_selector 不能超过 500 字符")

    identity = current_identity()

    if identity is None:
        raise PermissionError("没有经过认证的用户身份，拒绝生成网页截图")

    if sync_playwright is None:
        raise ValueError(
            "缺少 Playwright：请安装 requirements.txt 后执行 "
            "python -m playwright install chromium"
        )

    if cancel_token is not None:
        cancel_token.check()

    registry = ArtifactRegistry()
    image_path = registry.root / f"browser-{uuid.uuid4().hex}.png"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": height}
                )
                page.goto(
                    target,
                    wait_until="domcontentloaded",
                    timeout=NAVIGATION_TIMEOUT_MS,
                )
                if click_selector is not None:
                    page.locator(click_selector).click(timeout=3000)

                if wait_ms:
                    page.wait_for_timeout(wait_ms)

                title = page.title()
                body_text = page.locator("body").inner_text(timeout=3000)
                page.screenshot(
                    path=str(image_path),
                    full_page=full_page,
                    animations="disabled",
                    timeout=SCREENSHOT_TIMEOUT_MS,
                )
                final_url = page.url
            finally:
                browser.close()
    except Exception as error:  # Playwright 的异常类型随启动 / 导航阶段变化。
        image_path.unlink(missing_ok=True)
        raise ValueError(f"浏览器预览失败：{_error_message(error)}") from None

    if cancel_token is not None:
        cancel_token.check()

    try:
        artifact = registry.register(
            identity.user_id,
            image_path,
            title=f"网页截图：{title or source}"[:200],
            media_type="image/png",
        )
    except Exception:
        image_path.unlink(missing_ok=True)
        raise

    return {
        "success": True,
        "source": source,
        "final_url": final_url,
        "title": title,
        "text": body_text[:MAX_TEXT_CHARS],
        "text_truncated": len(body_text) > MAX_TEXT_CHARS,
        "clicked": click_selector is not None,
        "artifact_id": artifact.id,
        "image_url": f"/api/artifacts/{artifact.id}/content?inline=1",
        "artifact_preview_url": f"/api/artifacts/{artifact.id}/preview",
        "size_bytes": artifact.size_bytes,
    }


TOOL_DEFINITIONS = (
    (BROWSER_PREVIEW_TOOL, "ask", browser_preview),
)

MODEL_VISIBLE_TOOLS = (
    BROWSER_PREVIEW_TOOL,
)
