"""QQ 官方机器人的最小发送契约与 openid 身份映射。

- `worker.py` / `inbound.py` 只通过这一层发送文本、图片和图文消息；
- 被动回复的 `msg_id` / `msg_seq` 与条数预算由官方客户端落实；
- openid 是字符串，而 `qq_outbox.sender` 是 INTEGER，因此用 `official_uid()`
  稳定哈希成 63 位正整数，不改表结构；
- 协议方法全部同步，可在 worker 线程里直接调用；失败抛中文短句异常。
"""

from __future__ import annotations

import hashlib

from typing import Iterable, Protocol, runtime_checkable


#: 单条消息的富媒体项：("text", 文本) 或 ("image", 本地路径, 配文)。
RichItem = tuple


class QQTransportError(RuntimeError):
    """两个通道共用的发送异常基类。

    `sent_count` 表示「失败前已经成功发出几条」，供 worker 判断要不要标
    「已部分送达」。默认 0 = 一条都没发出去。
    """

    def __init__(self, message: str = "", *, sent_count: int = 0):
        super().__init__(message)
        self.sent_count = int(sent_count or 0)


@runtime_checkable
class QQTransport(Protocol):
    """发送侧最小接口，由 `OfficialBotClient` 实现。"""

    @property
    def configured(self) -> bool:
        """凭证/地址是否齐备；不齐备时发送方法应抛错而不是静默成功。"""

        ...

    def send_private_msg(
        self,
        user_ref,
        text: str,
        *,
        msg_id: str = "",
        msg_seq: int = 1,
    ) -> dict:
        """发一条纯文本私聊消息；`msg_id` / `msg_seq` 用于被动回复。"""

        ...

    def send_private_image(
        self,
        user_ref,
        image_path: str,
        caption: str = "",
        *,
        msg_id: str = "",
        msg_seq: int = 1,
    ) -> dict:
        """发一张本地图片，可带一条配文。"""

        ...

    def send_rich_items(
        self,
        user_ref,
        items: Iterable[RichItem],
        *,
        msg_id: str = "",
        msg_seq: int = 1,
        pause=None,
    ) -> None:
        """按序发一组图文。

        官方通道受「同一条消息最多回 4 次」限制，必须把图与配文合并成一条，
        并对总条数做预算。
        """

        ...

    def get_login_info(self) -> dict:
        """查自身登录态；官方通道返回机器人自身信息。"""

        ...


def official_uid(openid: str) -> int:
    """openid → 稳定的 63 位正整数（落进 `qq_outbox.sender` 的 INTEGER 列）。

    同一 openid 在任意进程/重启后都得到同一个值；碰撞概率按 64 位 birthday
    bound 计算，单用户场景可忽略。空 openid 返回 0（表示未知身份，调用方
    应丢弃而不是入库）。
    """

    text = str(openid or "").strip()

    if not text:
        return 0

    digest = hashlib.sha256(("qq-official:" + text).encode("utf-8")).digest()

    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF
