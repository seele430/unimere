"""单用户对话的流式 run 编排、取消事件与 run / step / exec 留痕。

allow 工具不再由本进程直接执行：调用交给 ``src.platform.exec_service``，
在 tier1 runner 子进程里跑；取消先落到 CancellationToken，再终止 runner
的进程树，进程树清空之后才允许写 cancelled 终态。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from src import agent
from src.core import approvals
from src.core.run_control import CancellationToken, ChatCancelled
from src.core.run_store import RunStore
from src.core.tool_registry import ToolApprovalRequired, ToolDenied
from src.memory import work as work_memory
from src.routing import Router
from src.routing.events import record_route_decision
from src.platform.trace_context import child_traceparent, traceparent_for_run


logger = logging.getLogger(__name__)

_INTENT_LABELS = {
    "general.chat": "闲聊问答",
    "data.analysis": "分析数据并出图",
    "code.execute": "执行代码或命令",
    "file.operate": "读写/整理文件",
    "platform.inspect": "查看平台状态",
    "reminder.manage": "设置提醒",
}
from src.platform.exec_service import (
    ERROR_CANCELLED,
    ERROR_RUNNER,
    EXEC_CANCELLED,
    EXEC_SUCCEEDED,
    PlatformExecutionError,
    raise_tool_error,
)


EventEmitter = Callable[[dict[str, Any]], None]


def _direct_tool_call(call_id: str, name: str, arguments: dict[str, Any]):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )


def _explicit_list_dir_call(user_input: str):
    """明确的目录查看请求直接形成工具调用，不让模型假装已经执行。"""

    lowered = user_input.lower()
    if any(word in user_input for word in ("不要", "不用", "不需要", "别调用")):
        return None

    names_tool = "list_dir" in lowered and any(
        word in user_input for word in ("调用", "使用", "执行", "列出", "查看")
    )
    describes_tool = (
        "列出" in user_input
        and "目录" in user_input
        and ("沙箱" in user_input or "工作区" in user_input)
    )
    if not (names_tool or describes_tool):
        return None

    return _direct_tool_call("direct-list-dir", "list_dir", {"path": "."})


def _explicit_write_then_list_calls(user_input: str):
    """明确的“写入文件；随后 list_dir”按固定顺序执行并复用审批门。"""

    if any(word in user_input for word in ("不要", "不用", "不需要", "别调用")):
        return None

    match = re.search(
        r"(?:写入(?:文件)?|write_file)\s*[`\"']?"
        r"(?P<path>[^\s，`\"'；;]+)"
        r"[^；;]*?内容为\s*(?P<content>.*?)\s*[；;]\s*"
        r"(?:随后|然后|再)[\s\S]*?list_dir",
        user_input,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None

    content = match.group("content").strip()
    if len(content) >= 2 and content[0] == content[-1] and content[0] in "\"'“”‘’":
        content = content[1:-1]

    return [
        _direct_tool_call(
            "direct-write-file",
            "write_file",
            {"path": match.group("path"), "content": content},
        ),
        _direct_tool_call("direct-list-after-write", "list_dir", {"path": "."}),
    ]


def _assistant_message_dict(message) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls or []
        ],
    }


_WORKFLOW_REFERENCES = {
    "$http_download.artifact_id": ("http_download", "artifact_id"),
    "$csv_summarize.report_artifact_id": (
        "csv_summarize", "report_artifact_id",
    ),
}


def _resolve_workflow_reference(tool_call, tool_history: list[dict[str, Any]]) -> None:
    """把技能中的产物引用替换为前序工具的真实返回值。

    引用只在执行当前步骤前解析，所以审批预览和 runner 收到的都是实际
    artifact_id；归属检查仍由 ArtifactRegistry 在 runner 内完成。
    """

    arguments = agent._tool_arguments(tool_call)
    reference = arguments.get("artifact_id")
    if reference not in _WORKFLOW_REFERENCES:
        return

    source_name, result_key = _WORKFLOW_REFERENCES[reference]
    for item in reversed(tool_history):
        if item.get("name") != source_name:
            continue
        result = item.get("result")
        value = result.get(result_key) if isinstance(result, dict) else None
        if not isinstance(value, str) or not value:
            raise ValueError(f"工作流引用失败：{source_name} 未返回 {result_key}")
        arguments["artifact_id"] = value
        tool_call.function.arguments = json.dumps(arguments, ensure_ascii=False)
        agent._check_tool_call_arguments(tool_call)
        return

    raise ValueError(f"工作流引用失败：尚未执行 {source_name}")


def _initial_messages(
    user_input: str, bot: dict | None, session_id: str | None = None
):
    agent.validate_user_input(user_input)
    agent.init_db()
    bot_id = str(bot["id"]) if bot is not None else None
    knowledge = agent._knowledge_context(user_input, bot_id=bot_id)
    has_context = knowledge is not None and not knowledge.empty
    messages = [
        {
            "role": "system",
            "content": agent.build_system_prompt(
                datetime.now().astimezone(),
                with_knowledge=has_context,
                bot=bot,
            ),
        }
    ]
    if has_context:
        messages.append({"role": "system", "content": knowledge.text})
    skill_context = agent._skill_context(user_input)
    if skill_context:
        messages.append({"role": "system", "content": skill_context})
    memory_context = agent._memory_context(user_input)
    if memory_context:
        messages.append({"role": "system", "content": memory_context})
    session_context = agent._session_context(session_id)
    if session_context:
        messages.append({"role": "system", "content": session_context})
    work_context = agent._work_memory_context(user_input)
    if work_context:
        messages.append({"role": "system", "content": work_context})
    messages.append({"role": "user", "content": user_input})
    return messages, knowledge


def _finish_platform_exec(
    outcome,
    *,
    store: RunStore | None,
    execution: dict[str, Any] | None,
    record_event,
) -> dict[str, Any]:
    """解释一次平台执行结果，并把终态写进 exec 与 trajectory。

    分工刻意保持「一个终态只写一次」：

    - 成功：exec 由上层既有的 ``else`` 分支写，这里只记 trajectory；
    - 取消：exec 由 ``except ChatCancelled`` 分支写，这里只记 trajectory；
    - 工具自身报错：exec 由既有的 except 分支写，这里只还原异常类型；
    - 平台级失败（超时 / 输出超限 / 清树失败）：上层不会写 exec，在这里写。
    """

    payload = {
        "exit_code": outcome.exit_code,
        "error_code": outcome.error_code,
        "cleanup": outcome.cleanup,
        "duration_ms": outcome.duration_ms,
        "truncated": outcome.truncated,
    }

    if outcome.status == EXEC_SUCCEEDED:
        record_event("exec_finished", "succeeded", payload=payload)
        return outcome.result or {}

    if outcome.status == EXEC_CANCELLED:
        # ExecOutcome 只有在进程树清空之后才返回，因此这里落 cancelled 是安全的。
        record_event("exec_finished", "cancelled", payload=payload)
        raise ChatCancelled("工具执行已取消")

    if outcome.tool_error is not None:
        payload["error_type"] = outcome.tool_error.get("error_type")
        record_event("exec_finished", "failed", payload=payload)
        raise_tool_error(outcome.tool_error)
        raise RuntimeError("工具错误没有被正确还原")

    if store is not None and execution is not None:
        store.update_exec(
            execution["id"],
            status=outcome.status,
            error_code=outcome.error_code,
        )

    record_event("exec_finished", outcome.status, payload=payload)

    raise PlatformExecutionError(
        f"平台执行失败：{outcome.status} / {outcome.error_code}",
        status=outcome.status,
        error_code=outcome.error_code or ERROR_RUNNER,
    )


def run_chat_stream(
    user_input: str,
    *,
    emit: EventEmitter,
    cancel_token: CancellationToken,
    bot: dict | None = None,
    session_id: str | None = None,
) -> str:
    """Run at most two streaming model calls and one approved tool call."""

    # 每条消息先过意图路由：决策落 trajectory；低置信度/歧义直接要求澄清，
    # 不把问题丢给模型去猜。
    decision = Router().decide(user_input)

    identity = agent.current_identity()
    store = RunStore() if identity is not None else None
    run = turn = None
    if store is not None:
        run = store.create_run(
            owner_id=identity.user_id, request_id=identity.request_id
        )
        turn = store.create_turn(run["id"], user_message=user_input)
        store.add_step(
            turn["id"], kind="user", actor="user",
            content=user_input, status="succeeded",
        )
        store.append_event(run["id"], event_type="run_started", status="running")
        record_route_decision(store, run["id"], decision)
    run_id = run["id"] if run is not None else None

    if decision.decision == "clarify":
        labels = "、".join(
            f"「{_INTENT_LABELS.get(item[0], item[0])}」"
            for item in decision.candidates[:2]
        )
        question = f"我不太确定你想让我做什么：是 {labels} 中的哪一个？"

        if store is not None:
            store.add_step(
                turn["id"], kind="assistant", actor="assistant",
                content=question, status="succeeded",
            )
            store.append_event(
                run_id, event_type="clarification_requested", status="succeeded"
            )
            store.append_event(run_id, event_type="run_finished", status="succeeded")
            store.finish(run_id, status="succeeded")

        emit({"type": "done", "run_id": run_id, "reply": question})
        return question

    # D24：同一条 run 共用一条 trace-id，沙箱子进程通过 TRACEPARENT 继承。
    run_traceparent = (
        traceparent_for_run(run_id) if run_id is not None else None
    )
    tool_history: list[dict[str, Any]] = []

    def finish_run(status: str) -> None:
        """先写运行终态，再尽力沉淀工作记忆；记忆失败不改变任务结果。"""

        if store is None or run_id is None:
            return
        store.append_event(run_id, event_type="run_finished", status=status)
        store.finish(run_id, status=status)
        try:
            work_memory.record_run(
                identity.user_id,
                run_id,
                user_input,
                status,
                tool_history,
            )
        except Exception:
            logger.exception("工作记忆写入失败 run_id=%s", run_id)

    active_step = None
    active_parts: list[str] = []
    terminal_emitted = False
    # D12：同一次 run 内的工具共享一个沙箱工作区；run 结束（含取消/失败）回收。
    run_sandbox: str | None = None

    def start_assistant_step():
        nonlocal active_step, active_parts
        active_parts = []
        active_step = (
            store.add_step(
                turn["id"], kind="assistant", actor="assistant", content=""
            )
            if store is not None else None
        )

    def on_delta(text: str) -> None:
        cancel_token.check()
        active_parts.append(text)
        emit({"type": "text_delta", "run_id": run_id, "text": text})

    def finish_assistant_step(content: str) -> None:
        nonlocal active_step
        if store is not None and active_step is not None:
            store.update_step(active_step["id"], status="succeeded", content=content)
        active_step = None

    try:
        emit({"type": "run_started", "run_id": run_id})
        messages, knowledge = _initial_messages(user_input, bot, session_id=session_id)
        direct_calls = _explicit_write_then_list_calls(user_input)
        if direct_calls is None:
            direct_call = _explicit_list_dir_call(user_input)
            direct_calls = [direct_call] if direct_call is not None else None
        assistant = None
        model_rounds = 0
        # 真实模型可能每轮只返回一个工具；同一 run 内允许分批继续，
        # 但最多四次模型调用，避免模型无休止地调用工具。
        while True:
            if assistant is None:
                start_assistant_step()
                if direct_calls is not None:
                    assistant = SimpleNamespace(content="", tool_calls=direct_calls)
                else:
                    model_rounds += 1
                    response = agent._completion(
                        messages, tool_choice="auto",
                        on_delta=on_delta, cancel_token=cancel_token,
                    )
                    assistant = response.choices[0].message
                finish_assistant_step(assistant.content or "")
            tool_calls = assistant.tool_calls

            if not tool_calls:
                reply = assistant.content or ""
                agent._record_citations(knowledge, reply)
                if store is not None:
                    finish_run("succeeded")
                terminal_emitted = True
                emit({"type": "done", "run_id": run_id, "reply": reply})
                return reply

            # 固定两步序列是用户明确指定的调用，不放宽模型自由多工具的上限。
            if direct_calls is None and len(tool_calls) > agent.MAX_TOOL_CALLS_PER_TURN:
                raise RuntimeError("模型一次请求的工具数量超过上限，已全部拒绝执行")

            reminder_calls = [
                call
                for call in tool_calls
                if call.function.name in agent.REMINDER_TOOL_NAMES
            ]

            if len(reminder_calls) > agent.MAX_REMINDER_TOOL_CALLS:
                raise RuntimeError("模型一次请求多个提醒工具，已全部拒绝执行")

            for tool_call in tool_calls:
                agent._check_tool_call_arguments(tool_call)
            assistant_message = _assistant_message_dict(assistant)
            messages.append(assistant_message)

            for tool_index, tool_call in enumerate(tool_calls):
                _resolve_workflow_reference(tool_call, tool_history)
                cancel_token.check()
                # D24：同一 run 共享 trace-id，每次 exec 派生自己的 span。
                exec_traceparent = (
                    child_traceparent(run_traceparent)
                    if run_traceparent is not None
                    else None
                )
                name = tool_call.function.name
                policy = agent.TOOL_REGISTRY.policy(name)
                history_entry: dict[str, Any] = {"name": name, "result": None}
                tool_history.append(history_entry)
                tool_step = (
                    store.add_step(
                        turn["id"], kind="tool", actor="assistant", content=name
                    )
                    if store is not None else None
                )
                execution = None
                stop_tool_calls = False
                if policy == "allow" and store is not None:
                    # queued 起步：running 由平台真正启动 runner 之后再迁移。
                    execution = store.record_exec(
                        tool_step["id"], tool_name=name,
                        request=agent._tool_arguments(tool_call), status="queued",
                    )
                emit({
                    "type": "tool_started", "run_id": run_id,
                    "tool": name, "policy": policy,
                    "exec_id": execution["id"] if execution is not None else None,
                })

                sandbox_ref: dict[str, str | None] = {"id": None}

                def record_event(event_type, status, *, sandbox_id=None, payload=None):
                    if store is None or run_id is None:
                        return None
                    # exec 可能是审批通过之后才建的，这里每次现取。
                    current_exec = execution["id"] if execution is not None else None
                    return store.append_event(
                        run_id,
                        event_type=event_type,
                        status=status,
                        step_id=tool_step["id"] if tool_step is not None else None,
                        exec_id=current_exec,
                        sandbox_id=sandbox_id or sandbox_ref["id"],
                        payload=payload,
                    )

                def on_started(sandbox_id):
                    sandbox_ref["id"] = sandbox_id
                    if store is not None and execution is not None:
                        store.start_exec(execution["id"], sandbox_id=sandbox_id)
                    record_event(
                        "exec_started",
                        "running",
                        sandbox_id=sandbox_id,
                        payload={"traceparent": exec_traceparent},
                    )

                def on_cancel_requested(requested_at):
                    # 取消请求与最终 cancelled 必须可区分，且先于终态写入。
                    record_event(
                        "exec_cancel_requested",
                        "cancel_requested",
                        payload={"requested_at": requested_at},
                    )

                try:
                    if policy == "ask":
                        # D13：审批之前不产生 exec；批准之后才建 exec 并执行。
                        if store is None or run_id is None:
                            raise ToolDenied("没有 run 记录，无法审批这次工具调用")

                        arguments = agent._tool_arguments(tool_call)
                        approval = store.create_approval(
                            run_id,
                            step_id=tool_step["id"],
                            tool_name=name,
                            arguments=arguments,
                            preview=approvals.build_preview(name, arguments),
                        )
                        record_event(
                            "tool_approval_requested",
                            "pending",
                            payload={"approval_id": approval["id"], "tool": name},
                        )
                        emit({
                            "type": "approval_required", "run_id": run_id,
                            "tool": name, "approval_id": approval["id"],
                            "preview": approval["arguments_preview"],
                        })

                        decision = approvals.wait_for_decision(
                            store, approval["id"], cancel_token=cancel_token
                        )

                        if decision == "cancelled":
                            store.expire_approval(approval["id"])
                            record_event(
                                "tool_approval_decided", "expired",
                                payload={
                                    "approval_id": approval["id"],
                                    "reason": "run_cancelled",
                                },
                            )
                            raise ChatCancelled("审批期间 run 被取消")

                        record_event(
                            "tool_approval_decided", decision,
                            payload={"approval_id": approval["id"]},
                        )
                        emit({
                            "type": "approval_decided", "run_id": run_id,
                            "tool": name, "approval_id": approval["id"],
                            "decision": decision,
                        })

                        if decision != approvals.APPROVED:
                            raise ToolDenied(
                                "用户拒绝了这次工具调用"
                                if decision == approvals.REJECTED
                                else "审批超时，未执行工具"
                            )

                        approvals.verify_digest(approval, arguments)

                        execution = store.record_exec(
                            tool_step["id"], tool_name=name,
                            request=arguments, status="queued",
                        )
                        if run_sandbox is None:
                            run_sandbox = agent.open_run_sandbox(run_id)
                        outcome = agent.execute_tool_on_platform(
                            tool_call,
                            cancel_token=cancel_token,
                            sandbox_id=run_sandbox,
                            on_started=on_started,
                            on_cancel_requested=on_cancel_requested,
                            require_allow=False,
                            approved=True,
                            traceparent=exec_traceparent,
                        )
                        result = _finish_platform_exec(
                            outcome,
                            store=store,
                            execution=execution,
                            record_event=record_event,
                        )
                    elif policy == "allow":
                        if run_sandbox is None:
                            run_sandbox = agent.open_run_sandbox(run_id)
                        outcome = agent.execute_tool_on_platform(
                            tool_call,
                            cancel_token=cancel_token,
                            sandbox_id=run_sandbox,
                            on_started=on_started,
                            on_cancel_requested=on_cancel_requested,
                            traceparent=exec_traceparent,
                        )
                        result = _finish_platform_exec(
                            outcome,
                            store=store,
                            execution=execution,
                            record_event=record_event,
                        )
                    else:
                        # deny：在产生 exec 之前就抛错，走既有分支。
                        result = agent.TOOL_REGISTRY.execute(
                            tool_call, cancel_token=cancel_token
                        )
                except ChatCancelled:
                    if store is not None and execution is not None:
                        store.update_exec(
                            execution["id"],
                            status="cancelled",
                            error_code=ERROR_CANCELLED,
                        )
                        store.update_step(tool_step["id"], status="cancelled", content=name)
                    raise
                except (
                    ValueError, TypeError, KeyError, PermissionError,
                    agent.ReminderDeliveryUnavailable,
                ) as exc:
                    result = {"success": False, "error": str(exc)}
                    if store is not None:
                        if execution is not None:
                            store.update_exec(
                                execution["id"], status="failed", result=result,
                                error_code="tool_failed",
                            )
                        status = (
                            "awaiting_approval"
                            if isinstance(exc, ToolApprovalRequired)
                            else "denied" if isinstance(exc, ToolDenied)
                            else "failed"
                        )
                        store.update_step(
                            tool_step["id"], status=status,
                            content=json.dumps(result, ensure_ascii=False),
                        )
                    if isinstance(exc, ToolDenied):
                        emit({"type": "tool_denied", "run_id": run_id, "tool": name})
                        # 拒绝是本轮工具链的停止条件，不能继续执行后续调用。
                        stop_tool_calls = True
                else:
                    if store is not None and execution is not None:
                        store.update_exec(
                            execution["id"], status="succeeded", result=result
                        )
                        store.update_step(
                            tool_step["id"], status="succeeded",
                            content=json.dumps(result, ensure_ascii=False),
                        )
                history_entry["result"] = result
                emit({
                    "type": "tool_finished", "run_id": run_id,
                    "tool": name, "result": result,
                    "exec_id": execution["id"] if execution is not None else None,
                })
                messages.append({
                    "role": "tool", "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
                if stop_tool_calls:
                    # 未执行的调用没有 tool 响应，不能留在下一次模型请求中。
                    assistant_message["tool_calls"] = assistant_message["tool_calls"][
                        : tool_index + 1
                    ]
                    break

            start_assistant_step()
            if model_rounds >= 4:
                raise RuntimeError("模型工具编排超过 4 轮上限，已停止")
            model_rounds += 1
            final_response = agent._completion(
                messages, tool_choice="auto",
                on_delta=on_delta, cancel_token=cancel_token,
            )
            assistant = final_response.choices[0].message
            finish_assistant_step(assistant.content or "")
            if not assistant.tool_calls:
                reply = assistant.content or ""
                agent._record_citations(knowledge, reply)
                if store is not None:
                    finish_run("succeeded")
                terminal_emitted = True
                emit({"type": "done", "run_id": run_id, "reply": reply})
                return reply
            # 新一批工具调用沿用同一 messages、run、沙箱和工作记忆。
            direct_calls = None
    except ChatCancelled:
        if store is not None:
            if active_step is not None:
                store.update_step(
                    active_step["id"], status="cancelled",
                    content="".join(active_parts),
                )
            finish_run("cancelled")
        terminal_emitted = True
        emit({"type": "cancelled", "run_id": run_id})
        raise
    except Exception as exc:
        if store is not None:
            if active_step is not None:
                store.update_step(
                    active_step["id"], status="failed",
                    content="".join(active_parts),
                )
            finish_run("failed")
        if not terminal_emitted:
            emit({"type": "run_failed", "run_id": run_id, "error": str(exc)})
        raise
    finally:
        # run 结束（成功 / 取消 / 失败）都回收工作区；文件不跨 run 残留。
        agent.destroy_run_sandbox(run_sandbox)

        # 还有挂着没决定的审批就一并过期，避免永远停在 pending。
        if store is not None and run_id is not None:
            try:
                store.expire_pending_approvals(run_id)
            except Exception:  # noqa: BLE001 - 收尾失败不能盖掉真实结果
                pass
