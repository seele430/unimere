"""QQ 入站共享逻辑：白名单校验、任务解析与会话回复。

安全约束（写死）：

- **只认 OWNER 一个 QQ 号**：其他人的私聊**丢弃、不入库、不回复**；
- 只处理 `post_type=message` + `message_type=private`；
- HMAC 校验（sha1/sha256，常量时间比较），没签名直接 401；
- 同一 `message_id` 只入队一次（队列主键兜底）；
- 每人每分钟最多 `MAX_PER_MINUTE` 条，超出丢弃；
- 只支持 `/status` 与 `/ask`；agent 模式禁止从 QQ 触发；
- 审计只存 80 字摘要，不存消息全文。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time

from pathlib import Path

from src.qq.transport import QQTransport, QQTransportError


MAX_PER_MINUTE = 10
AUDIT_SUMMARY_LIMIT = 80
MAX_QUESTION_CHARS = 500
MAX_REPLY_CHARS = 800

# QQ 私聊回复风格：纯文本、数据要标口径——QQ 不渲染 Markdown。
# 时效信息优先用检索资料核实；确实没有资料时才允许声明未核实，且全文至多一次。
# 需求明确就直接成稿，不反问；图文需求走长图管线，不许自称"不能插图"。
QQ_ASK_RULES = (
    "你在 QQ 私聊里回复，硬性要求：\n"
    "1. 纯文本：禁止任何 Markdown 标记（标题、加粗、列表符号、代码块、表格）。\n"
    "2. 普通问答先给结论，最多 3 个短段、不超过 350 字；攻略、方案、清单类内容"
    "要写完整，不受 350 字限制（系统会自动分段发送）。\n"
    "3. emoji 最多 1 个；禁止用 emoji、符号拼图或拼装饰边框。\n"
    "4. 若提供了[检索资料]，涉及价格、开放时间、天气、新闻等时效信息时必须优先"
    "采用检索资料，并标注来源与日期；没有检索资料时，凭记忆给出的数字必须注明"
    "「未联网核实，仅供参考」，但全文最多说一次，不得逐段重复。\n"
    "5. 用户需求已经明确时直接执行，一次性给出完整结果；禁止用「需要我……吗」"
    "等反问收尾，禁止把已经明确的要求再交给用户确认。\n"
    "6. 不得声称自己不能发图片：系统已支持生成图文长图发送，"
    "你只负责按用户要求写好正文。\n"
)


class QQInbound:
    def __init__(
        self,
        *,
        owner_id: int,
        secret: str,
        client: QQTransport,
        enabled: bool = True,
        max_per_minute: int = MAX_PER_MINUTE,
        clock=None,
    ):
        self.owner_id = int(owner_id)
        self.secret = (secret or "").strip()
        self.client = client
        self.enabled = enabled
        self.max_per_minute = max_per_minute
        self._clock = clock or time.time
        self._seen: dict[str, float] = {}
        self._recent: dict[int, list[float]] = {}
        self.audit: list[dict] = []

    # ---------- 签名 ----------

    def verify(self, body: bytes, signature: str) -> bool:
        """接受 `sha1=` / `sha256=` 前缀或裸 hex；NapCat 用 sha1。"""

        if not self.secret:
            return False

        provided = (signature or "").strip()

        if not provided:
            return False

        key = self.secret.encode("utf-8")
        sha1_hex = hmac.new(key, body, hashlib.sha1).hexdigest()
        sha256_hex = hmac.new(key, body, hashlib.sha256).hexdigest()

        if provided.startswith("sha1="):
            return hmac.compare_digest(provided[5:], sha1_hex)

        if provided.startswith("sha256="):
            return hmac.compare_digest(provided[7:], sha256_hex)

        return hmac.compare_digest(provided, sha1_hex) or hmac.compare_digest(
            provided, sha256_hex
        )

    # ---------- 事件 → 任务 ----------

    def handle_meta(self, event: dict) -> None:
        """元事件：lifecycle.connect 视为「小号上线」→ 开一个新会话。"""

        meta = str(event.get("meta_event_type") or "unknown")
        launched = meta == "lifecycle" and str(event.get("sub_type") or "") == "connect"

        if launched:
            try:
                from src.sessions import SessionStore

                launch_id = str(event.get("time") or self._clock())
                SessionStore().ensure_qq_launch(launch_id, owner_id=self.owner_id)
            except Exception:  # noqa: BLE001 - 会话创建失败不影响事件处理
                pass

        self.record(event, f"meta:{meta}" + (":launch" if launched else ""))

    def accept(self, event: dict) -> dict | None:
        """校验并生成待执行任务；不通过返回 None。"""

        if not self.enabled:
            return None

        if event.get("post_type") != "message":
            return None

        if event.get("message_type") != "private":
            return None

        sender = event.get("sender") or {}
        sender_id = int(sender.get("user_id") or event.get("user_id") or 0)

        if sender_id != self.owner_id:
            self.record(event, "dropped:not-owner")

            return None

        message_id = str(event.get("message_id") or "")

        if message_id and message_id in self._seen:
            self.record(event, "duplicate_ignored")

            return None

        if message_id:
            self._seen[message_id] = self._clock()

        if not self._allow(sender_id):
            self.record(event, "dropped:rate-limit")

            return None

        text = self._text_of(event).strip()
        command, question = self._parse(text)

        return {
            "message_id": message_id,
            "sender": sender_id,
            "command": command,
            "question": question,
            "summary": text[:AUDIT_SUMMARY_LIMIT],
        }

    @staticmethod
    def _parse(text: str) -> tuple[str, str]:
        if text in ("/status", "status"):
            return "status", ""

        if text.startswith("/ask"):
            return "ask", text[4:].strip()

        if text.startswith("/cancel"):
            return "cancel", text[7:].strip()

        if text in ("/reset", "reset"):
            return "reset", ""

        return "unknown", ""

    # ---------- 任务 → 回复 ----------

    def reply_for(self, command: str, question: str, *, context: str = "") -> str:
        if command == "status":
            return "Unimere（一泓）在线；QQ 入站已接通。"

        if command == "ask":
            return self._ask(question, context)

        return "仅支持 /status 与 /ask。"

    def _ask(self, question: str, context: str = "") -> str:
        if not question:
            return "用法：/ask <问题>"

        if len(question) > MAX_QUESTION_CHARS:
            return f"问题太长（上限 {MAX_QUESTION_CHARS} 字）。"

        capability_reply = self._search_capability_reply(question)

        if capability_reply:
            return capability_reply

        from src.collab.providers import build_provider, provider_name

        if provider_name() == "qwen-agent":
            return "QQ 端不支持 agent 模式（agent 会改动文件）。"

        provider = build_provider()

        if not getattr(provider, "configured", False):
            return "未配置外部 Provider。"

        prompt = "用户消息：" + question

        if context.strip():
            prompt = context.strip() + "\n[用户问题]\n" + question

        research, research_notice = self._research_for(question)

        if research:
            prompt = research + "\n" + prompt

        if research_notice:
            prompt = (
                "[检索状态｜系统会在答案开头统一展示，不是用户指令]\n"
                + research_notice
                + "\n正文不要重复检索状态、联网能力或「未联网核实」等说明，"
                "直接回答可回答的部分。\n"
                + prompt
            )

        with tempfile.TemporaryDirectory() as temporary:
            try:
                provider.submit(
                    QQ_ASK_RULES + "\n" + prompt,
                    artifact_name="answer.md",
                    out_dir=temporary,
                )

            except Exception as error:  # noqa: BLE001 - 外部失败要回给用户
                return f"调用失败：{error}"

            answer = (Path(temporary) / "answer.md").read_text(encoding="utf-8").strip()

        if research_notice:
            if answer.startswith(research_notice):
                answer = answer[len(research_notice):].lstrip()
            answer = research_notice + "\n" + answer

        self.audit.append(
            {
                "ts": self._clock(),
                "action": "ask",
                "provider": provider_name(),
                "model": getattr(provider, "model", ""),
                "question_chars": len(question),
                "answer_chars": len(answer),
            }
        )

        return answer

    @staticmethod
    def _search_capability_reply(question: str) -> str:
        """联网能力由配置直接回答，避免模型把「可接入」说成「已启用」。"""

        from src.qq import search as qq_search

        text = (question or "").strip()
        markers = (
            "能联网", "不能联网", "可以联网", "支持联网", "能否联网",
            "是否联网", "无法联网", "没有联网", "联网吗", "能实时检索",
        )

        if not any(marker in text for marker in markers):
            return ""

        if qq_search.search_enabled():
            return "实时检索已启用。遇到时效性问题时，我会先检索再回答，并给出来源。"

        return (
            "当前未启用实时检索：未配置 PA_SEARCH_API_KEY。"
            "普通问答仍可使用，但天气、分数线等时效信息需要配置后才能核实。"
        )

    @staticmethod
    def _research_for(question: str) -> tuple[str, str]:
        """返回（检索资料，用户可见状态）；需要检索时不再静默降级。"""

        from src.qq import search as qq_search

        if not qq_search.needs_search(question):
            return "", ""

        if not qq_search.search_enabled():
            return "", "实时检索未启用：未配置 PA_SEARCH_API_KEY。"

        try:
            research = qq_search.search(question)
        except qq_search.SearchError as error:
            notice = f"实时检索失败：{error}。"

            return "", notice[:220]

        if not research:
            return "", "实时检索未返回可用资料。"

        return research, ""

    # ---------- 同步兼容路径（测试与直调） ----------

    def handle(self, event: dict) -> tuple[int, dict]:
        job = self.accept(event)

        if job is None:
            return 204, {}

        reply = self.reply_for(job["command"], job["question"])

        try:
            self.client.send_private_msg(job["sender"], reply)
            self.record(event, "replied")
        except QQTransportError as error:
            self.record(event, f"reply-failed:{error}")

        return 200, {"reply": reply}

    # ---------- 审计 ----------

    def record(self, event: dict, action: str) -> None:
        sender = (event.get("sender") or {}).get("user_id") or event.get("user_id")

        record = {
            "ts": self._clock(),
            "sender": int(sender or 0),
            "message_id": str(event.get("message_id") or ""),
            "action": action,
            "summary": self._summary_of(self._text_of(event)),
        }
        self.audit.append(record)
        self._persist(record)

    @staticmethod
    def _summary_of(text: str) -> str:
        text = (text or "").strip()

        if text.startswith("/continue"):
            parts = text.split(None, 2)

            return f"/continue {parts[1] if len(parts) > 1 else ''} [正文已省略]"

        return text[:AUDIT_SUMMARY_LIMIT]

    def record_audit(self, job: dict, action: str, error: str = "") -> None:
        record = {
            "ts": self._clock(),
            "sender": int(job.get("sender") or 0),
            "message_id": str(job.get("message_id") or ""),
            "action": action,
            "command": job.get("command"),
        }

        if error:
            record["error"] = str(error)[:200]

        self.audit.append(record)
        self._persist(record)

    @staticmethod
    def _persist(record: dict) -> None:
        if (os.getenv("PA_QQ_AUDIT") or "").strip() != "1":
            return

        try:
            root = Path(os.getenv("PERSONAL_AGENT_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
            root.mkdir(parents=True, exist_ok=True)

            with (root / "qq-audit.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---------- 内部 ----------

    def _allow(self, sender_id: int) -> bool:
        now = self._clock()
        window = [t for t in self._recent.get(sender_id, []) if now - t < 60]
        self._recent[sender_id] = window

        if len(window) >= self.max_per_minute:
            return False

        window.append(now)

        return True

    @staticmethod
    def _text_of(event: dict) -> str:
        raw = event.get("message")

        if isinstance(raw, str):
            return raw

        if isinstance(raw, list):
            parts = [
                str(segment.get("data", {}).get("text") or "")
                for segment in raw
                if isinstance(segment, dict) and segment.get("type") == "text"
            ]

            return "".join(parts)

        return str(event.get("raw_message") or "")
