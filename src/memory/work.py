"""平台工作记忆：记录真实工具结果，并在相关对话中召回。

只保存任务目标、执行状态和少量安全元数据，不保存文件正文、工具参数或模型
推测。历史沙箱会在 run 结束时回收，因此文件路径只能用于追溯，不能当成仍可
访问的产物链接。
"""

from __future__ import annotations

import os

from typing import Any

from src.memory import service


WORK_MEMORY_ENV = "PERSONAL_AGENT_WORK_MEMORY"
_MEMORY_TOOL_NAMES = {"memory_remember", "memory_recall"}
_HISTORY_CUES = (
    "上次", "上一次", "之前", "此前", "刚才", "继续", "接着", "那个文件",
    "那个任务", "工作记录", "做了什么", "历史工作", "历史任务", "产物",
)
_STATUS_LABELS = {
    "succeeded": "成功",
    "failed": "失败",
    "cancelled": "已取消",
}
_MAX_GOAL_CHARS = 600
_MAX_DETAIL_CHARS = 240
_MAX_TOOLS = 12


def enabled() -> bool:
    """工作记忆默认开启；显式设 0 / false / no / off 可关闭。"""

    return os.getenv(WORK_MEMORY_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _tool_result_status(result: Any) -> str:
    if not isinstance(result, dict):
        return "未完成"
    if result.get("success") is False:
        return "失败"
    return "成功"


def _tool_detail(result: Any) -> str:
    if not isinstance(result, dict):
        return ""

    details = []
    path = result.get("path")
    if isinstance(path, str) and path.strip():
        details.append(f"路径={_clean(path, 200)}")

    error = result.get("error")
    if isinstance(error, str) and error.strip():
        details.append(f"错误={_clean(error, _MAX_DETAIL_CHARS)}")

    reminder_id = result.get("reminder_id")
    if reminder_id is not None:
        details.append(f"提醒编号={_clean(reminder_id, 80)}")

    for key, label in (
        ("artifact_id", "产物编号"),
        ("summary_artifact_id", "汇总产物编号"),
        ("report_artifact_id", "报告产物编号"),
        ("download_url", "下载地址"),
        ("summary_url", "汇总地址"),
        ("report_url", "报告地址"),
        ("image_url", "截图地址"),
    ):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            details.append(f"{label}={_clean(value, 240)}")

    return "，".join(details)


def record_run(
    owner_id: str,
    run_id: str,
    user_input: str,
    status: str,
    tool_history: list[dict[str, Any]],
) -> object | None:
    """把一次包含真实工具调用的 run 写成一条可更新的工作记忆。

    ``key=run:<id>`` 让重复收尾只形成同一逻辑记忆的新版本，不会在召回结果里
    出现多条当前记录。记忆自身的工具调用被排除，避免“记住了一次记忆操作”的
    递归噪声。
    """

    if not enabled():
        return None

    useful = [
        item for item in tool_history
        if item.get("name") not in _MEMORY_TOOL_NAMES
    ]
    if not useful:
        return None

    lines = [
        f"历史工作任务 {run_id}",
        f"任务目标：{_clean(user_input, _MAX_GOAL_CHARS)}",
        f"最终状态：{_STATUS_LABELS.get(status, _clean(status, 40))}",
    ]
    tool_lines = []
    for item in useful[:_MAX_TOOLS]:
        name = _clean(item.get("name"), 80)
        result = item.get("result")
        summary = f"{name}：{_tool_result_status(result)}"
        detail = _tool_detail(result)
        if detail:
            summary += f"（{detail}）"
        tool_lines.append(summary)

    lines.append("工具结果：" + "；".join(tool_lines))
    if len(useful) > _MAX_TOOLS:
        lines.append(f"另有 {len(useful) - _MAX_TOOLS} 次工具调用未展开。")

    paths = [
        item.get("result", {}).get("path")
        for item in useful
        if isinstance(item.get("result"), dict)
        and item.get("result", {}).get("path")
    ]
    if paths:
        lines.append(
            "记录中的路径属于历史沙箱；run 结束后沙箱会回收，"
            "路径仅用于追溯，不代表文件仍然存在。"
        )

    return service.remember(
        owner_id,
        "\n".join(lines),
        key=f"run:{run_id}",
        kind="episode",
        layer="project",
        trust="platform_verified",
        source_ref=f"run:{run_id}",
    )


def _needs_recent(query: str) -> bool:
    return any(cue in query for cue in _HISTORY_CUES)


def _recent_items(owner_id: str, k: int) -> list[dict[str, Any]]:
    """没有关键词命中时，仅为“继续/上次”类问题补最近工作。"""

    listing = service.list_current(owner_id, limit=100, offset=0)
    return [
        item for item in listing["items"]
        if item["kind"] == "episode"
        and item["trust"] == "platform_verified"
    ][:k]


def recall_context(owner_id: str, query: str, *, k: int = 3) -> str | None:
    """检索与当前输入相关的历史工作，并生成可注入 system 的上下文。"""

    if not enabled():
        return None

    result = service.recall(
        owner_id,
        query,
        k=k,
        kind="episode",
        layer="project",
        min_trust="platform_verified",
    )
    texts = [hit.item.text for hit in result["hits"]]

    if not texts and _needs_recent(query):
        texts = [item["text"] for item in _recent_items(owner_id, k)]

    if not texts:
        return None

    return (
        "以下是平台自动记录的相关历史工作，只在有助于当前请求时使用。"
        "历史路径不代表文件仍可访问；如需使用文件，必须重新读取或生成。"
        "不得把历史任务的状态说成当前刚刚执行的结果。\n- "
        + "\n- ".join(texts)
    )
