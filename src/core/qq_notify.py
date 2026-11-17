"""QQ 通知发送：使用官方机器人，仅发给白名单 owner。

边界：
- 只支持 owner 单聊，不支持群/多收件人；
- 正文截断 1000 个 Unicode 字符；
- 配置错误抛 ValueError（永久失败），官方接口错误抛 OfficialError（可重试）；
- AppSecret 不写日志或异常；主动消息不带 msg_id，受官方主动消息限额。
"""

from __future__ import annotations

import os

from src.qq.official import OfficialBotClient, OfficialError


MAX_CHARS = 1000


def _qq_owner() -> str:
    owner = (os.getenv("PA_QQ_OWNER_OPENID") or "").strip()

    if not owner:
        raise ValueError("PA_QQ_OWNER_OPENID 未配置")

    return owner


def send_qq_notification(message, *, client=None) -> None:
    text = str(message or "").strip()

    if not text:
        raise ValueError("通知内容不能为空")

    text = text[:MAX_CHARS]
    owner = _qq_owner()

    if client is None:
        app_id = (os.getenv("PA_QQ_APP_ID") or "").strip()
        app_secret = (os.getenv("PA_QQ_APP_SECRET") or "").strip()

        if not app_id or not app_secret:
            raise ValueError("PA_QQ_APP_ID / PA_QQ_APP_SECRET 未配置")

        client = OfficialBotClient(
            app_id=app_id, app_secret=app_secret, owner_openid=owner
        )

    # 主动通知没有可回复的入站消息，因此不带 msg_id。
    client.send_private_msg(owner, text)
