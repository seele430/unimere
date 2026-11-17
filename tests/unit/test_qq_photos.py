"""Wikimedia 照片检索与长图图片安全清洗测试：全 mock，不联网。"""

import json
import os
import tempfile
import unittest
import urllib.error

from pathlib import Path
from unittest.mock import patch

from src.qq.guide import _sanitize_images
from src.qq.photos import fetch_photo, photos_enabled


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit is None or limit < 0 else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


COMMONS_BODY = json.dumps(
    {
        "query": {
            "pages": {
                "123": {
                    "imageinfo": [
                        {
                            "thumburl": "https://upload.wikimedia.org/west-lake.jpg",
                            "extmetadata": {
                                "Artist": {"value": "<a href='x'>Tester</a>"},
                                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                            },
                        }
                    ]
                }
            }
        }
    }
).encode("utf-8")


class FetchPhotoCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        # 本组用例专注 Commons 源；Bing 与多源降级见下方 BingPhotoCase
        self._providers = patch.dict(
            os.environ, {"PA_PHOTO_PROVIDERS": "commons"}, clear=False
        )
        self._providers.start()
        self.addCleanup(self._providers.stop)

    def opener_for(self, body=COMMONS_BODY, content_type="image/jpeg"):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)

            if "api.php" in request.full_url:
                return FakeResponse(body)

            return FakeResponse(b"jpeg-bytes", content_type)

        opener.calls = calls

        return opener

    def test_fetch_downloads_photo_and_credit(self):
        opener = self.opener_for()

        result = fetch_photo("West Lake", self.root / "photo-1", opener=opener)

        self.assertIsNotNone(result)
        path, credit = result
        self.assertEqual(path.suffix, ".jpg")
        self.assertEqual(path.read_bytes(), b"jpeg-bytes")
        self.assertIn("Wikimedia Commons", credit)
        self.assertIn("Tester", credit)
        self.assertIn("CC BY-SA 4.0", credit)
        self.assertNotIn("<a", credit)
        self.assertEqual(len(opener.calls), 2)

    def test_empty_search_result_returns_none(self):
        opener = self.opener_for(body=b'{"query":{"pages":{}}}')

        self.assertIsNone(fetch_photo("West Lake", self.root / "p", opener=opener))

    def test_non_image_content_type_rejected(self):
        opener = self.opener_for(content_type="text/html")

        self.assertIsNone(fetch_photo("West Lake", self.root / "p", opener=opener))

    def test_network_failure_returns_none(self):
        def opener(request, timeout):
            raise urllib.error.URLError("timeout")

        self.assertIsNone(fetch_photo("West Lake", self.root / "p", opener=opener))

    def test_blank_query_returns_none(self):
        self.assertIsNone(fetch_photo("", self.root / "p", opener=self.opener_for()))

    def test_disable_switch(self):
        with patch.dict(os.environ, {"PA_GUIDE_PHOTOS": "0"}, clear=False):
            self.assertFalse(photos_enabled())

        with patch.dict(os.environ, {"PA_GUIDE_PHOTOS": "1"}, clear=False):
            self.assertTrue(photos_enabled())


BING_HTML = (
    '<a class="iusc" m="{&quot;murl&quot;:&quot;https://example.com/full.jpg&quot;,'
    '&quot;turl&quot;:&quot;https://mm.bing.net/thumb.jpg&quot;,'
    '&quot;purl&quot;:&quot;https://www.example.com/page&quot;,'
    '&quot;t&quot;:&quot;West Lake&quot;}">'
).encode("utf-8")


class BingPhotoCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    @staticmethod
    def opener_for(bing_body=BING_HTML):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)

            if "bing.com/images/search" in request.full_url:
                return FakeResponse(bing_body, "text/html")

            if "api.php" in request.full_url:
                return FakeResponse(COMMONS_BODY)

            return FakeResponse(b"jpeg-bytes", "image/jpeg")

        opener.calls = calls

        return opener

    def test_bing_original_downloaded_with_credit(self):
        opener = self.opener_for()

        with patch.dict(os.environ, {"PA_PHOTO_PROVIDERS": "bing"}, clear=False):
            result = fetch_photo("West Lake", self.root / "photo-1", opener=opener)

        self.assertIsNotNone(result)
        path, credit = result
        self.assertEqual(path.read_bytes(), b"jpeg-bytes")
        self.assertIn("Bing 图片", credit)
        self.assertIn("West Lake", credit)
        self.assertIn("example.com", credit)
        self.assertIn("example.com/full.jpg", opener.calls[1])

    def test_bing_irrelevant_result_is_rejected(self):
        body = (
            '<a class="iusc" m="{&quot;murl&quot;:&quot;https://example.com/gym.jpg&quot;,'
            '&quot;purl&quot;:&quot;https://example.com/fitness&quot;,'
            '&quot;t&quot;:&quot;Muscular man during fitness workout&quot;}">'
            '<a class="iusc" m="{&quot;murl&quot;:&quot;https://example.com/panda.jpg&quot;,'
            '&quot;purl&quot;:&quot;https://example.com/chengdu-panda-base&quot;,'
            '&quot;t&quot;:&quot;Chengdu Panda Base&quot;}">'
        ).encode("utf-8")
        opener = self.opener_for(body)

        with patch.dict(os.environ, {"PA_PHOTO_PROVIDERS": "bing"}, clear=False):
            result = fetch_photo("Chengdu Panda Base", self.root / "p", opener=opener)

        self.assertIsNotNone(result)
        self.assertNotIn("gym.jpg", opener.calls)
        self.assertIn("example.com/panda.jpg", opener.calls[1])

    def test_bing_chinese_place_name_is_accepted(self):
        body = (
            '<a class="iusc" m="{&quot;murl&quot;:&quot;https://example.com/xihu.jpg&quot;,'
            '&quot;purl&quot;:&quot;https://example.com/hangzhou&quot;,'
            '&quot;t&quot;:&quot;杭州西湖断桥风景&quot;}">'
        ).encode("utf-8")
        opener = self.opener_for(body)

        with patch.dict(os.environ, {"PA_PHOTO_PROVIDERS": "bing"}, clear=False):
            result = fetch_photo("杭州三日游 西湖断桥", self.root / "p", opener=opener)

        self.assertIsNotNone(result)
        self.assertIn("example.com/xihu.jpg", opener.calls[1])

    def test_bing_irrelevant_chinese_result_is_rejected(self):
        body = (
            '<a class="iusc" m="{&quot;murl&quot;:&quot;https://example.com/gym.jpg&quot;,'
            '&quot;purl&quot;:&quot;https://example.com/fitness&quot;,'
            '&quot;t&quot;:&quot;健身房力量训练&quot;}">'
        ).encode("utf-8")
        opener = self.opener_for(body)

        with patch.dict(os.environ, {"PA_PHOTO_PROVIDERS": "bing"}, clear=False):
            result = fetch_photo("杭州西湖断桥", self.root / "p", opener=opener)

        self.assertIsNone(result)
        self.assertEqual(len(opener.calls), 1)

    def test_bing_duplicate_url_and_content_are_skipped(self):
        body = (
            '<a class="iusc" m="{&quot;murl&quot;:&quot;https://example.com/full-one.jpg&quot;,'
            '&quot;turl&quot;:&quot;https://mm.bing.net/thumb-one.jpg&quot;,'
            '&quot;t&quot;:&quot;West Lake first view&quot;}">'
            '<a class="iusc" m="{&quot;murl&quot;:&quot;https://example.com/full-two.jpg&quot;,'
            '&quot;t&quot;:&quot;West Lake second view&quot;}">'
        ).encode("utf-8")

        def opener(request, timeout):
            if "bing.com/images/search" in request.full_url:
                return FakeResponse(body, "text/html")
            data = b"unique" if "full-two" in request.full_url else b"same"
            return FakeResponse(data, "image/jpeg")

        seen = set()

        with patch.dict(os.environ, {"PA_PHOTO_PROVIDERS": "bing"}, clear=False):
            first = fetch_photo("West Lake", self.root / "p1", opener=opener, seen=seen)
            second = fetch_photo("West Lake", self.root / "p2", opener=opener, seen=seen)

        self.assertEqual(first[0].read_bytes(), b"same")
        self.assertEqual(second[0].read_bytes(), b"unique")

    def test_bing_empty_page_returns_none(self):
        opener = self.opener_for(bing_body=b"<html></html>")

        with patch.dict(os.environ, {"PA_PHOTO_PROVIDERS": "bing"}, clear=False):
            self.assertIsNone(
                fetch_photo("West Lake", self.root / "photo-1", opener=opener)
            )

    def test_bing_failure_falls_back_to_commons(self):
        opener = self.opener_for(bing_body=b"<html></html>")

        with patch.dict(
            os.environ, {"PA_PHOTO_PROVIDERS": "bing,commons"}, clear=False
        ):
            result = fetch_photo("West Lake", self.root / "photo-1", opener=opener)

        self.assertIsNotNone(result)
        _path, credit = result
        self.assertIn("Wikimedia Commons", credit)
        self.assertIn("bing.com/images/search", opener.calls[0])
        self.assertIn("api.php", opener.calls[1])


class SanitizeImagesCase(unittest.TestCase):
    HTML = (
        "<html><body>"
        '<img src="photo-1.jpg">'
        '<img src="https://evil.example.com/x.png">'
        '<img src="file:///C:/secret.png">'
        '<img src="photo-2.jpg">'
        "</body></html>"
    )

    def test_only_allowed_local_photo_is_kept(self):
        cleaned = _sanitize_images(self.HTML, "photo-1.jpg")

        self.assertIn('src="photo-1.jpg"', cleaned)
        self.assertNotIn("evil.example.com", cleaned)
        self.assertNotIn("secret.png", cleaned)
        self.assertNotIn("photo-2.jpg", cleaned)

    def test_all_images_removed_without_photo(self):
        self.assertNotIn("<img", _sanitize_images(self.HTML).lower())


if __name__ == "__main__":
    unittest.main()
