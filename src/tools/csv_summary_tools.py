"""P4：把当前用户的 CSV 产物读入 tier1，生成列级汇总和 HTML 报告。

只接受 artifact_id，并通过 ArtifactRegistry 做归属与路径校验。解析和报告
生成都在 runner 内执行；所有原始文本在 HTML 中转义，在汇总 CSV 中防公式注入。
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import re
import uuid

from dataclasses import dataclass, field
from pathlib import Path

from src.artifacts.registry import ArtifactRegistry
from src.core.reminder_identity import current_identity


DEFAULT_MAX_ROWS = 100_000
MAX_ROWS = 1_000_000
MAX_COLUMNS = 200
MAX_UNIQUE_TRACKED = 1000
_NUMBER = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_GROUPED_NUMBER = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


CSV_SUMMARIZE_TOOL = {
    "type": "function",
    "function": {
        "name": "csv_summarize",
        "description": (
            "读取当前用户已有的 CSV 产物，生成列级汇总 CSV 和 HTML 报告。"
            "统计非空、缺失、唯一值及数值列的最小值、最大值、平均值。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "http_download 返回的产物编号"},
                "max_rows": {
                    "type": "integer",
                    "description": "最多扫描的数据行数，默认 100000，最大 1000000",
                },
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class Column:
    name: str
    non_empty: int = 0
    missing: int = 0
    numeric_count: int = 0
    numeric_sum: float = 0.0
    numeric_min: float | None = None
    numeric_max: float | None = None
    unique_hashes: set[bytes] = field(default_factory=set)
    unique_overflow: bool = False

    def add(self, raw: str) -> None:
        value = raw.strip()

        if not value:
            self.missing += 1
            return

        self.non_empty += 1
        key = hashlib.sha256(value.encode("utf-8", "replace")).digest()

        if len(self.unique_hashes) < MAX_UNIQUE_TRACKED:
            self.unique_hashes.add(key)
        elif key not in self.unique_hashes:
            self.unique_overflow = True

        number = parse_number(value)

        if number is None:
            return

        self.numeric_count += 1
        self.numeric_sum += number
        self.numeric_min = number if self.numeric_min is None else min(self.numeric_min, number)
        self.numeric_max = number if self.numeric_max is None else max(self.numeric_max, number)

    @property
    def kind(self) -> str:
        if self.non_empty == 0:
            return "empty"
        if self.numeric_count == self.non_empty:
            return "numeric"
        if self.numeric_count:
            return "mixed"
        return "text"

    @property
    def mean(self) -> float | None:
        return self.numeric_sum / self.numeric_count if self.numeric_count else None

    @property
    def unique_display(self) -> str:
        suffix = "+" if self.unique_overflow else ""
        return f"{len(self.unique_hashes)}{suffix}"


def _arguments(tool_call) -> dict:
    raw = getattr(tool_call.function, "arguments", "{}") or "{}"
    arguments = json.loads(raw)

    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是对象")

    return arguments


def _max_rows(args: dict) -> int:
    value = args.get("max_rows", DEFAULT_MAX_ROWS)

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_rows 必须是整数")

    if not 1 <= value <= MAX_ROWS:
        raise ValueError(f"max_rows 必须在 1..{MAX_ROWS}")

    return value


def parse_number(value: str) -> float | None:
    normalized = value

    if _GROUPED_NUMBER.fullmatch(value):
        normalized = value.replace(",", "")
    elif not _NUMBER.fullmatch(value):
        return None

    try:
        number = float(normalized)
    except ValueError:
        return None

    return number if math.isfinite(number) else None


def _headers(raw_headers: list[str]) -> list[str]:
    if not raw_headers:
        raise ValueError("CSV 缺少表头")
    if len(raw_headers) > MAX_COLUMNS:
        raise ValueError(f"CSV 列数超过 {MAX_COLUMNS} 上限")

    result = []
    used = set()

    for index, raw in enumerate(raw_headers):
        base = raw.strip()[:200] or f"column_{index + 1}"
        name = base
        sequence = 2

        while name in used:
            name = f"{base}_{sequence}"
            sequence += 1

        used.add(name)
        result.append(name)

    return result


def _encoding_and_sample(path: Path) -> tuple[str, str]:
    last_error = None

    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return encoding, handle.read(16_384)
        except UnicodeDecodeError as error:
            last_error = error

    raise ValueError("CSV 编码无法识别，请转换为 UTF-8") from last_error


def _delimiter(sample: str, path: Path) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        return "\t" if path.suffix.lower() == ".tsv" else ","


def _scan(path: Path, max_rows: int, cancel_token=None) -> tuple[list[Column], dict]:
    encoding, sample = _encoding_and_sample(path)
    delimiter = _delimiter(sample, path)

    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            headers = _headers(next(reader, []))
            columns = [Column(name) for name in headers]
            rows_read = 0
            valid_rows = 0
            invalid_rows = 0
            truncated = False

            for row in reader:
                if rows_read >= max_rows:
                    truncated = True
                    break

                rows_read += 1

                if len(row) != len(columns):
                    invalid_rows += 1
                    continue

                valid_rows += 1

                for column, value in zip(columns, row):
                    column.add(value)

                if cancel_token is not None and rows_read % 1000 == 0:
                    cancel_token.check()
    except csv.Error as error:
        raise ValueError(f"CSV 解析失败：{error}") from None
    except UnicodeDecodeError:
        raise ValueError("CSV 编码不完整，请转换为 UTF-8") from None

    metadata = {
        "encoding": encoding,
        "delimiter": delimiter,
        "rows_read": rows_read,
        "row_count": valid_rows,
        "invalid_rows": invalid_rows,
        "truncated": truncated,
    }
    return columns, metadata


def _number_text(value: float | None) -> str:
    return "" if value is None else format(value, ".12g")


def _csv_text(value: str) -> str:
    return "'" + value if value.startswith(("=", "+", "-", "@")) else value


def _write_summary(path: Path, columns: list[Column]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "column", "type", "non_empty", "missing", "unique",
            "numeric_min", "numeric_max", "numeric_mean",
        ])

        for column in columns:
            writer.writerow([
                _csv_text(column.name), column.kind, column.non_empty, column.missing,
                column.unique_display, _number_text(column.numeric_min),
                _number_text(column.numeric_max), _number_text(column.mean),
            ])


def _write_report(path: Path, source, columns: list[Column], metadata: dict) -> None:
    rows = []

    for column in columns:
        rows.append(
            "<tr>"
            f"<td>{html.escape(column.name)}</td><td>{column.kind}</td>"
            f"<td>{column.non_empty}</td><td>{column.missing}</td>"
            f"<td>{column.unique_display}</td>"
            f"<td>{html.escape(_number_text(column.numeric_min) or '—')}</td>"
            f"<td>{html.escape(_number_text(column.numeric_max) or '—')}</td>"
            f"<td>{html.escape(_number_text(column.mean) or '—')}</td></tr>"
        )

    warning = "<p class='warning'>已达到扫描行数上限，本报告只覆盖部分数据。</p>" if metadata["truncated"] else ""
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>CSV 列级汇总</title><style>
body{{margin:0;background:#f4f6fa;color:#182235;font-family:'Microsoft YaHei',sans-serif}}
main{{max-width:1120px;margin:36px auto;padding:30px;background:white;border-radius:18px}}
h1{{margin-bottom:8px}}.meta{{color:#657189;line-height:1.8}}.warning{{color:#a43b16;font-weight:700}}
table{{width:100%;border-collapse:collapse;margin-top:24px}}th,td{{padding:11px;text-align:right;border-bottom:1px solid #e5e9f0}}
th{{background:#edf2fb;color:#44516a}}th:first-child,td:first-child{{text-align:left}}td:first-child{{font-weight:600}}
</style></head><body><main><h1>CSV 列级汇总</h1>
<div class="meta">来源：{html.escape(source.title)}<br>源文件 SHA256：{source.sha256}<br>
有效数据行：{metadata['row_count']}；无效行：{metadata['invalid_rows']}；列数：{len(columns)}；
编码：{metadata['encoding']}</div>{warning}
<table><thead><tr><th>列名</th><th>类型</th><th>非空</th><th>缺失</th><th>唯一值</th>
<th>最小值</th><th>最大值</th><th>平均值</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</main></body></html>"""
    path.write_text(document, encoding="utf-8")


def csv_summarize(tool_call, cancel_token=None) -> dict:
    args = _arguments(tool_call)
    artifact_id = args.get("artifact_id")
    max_rows = _max_rows(args)

    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id 不能为空")

    identity = current_identity()

    if identity is None:
        raise PermissionError("没有经过认证的用户身份，拒绝汇总 CSV")

    registry = ArtifactRegistry()
    source = registry.get(artifact_id.strip(), owner_id=identity.user_id)

    if (
        Path(source.rel_path).suffix.lower() not in (".csv", ".tsv")
        and source.media_type not in ("text/csv", "application/vnd.ms-excel")
    ):
        raise ValueError("只支持 CSV / TSV 产物")

    source_path = registry.absolute_path(source.id, owner_id=identity.user_id)

    if cancel_token is not None:
        cancel_token.check()

    columns, metadata = _scan(source_path, max_rows, cancel_token=cancel_token)
    summary_path = registry.root / f"column-summary-{uuid.uuid4().hex}.csv"
    report_path = registry.root / f"summary-report-{uuid.uuid4().hex}.html"
    summary_artifact = None
    report_artifact = None

    try:
        _write_summary(summary_path, columns)
        _write_report(report_path, source, columns, metadata)
        summary_artifact = registry.register(
            identity.user_id, summary_path,
            title=f"列级汇总：{source.title}"[:200], media_type="text/csv",
        )
        report_artifact = registry.register(
            identity.user_id, report_path,
            title=f"汇总报告：{source.title}"[:200], media_type="text/html",
        )
    except Exception:
        for artifact in (summary_artifact, report_artifact):
            if artifact is not None:
                registry.delete(artifact.id, owner_id=identity.user_id)
        summary_path.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        raise

    return {
        "success": True,
        "source_artifact_id": source.id,
        "source_sha256": source.sha256,
        "column_count": len(columns),
        **metadata,
        "summary_artifact_id": summary_artifact.id,
        "summary_url": f"/api/artifacts/{summary_artifact.id}/content",
        "report_artifact_id": report_artifact.id,
        "report_url": f"/api/artifacts/{report_artifact.id}/content?inline=1",
        "report_preview_url": f"/api/artifacts/{report_artifact.id}/preview",
    }


TOOL_DEFINITIONS = (
    (CSV_SUMMARIZE_TOOL, "ask", csv_summarize),
)

MODEL_VISIBLE_TOOLS = (
    CSV_SUMMARIZE_TOOL,
)
