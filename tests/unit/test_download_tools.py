"""P3 数据集下载：完整性、大小边界、身份与失败清理。"""

import hashlib
import http.client
import io
import json
import tempfile
import unittest
import urllib.error

from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.artifacts.registry import ArtifactRegistry
from src.core.reminder_identity import bind_authenticated_reminder
from src.tools import download_tools
from tests.helper import isolate_paths


CSV = "month,sales\n1月,12400\n2月,16800\n".encode("utf-8")


def call(arguments: dict):
    return SimpleNamespace(
        function=SimpleNamespace(arguments=json.dumps(arguments))
    )


class FakeResponse:
    def __init__(
        self,
        data=CSV,
        *,
        url="https://data.example.test/sales.csv",
        content_length=True,
        encoding="",
        error=None,
    ):
        self.status = 200
        self._url = url
        self._stream = io.BytesIO(data)
        self._error = error
        self.headers = Message()
        self.headers["Content-Type"] = "text/csv; charset=utf-8"
        if content_length:
            self.headers["Content-Length"] = str(len(data))
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def geturl(self):
        return self._url

    def read(self, size):
        if self._error is not None:
            raise self._error
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class HttpDownloadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

    def invoke(self, arguments: dict):
        with bind_authenticated_reminder(
            user_id="alice", request_id="p3_download", role="member"
        ):
            return download_tools.http_download(call(arguments))

    def assert_clean(self):
        registry = ArtifactRegistry()
        self.assertEqual(registry.count("alice"), 0)
        self.assertEqual(list(registry.root.glob("download-*")), [])

    def test_complete_csv_is_registered_with_real_size_and_sha256(self):
        with patch.object(
            download_tools.urllib.request, "urlopen", return_value=FakeResponse()
        ) as opener:
            result = self.invoke({
                "url": "https://data.example.test/sales.csv",
                "max_bytes": len(CSV),
            })

        self.assertTrue(result["success"])
        self.assertEqual(result["size_bytes"], len(CSV))
        self.assertEqual(result["max_bytes"], len(CSV))
        self.assertEqual(result["sha256"], hashlib.sha256(CSV).hexdigest())
        self.assertEqual(result["media_type"], "text/csv")
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Accept-encoding"), "identity")

        registry = ArtifactRegistry()
        artifact = registry.get(result["artifact_id"], owner_id="alice")
        self.assertEqual(artifact.kind, "text")
        self.assertEqual(artifact.title, "数据集：sales.csv")
        self.assertEqual(
            registry.absolute_path(artifact.id, owner_id="alice").read_bytes(), CSV
        )

    def test_declared_content_length_over_limit_is_rejected_before_writing(self):
        with patch.object(
            download_tools.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            with self.assertRaisesRegex(ValueError, "超过大小上限"):
                self.invoke({
                    "url": "https://data.example.test/sales.csv",
                    "max_bytes": len(CSV) - 1,
                })

        self.assert_clean()

    def test_streaming_over_limit_without_length_deletes_partial_file(self):
        response = FakeResponse(content_length=False)

        with patch.object(
            download_tools.urllib.request, "urlopen", return_value=response
        ):
            with self.assertRaisesRegex(ValueError, "超过大小上限"):
                self.invoke({
                    "url": "https://data.example.test/sales.csv",
                    "max_bytes": 5,
                })

        self.assert_clean()

    def test_invalid_url_and_limits_never_open_network(self):
        cases = (
            {"url": "file:///C:/data.csv"},
            {"url": "https://data.example.test/a.csv", "max_bytes": True},
            {"url": "https://data.example.test/a.csv", "max_bytes": 0},
            {"url": "https://data.example.test/a.csv", "max_bytes": download_tools.MAX_DOWNLOAD_BYTES + 1},
        )

        with patch.object(download_tools.urllib.request, "urlopen") as opener:
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        self.invoke(arguments)

        opener.assert_not_called()
        self.assert_clean()

    def test_authenticated_identity_is_required(self):
        with patch.object(download_tools.urllib.request, "urlopen") as opener:
            with self.assertRaises(PermissionError):
                download_tools.http_download(call({
                    "url": "https://data.example.test/sales.csv"
                }))

        opener.assert_not_called()
        self.assert_clean()

    def test_compressed_response_is_not_saved_as_original_dataset(self):
        response = FakeResponse(encoding="gzip", content_length=False)

        with patch.object(
            download_tools.urllib.request, "urlopen", return_value=response
        ):
            with self.assertRaisesRegex(ValueError, "压缩编码"):
                self.invoke({"url": "https://data.example.test/sales.csv"})

        self.assert_clean()

    def test_incomplete_read_deletes_partial_file(self):
        response = FakeResponse(
            content_length=False,
            error=http.client.IncompleteRead(b"partial", 12),
        )

        with patch.object(
            download_tools.urllib.request, "urlopen", return_value=response
        ):
            with self.assertRaisesRegex(ValueError, "数据下载失败"):
                self.invoke({"url": "https://data.example.test/sales.csv"})

        self.assert_clean()

    def test_network_error_leaves_no_artifact(self):
        error = urllib.error.URLError("simulated offline")

        with patch.object(
            download_tools.urllib.request, "urlopen", side_effect=error
        ):
            with self.assertRaisesRegex(ValueError, "数据下载失败"):
                self.invoke({"url": "https://data.example.test/sales.csv"})

        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
