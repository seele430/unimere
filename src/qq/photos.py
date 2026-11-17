"""真实照片检索：Wikimedia Commons（开放许可、无需 API key）。

边界（写死）：

- ``PA_GUIDE_PHOTOS=0`` 可整体关闭；默认启用但**任何失败都返回 None 降级**，
  绝不阻塞长图出图；
- 每次检索只取 1 张，API 与下载各 10 秒超时；
- 只接受 ``image/*`` 且不超过 8 MB；扩展名由 Content-Type 决定，不信 URL；
- 返回 (本地图片路径, 署名信息)，署名来自 Commons 作者与许可字段。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path


PHOTO_API = "https://commons.wikimedia.org/w/api.php"
TIMEOUT_SECONDS = 10.0
MAX_PHOTO_BYTES = 8 * 1024 * 1024
USER_AGENT = "personal-agent-qq-guide/1.0 (Wikimedia Commons photo lookup)"

CONTENT_TYPE_SUFFIX = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def photos_enabled() -> bool:
    return (os.getenv("PA_GUIDE_PHOTOS") or "1").strip() != "0"


def _open(opener, request):
    return (opener or urllib.request.urlopen)(request, timeout=TIMEOUT_SECONDS)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _credit_of(info: dict) -> str:
    metadata = info.get("extmetadata") or {}
    artist = _strip_html(str((metadata.get("Artist") or {}).get("value") or ""))
    license_name = _strip_html(
        str((metadata.get("LicenseShortName") or {}).get("value") or "")
    )
    parts = ["Wikimedia Commons"]

    if artist:
        parts.append(artist[:60])

    if license_name:
        parts.append(license_name[:40])

    return "／".join(parts)[:120]


def fetch_photo(
    query: str, target_base: Path, *, opener=None, seen: set[str] | None = None
) -> tuple[Path, str] | None:
    """检索并下载 1 张相关且不重复的照片；失败返回 None。"""

    query = (query or "").strip()

    if not query:
        return None

    for name in _providers():
        fetcher = _fetch_bing if name == "bing" else _fetch_commons
        result = fetcher(query, Path(target_base), opener=opener, seen=seen)

        if result:
            return result

    return None


BING_SEARCH = "https://cn.bing.com/images/search"
BING_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
DEFAULT_PROVIDERS = ("bing", "commons")


def _providers() -> list[str]:
    """照片源顺序：默认 Bing（国内可达）优先，Commons 兜底。"""

    raw = (os.getenv("PA_PHOTO_PROVIDERS") or "").strip()

    if not raw:
        return list(DEFAULT_PROVIDERS)

    names = []

    for part in raw.split(","):
        name = part.strip().lower()

        if name in ("bing", "commons") and name not in names:
            names.append(name)

    return names or list(DEFAULT_PROVIDERS)


def _request(url: str, user_agent: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": user_agent})


def _download(
    image_url: str, target_base: Path, *, opener, user_agent: str, credit: str,
    seen: set[str] | None = None,
) -> tuple[Path, str] | None:
    """下载并校验图片；拒绝跨页重复 URL 与重复内容。"""

    url_key = "url:" + image_url

    if seen is not None and url_key in seen:
        return None

    try:
        with _open(opener, _request(image_url, user_agent)) as response:
            content_type = str(response.headers.get("Content-Type") or "")
            content_type = content_type.split(";")[0].strip().lower()
            suffix = CONTENT_TYPE_SUFFIX.get(content_type)

            if suffix is None:
                return None

            data = response.read(MAX_PHOTO_BYTES + 1)

        if not data or len(data) > MAX_PHOTO_BYTES:
            return None

        digest_key = "sha256:" + hashlib.sha256(data).hexdigest()

        if seen is not None and digest_key in seen:
            return None

        target = Path(target_base).with_suffix(suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

        if seen is not None:
            seen.update((url_key, digest_key))

        return target, credit
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _fetch_commons(
    query: str, target_base: Path, *, opener=None, seen: set[str] | None = None
) -> tuple[Path, str] | None:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": 1,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "iiurlwidth": 1200,
        }
    )

    try:
        with _open(opener, _request(f"{PHOTO_API}?{params}", USER_AGENT)) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None

    pages = (body.get("query") or {}).get("pages") or {}
    page = next(iter(pages.values()), {})
    info = (page.get("imageinfo") or [{}])[0]
    image_url = str(info.get("thumburl") or info.get("url") or "").strip()

    if not image_url:
        return None

    return _download(
        image_url, target_base,
        opener=opener, user_agent=USER_AGENT, credit=_credit_of(info), seen=seen,
    )


def _bing_items(html_text: str):
    """从 Bing 图片搜索页提取图片项（iusc 的 m 属性是 HTML 转义 JSON）。"""

    import html as html_lib

    for match in re.finditer(r'\bm="([^"]+)"', html_text or ""):
        payload = html_lib.unescape(match.group(1))

        if not payload.startswith("{"):
            continue

        try:
            item = json.loads(payload)
        except json.JSONDecodeError:
            continue

        if isinstance(item, dict) and (item.get("turl") or item.get("murl")):
            yield item


GENERIC_QUERY_TERMS = {
    "chengdu", "china", "chinese", "travel", "guide", "photo", "image",
    "picture", "tour", "tourism", "scenic", "attraction",
}


def _query_terms(query: str) -> set[str]:
    raw = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
    raw.update(re.findall(r"[\u4e00-\u9fff]{2,}", query or ""))
    return raw - GENERIC_QUERY_TERMS or raw


def _term_matches(term: str, metadata: str) -> bool:
    if re.fullmatch(r"[\u4e00-\u9fff]{2,}", term):
        # 中文没有单词边界；完整词或任一二元片段命中即可，如「西湖断桥」
        # 可由结果标题中的「西湖」或「断桥」确认，避免把中文结果全部误杀。
        return term in metadata or any(
            term[index:index + 2] in metadata for index in range(len(term) - 1)
        )

    return bool(re.search(r"\b" + re.escape(term), metadata))


def _relevant_bing_item(query: str, item: dict) -> bool:
    """用结果标题和来源 URL 交叉校验关键词，拒绝明显无关图片。"""

    terms = _query_terms(query)

    if not terms:
        return False

    metadata = " ".join(
        urllib.parse.unquote(str(item.get(key) or ""))
        for key in ("t", "purl", "murl")
    ).lower()
    matched = {term for term in terms if _term_matches(term, metadata)}
    required = len(terms) if len(terms) <= 2 else (len(terms) + 1) // 2

    return len(matched) >= required


def _fetch_bing(
    query: str, target_base: Path, *, opener=None, seen: set[str] | None = None
) -> tuple[Path, str] | None:
    url = BING_SEARCH + "?" + urllib.parse.urlencode(
        {"q": query, "form": "HDRSC2", "first": "1"}
    )

    try:
        with _open(opener, _request(url, BING_UA)) as response:
            html_text = response.read(2_000_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None

    for index, item in enumerate(_bing_items(html_text)):
        if index >= 12:
            break

        if not _relevant_bing_item(query, item):
            continue

        title = str(item.get("t") or query).strip()
        host = urllib.parse.urlparse(str(item.get("purl") or "")).netloc
        credit = f"Bing 图片／{title[:50]}"

        if host:
            credit += f"（来源：{host}）"

        # 原图与该搜索项直接对应；失败时再尝试 Bing CDN 缩略图。
        for key in ("murl", "turl"):
            image_url = str(item.get(key) or "").strip()

            if not image_url:
                continue

            result = _download(
                image_url, target_base,
                opener=opener, user_agent=BING_UA, credit=credit[:160], seen=seen,
            )

            if result:
                return result

    return None
