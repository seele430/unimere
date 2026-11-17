"""QQ 官方单聊入站：白名单校验、幂等入队与网关生命周期。"""

from __future__ import annotations

import logging
import os

from pathlib import Path

from fastapi import APIRouter

from src.qq import router as qq_router
from src.qq.gateway import OfficialGateway
from src.qq.inbound import QQInbound
from src.qq.official import OfficialBotClient
from src.qq.outbox import QQOutbox
from src.qq.transport import official_uid
from src.qq.worker import build_workers


router = APIRouter(tags=["qq"])

_log = logging.getLogger(__name__)

_inbound: QQInbound | None = None
_outbox: QQOutbox | None = None
_task_worker = None
_control_worker = None
_gateway: OfficialGateway | None = None


def data_root() -> Path:
    return Path(
        os.getenv("PERSONAL_AGENT_DATA_DIR")
        or Path(__file__).resolve().parents[2] / "data"
    )


def build_client():
    """构造唯一的 QQ 官方机器人客户端。"""

    return OfficialBotClient(
        app_id=os.getenv("PA_QQ_APP_ID") or "",
        app_secret=os.getenv("PA_QQ_APP_SECRET") or "",
        owner_openid=os.getenv("PA_QQ_OWNER_OPENID") or "",
    )


def configured_inbound() -> QQInbound:
    global _inbound

    if _inbound is None:
        text_enabled = (os.getenv("PA_QQ_ENABLED") or "").strip() == "1"
        _inbound = QQInbound(
            owner_id=int(os.getenv("PA_QQ_OWNER") or 0),
            secret=os.getenv("PA_QQ_SECRET") or "",
            client=build_client(),
            enabled=text_enabled,
        )

    return _inbound


def configured_workers():
    global _outbox, _task_worker, _control_worker

    if _task_worker is None:
        handler = configured_inbound()
        root = data_root()
        root.mkdir(parents=True, exist_ok=True)
        _outbox, _task_worker, _control_worker = build_workers(
            db_path=str(root / "qq-outbox.db"),
            client=handler.client,
            handler=handler,
        )
        _task_worker.start()
        _control_worker.start()

    return _outbox, _task_worker, _control_worker


def reset_inbound() -> None:
    """测试用：停掉 worker、丢掉缓存。"""

    global _inbound, _outbox, _task_worker, _control_worker

    for worker in (_task_worker, _control_worker):
        if worker is not None:
            worker.stop()

    _task_worker = None
    _control_worker = None
    _outbox = None
    _inbound = None


def queue_for(text: str) -> str:
    stripped = (text or "").strip()

    if stripped.startswith("/confirm") or stripped.startswith("/cancel"):
        return "control"

    return "task"


def is_conversation(text: str) -> bool:
    """`/new`、`/use` 这类切换命令不是对话内容，不写进会话消息表。

    它们由入站按「旧会话」落 user 行、由 worker 按「新会话」落助手行，
    一旦两边都写，一次命令就被劈成两条孤立记录。
    """

    intent = qq_router.classify(text).get("intent")

    return intent not in qq_router.SESSION_SWITCH_INTENTS


def enqueue_inbound(*, sender: int, message_id: str, text: str) -> bool:
    """官方入站写 outbox（主键幂等）并记录会话消息。"""

    outbox, _, _ = configured_workers()
    enqueued = outbox.enqueue(
        message_id=message_id,
        sender=int(sender),
        raw_text=text,
        queue=queue_for(text),
    )

    if enqueued and is_conversation(text):
        try:
            from src.sessions import SessionStore

            store = SessionStore()
            session = store.ensure_active(int(sender))
            store.add_message(session["id"], "user", text, message_id=message_id)
        except Exception:  # noqa: BLE001 - 会话记录失败不影响入站
            pass

    return enqueued


# ==========================================
# 官方机器人通道（WebSocket 网关）
# ==========================================


def handle_official_message(openid: str, message_id: str, text: str) -> None:
    """网关收到 C2C 单聊 → 白名单 → 幂等入队。"""

    handler = configured_inbound()

    if not handler.enabled:
        return

    owner_openid = (os.getenv("PA_QQ_OWNER_OPENID") or "").strip()

    if not owner_openid:
        _log.warning(
            "QQ 官方：未配置 PA_QQ_OWNER_OPENID，消息已丢弃；收到 openid=%s", openid
        )

        return

    sender = official_uid(openid)

    if sender != official_uid(owner_openid):
        _log.warning("QQ 官方：非白名单 openid 消息已丢弃（sender=%s）", sender)

        return

    if not enqueue_inbound(sender=sender, message_id=message_id, text=text):
        _log.info("QQ 官方：重复消息已忽略 %s", message_id)


def start_official_gateway() -> bool:
    """凭证齐备时启动官方网关；重复调用无副作用。"""

    global _gateway

    if _gateway is not None:
        return False

    handler = configured_inbound()
    client = handler.client

    if not getattr(client, "configured", False):
        _log.warning("QQ 官方：缺少 PA_QQ_APP_ID / PA_QQ_APP_SECRET，网关未启动")

        return False

    _gateway = OfficialGateway(client=client, on_message=handle_official_message)

    if _gateway.start():
        _log.info("QQ 官方网关已启动（出站长连）")

        return True

    return False


def stop_official_gateway() -> None:
    global _gateway

    if _gateway is not None:
        _gateway.stop()
        _gateway = None
