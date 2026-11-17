"""P4 CSV 汇总：统计正确性、产物归属、安全输出与扫描边界。"""

import csv
import hashlib
import io
import json
import tempfile
import unittest

from types import SimpleNamespace

from src.artifacts.models import ArtifactForbidden
from src.artifacts.registry import ArtifactRegistry
from src.core.reminder_identity import bind_authenticated_reminder
from src.tools import csv_summary_tools
from tests.helper import isolate_paths


def call(arguments: dict):
    return SimpleNamespace(
        function=SimpleNamespace(arguments=json.dumps(arguments))
    )


class CsvSummaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)
        self.registry = ArtifactRegistry()

    def seed(self, text: str, *, owner="alice", suffix=".csv", media_type="text/csv"):
        path = self.registry.root / f"input{suffix}"
        path.write_text(text, encoding="utf-8")
        return self.registry.register(
            owner, path, title=f"测试数据{suffix}", media_type=media_type
        )

    def invoke(self, artifact_id: str, *, user="alice", max_rows=None):
        arguments = {"artifact_id": artifact_id}
        if max_rows is not None:
            arguments["max_rows"] = max_rows
        with bind_authenticated_reminder(
            user_id=user, request_id="p4_summary", role="member"
        ):
            return csv_summary_tools.csv_summarize(call(arguments))

    def summary_rows(self, result):
        artifact = self.registry.get(result["summary_artifact_id"], owner_id="alice")
        text = self.registry.absolute_path(
            artifact.id, owner_id="alice"
        ).read_text(encoding="utf-8-sig")
        return {row["column"]: row for row in csv.DictReader(io.StringIO(text))}

    def test_real_csv_statistics_and_owned_outputs(self):
        source = self.seed(
            "month,sales,profit\n"
            "1月,\"12,400\",10\n"
            "2月,16800,\n"
            "3月,20000,30\n"
        )

        result = self.invoke(source.id)

        self.assertTrue(result["success"])
        self.assertEqual(result["source_artifact_id"], source.id)
        self.assertEqual(result["source_sha256"], hashlib.sha256(
            self.registry.absolute_path(source.id, owner_id="alice").read_bytes()
        ).hexdigest())
        self.assertEqual(result["rows_read"], 3)
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["invalid_rows"], 0)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["column_count"], 3)

        rows = self.summary_rows(result)
        self.assertEqual(rows["sales"]["type"], "numeric")
        self.assertEqual(rows["sales"]["non_empty"], "3")
        self.assertEqual(rows["sales"]["unique"], "3")
        self.assertEqual(rows["sales"]["numeric_min"], "12400")
        self.assertEqual(rows["sales"]["numeric_max"], "20000")
        self.assertEqual(rows["sales"]["numeric_mean"], "16400")
        self.assertEqual(rows["profit"]["missing"], "1")
        self.assertEqual(rows["profit"]["numeric_mean"], "20")
        self.assertEqual(rows["month"]["type"], "text")

        report = self.registry.get(result["report_artifact_id"], owner_id="alice")
        self.assertEqual(report.owner_id, "alice")
        self.assertEqual(report.media_type, "text/html")
        document = self.registry.absolute_path(
            report.id, owner_id="alice"
        ).read_text(encoding="utf-8")
        self.assertIn("有效数据行：3", document)
        self.assertIn("<td>sales</td>", document)
        self.assertEqual(self.registry.count("alice"), 3)

    def test_other_user_cannot_read_source_or_create_outputs(self):
        source = self.seed("name,value\nAlice,1\n")
        before = set(self.registry.root.iterdir())

        with self.assertRaises(ArtifactForbidden):
            self.invoke(source.id, user="bob")

        self.assertEqual(set(self.registry.root.iterdir()), before)
        self.assertEqual(self.registry.count("alice"), 1)
        self.assertEqual(self.registry.count("bob"), 0)

    def test_invalid_rows_are_not_silently_counted_as_valid(self):
        source = self.seed("name,value\nAlice,1\nBroken\nBob,2,extra\n")

        result = self.invoke(source.id)
        rows = self.summary_rows(result)

        self.assertEqual(result["rows_read"], 3)
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["invalid_rows"], 2)
        self.assertEqual(rows["name"]["non_empty"], "1")
        self.assertEqual(rows["value"]["numeric_mean"], "1")

    def test_max_rows_marks_partial_scan_and_report(self):
        source = self.seed("name,value\nAlice,1\nBob,2\nCara,3\n")

        result = self.invoke(source.id, max_rows=2)
        report_path = self.registry.absolute_path(
            result["report_artifact_id"], owner_id="alice"
        )

        self.assertEqual(result["rows_read"], 2)
        self.assertEqual(result["row_count"], 2)
        self.assertTrue(result["truncated"])
        self.assertIn("本报告只覆盖部分数据", report_path.read_text(encoding="utf-8"))

    def test_empty_and_duplicate_headers_are_normalized(self):
        source = self.seed(",name,name\n1,Alice,A\n2,Bob,B\n")

        result = self.invoke(source.id)
        rows = self.summary_rows(result)

        self.assertEqual(list(rows), ["column_1", "name", "name_2"])
        self.assertEqual(result["column_count"], 3)

    def test_non_csv_artifact_is_rejected_without_new_files(self):
        source = self.seed("plain text", suffix=".txt", media_type="text/plain")
        before = set(self.registry.root.iterdir())

        with self.assertRaisesRegex(ValueError, "只支持 CSV"):
            self.invoke(source.id)

        self.assertEqual(set(self.registry.root.iterdir()), before)

    def test_html_is_escaped_and_formula_headers_are_neutralized(self):
        source = self.seed("<script>alert(1)</script>,=SUM(A1:A2)\n<x>,1\n")

        result = self.invoke(source.id)
        rows = self.summary_rows(result)
        document = self.registry.absolute_path(
            result["report_artifact_id"], owner_id="alice"
        ).read_text(encoding="utf-8")

        self.assertIn("'=SUM(A1:A2)", rows)
        self.assertNotIn("<script>alert(1)</script>", document)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", document)

    def test_authenticated_identity_is_required(self):
        source = self.seed("name,value\nAlice,1\n")
        before = set(self.registry.root.iterdir())

        with self.assertRaises(PermissionError):
            csv_summary_tools.csv_summarize(call({"artifact_id": source.id}))

        self.assertEqual(set(self.registry.root.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
