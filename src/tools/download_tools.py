"""P3：经审批把 HTTP(S) 数据集完整下载为当前用户的产物。

不接受输出路径；文件写入平台授予的产物根目录。默认最多 20 MiB，硬上限
50 MiB。无论响应头声明超限还是流式读取实际超限，都删除半成品并报错，
绝不把截断内容当成完整数据集返回。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import urllib.error
import urllib.request
import uuid

from pathlib import Path
from urllib.parse import unquote, urlsplit

from src.artifacts.registry import ArtifactRegistry
from src.core.reminder_identity import current_identity


DEFAULT_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 20
DOWNLOAD_CHUNK_BYTES = 64 * 1024
MAX_URL_CHARS = 2048
_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


HTTP_DOWNLOAD_TOOL = {
    "type": "function",
    "function": {
        "name": "http_download",
        "description": (
            "把 HTTP(S) URL 的数据集完整下载为产物，返回文件大小、SHA256 和下载地址。"
            "默认限制 20 MiB，max_bytes 最大可设为 50 MiB；超限会失败而不是截断。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "数据集的 HTTP(S) 直链"},
                "max_bytes": {
                    "type": "integer",
                    "description": "允许的最大字节数，1..52428800，默认 20971520",
                },
            },
            "required": ["url"],
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


def _url(args: dict) -> str:
    url = args.get("url")

    if not isinstance(url, str) or not url.strip():
        raise ValueError("url 不能为空")

    url = url.strip()

    if len(url) > MAX_URL_CHARS:
        raise ValueError("url 太长")

    parsed = urlsplit(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("只支持 http / https URL")

    return url


def _limit(args: dict) -> int:
    value = args.get("max_bytes", DEFAULT_MAX_DOWNLOAD_BYTES)

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_bytes 必须是整数")

    if not 1 <= value <= MAX_DOWNLOAD_BYTES:
        raise ValueError(f"max_bytes 必须在 1..{MAX_DOWNLOAD_BYTES}")

    return value


def _suffix(url: str) -> str:
    suffix = Path(urlsplit(url).path).suffix
    return suffix if _SUFFIX.fullmatch(suffix) else ".bin"


def _title(url: str) -> str:
    name = unquote(Path(urlsplit(url).path).name) or "dataset-download"
    return f"数据集：{name}"[:200]


def http_download(tool_call, cancel_token=None) -> dict:
    args = _arguments(tool_call)
    url = _url(args)
    max_bytes = _limit(args)
    identity = current_identity()

    if identity is None:
        raise PermissionError("没有经过认证的用户身份，拒绝下载数据集")

    if cancel_token is not None:
        cancel_token.check()

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "personal-agent-tier1/1.0",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    registry = ArtifactRegistry()
    destination = registry.root / f"download-{uuid.uuid4().hex}{_suffix(url)}"
    digest = hashlib.sha256()
    size = 0

    try:
        with urllib.request.urlopen(
            request, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            status = int(response.status)
            final_url = response.geturl()
            encoding = response.headers.get("Content-Encoding", "").strip().lower()

            if encoding not in ("", "identity"):
                raise ValueError("暂不支持压缩编码响应，请使用原始文件直链")

            raw_length = response.headers.get("Content-Length")

            if raw_length is not None:
                try:
                    declared = int(raw_length)
                except (TypeError, ValueError):
                    declared = -1

                if declared > max_bytes:
                    raise ValueError(f"下载文件超过大小上限 {max_bytes} 字节")

            with destination.open("xb") as handle:
                while True:
                    if cancel_token is not None:
                        cancel_token.check()

                    chunk = response.read(min(
                        DOWNLOAD_CHUNK_BYTES, max_bytes - size + 1
                    ))

                    if not chunk:
                        break

                    size += len(chunk)

                    if size > max_bytes:
                        raise ValueError(f"下载文件超过大小上限 {max_bytes} 字节")

                    digest.update(chunk)
                    handle.write(chunk)

            media_type = response.headers.get_content_type()
    except ValueError:
        destination.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        destination.unlink(missing_ok=True)
        raise ValueError(f"数据下载失败：{error}") from None

    if cancel_token is not None:
        cancel_token.check()

    try:
        artifact = registry.register(
            identity.user_id,
            destination,
            title=_title(final_url),
            media_type=media_type,
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    return {
        "success": True,
        "url": url,
        "final_url": final_url,
        "status": status,
        "artifact_id": artifact.id,
        "download_url": f"/api/artifacts/{artifact.id}/content",
        "artifact_preview_url": f"/api/artifacts/{artifact.id}/preview",
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "max_bytes": max_bytes,
        "sha256": digest.hexdigest(),
    }


TOOL_DEFINITIONS = (
    (HTTP_DOWNLOAD_TOOL, "ask", http_download),
)

MODEL_VISIBLE_TOOLS = (
    HTTP_DOWNLOAD_TOOL,
)
