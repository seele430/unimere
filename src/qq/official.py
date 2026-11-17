"""QQ 官方机器人（开放平台 API v2）发送客户端。

只做三件事：换 access_token（带缓存与提前刷新）、发文本、发图文混排。
接收侧在 `gateway.py`，两者共享同一个客户端实例 —— 网关收到事件时
`bind(openid)`，客户端才能把内部的整数 uid 反查回 openid。

边界（写死）：

- 只连 official 域名，超时 15 秒，失败抛 `OfficialError`（中文短句），不自动重试；
- token 提前 60 秒刷新；`expires_in` 缺失时按 7200 秒兜底；
- 被动回复带 `msg_id`（同一条用户消息最多回 4 次），主动消息不带；
- 富媒体走 `file_data`（base64）上传，**不需要公网 URL**；
- 图片上限 10 MB（与官方插件一致），超限直接拒绝，不发半张图。
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request

from pathlib import Path
from typing import Iterable

from src.qq.transport import QQTransportError, official_uid


TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

#: 同一条用户消息最多被动回复 4 次（官方文档口径；新旧文档有 4/5 冲突，取保守值）。
MAX_REPLIES = 4

#: 单张图片上限，与官方 OpenClaw 插件一致。
MAX_IMAGE_BYTES = 10 * 1024 * 1024

DEFAULT_TIMEOUT = 15.0
TOKEN_SAFETY_SECONDS = 60.0

_log = logging.getLogger(__name__)


class OfficialError(QQTransportError):
    """官方接口调用失败（中文短句）。"""


class OfficialBotClient:
    """实现 `QQTransport` 的官方通道发送端。"""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        owner_openid: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        clock=time.time,
        opener=None,
    ):
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.timeout = timeout
        self._clock = clock
        self._opener = opener or urllib.request.urlopen
        self._token = ""
        self._token_expires_at = 0.0
        self._openids: dict[int, str] = {}

        if (owner_openid or "").strip():
            self.bind(owner_openid)

    # ---------- 身份 ----------

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def max_replies(self) -> int:
        """被动回复预算：同一条用户消息最多回几条。"""

        return MAX_REPLIES

    def bind(self, openid: str) -> int:
        """登记一个 openid，返回它的内部 uid（用于写进 `sender` 列）。"""

        text = str(openid or "").strip()

        if not text:
            return 0

        uid = official_uid(text)
        self._openids[uid] = text

        return uid

    def _openid_of(self, user_ref) -> str:
        if isinstance(user_ref, str) and not user_ref.isdigit():
            text = user_ref.strip()

            if text:
                self._openids.setdefault(official_uid(text), text)

                return text

        uid = int(user_ref or 0)
        openid = self._openids.get(uid) or ""

        if not openid:
            raise OfficialError("未知的 QQ 用户：openid 尚未绑定（先让对方发一条消息）")

        return openid

    # ---------- HTTP ----------

    def _access_token(self) -> str:
        now = float(self._clock())

        if self._token and now < self._token_expires_at - TOKEN_SAFETY_SECONDS:
            return self._token

        if not self.configured:
            raise OfficialError("未配置 PA_QQ_APP_ID / PA_QQ_APP_SECRET")

        body = self._request(
            "POST",
            TOKEN_URL,
            {"appId": self.app_id, "clientSecret": self.app_secret},
            auth=False,
        )
        token = str((body or {}).get("access_token") or "")

        if not token:
            raise OfficialError("官方鉴权失败：没有返回 access_token")

        try:
            expires_in = float((body or {}).get("expires_in") or 7200)

        except (TypeError, ValueError):
            expires_in = 7200.0

        self._token = token
        self._token_expires_at = now + max(60.0, expires_in)

        return token

    def _request(self, method: str, url: str, payload: dict | None, *, auth: bool = True) -> dict:
        headers = {"Content-Type": "application/json"}

        if auth:
            headers["Authorization"] = f"QQBot {self._access_token()}"
            headers["X-Union-Appid"] = self.app_id

        data = None

        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )

        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")

        except urllib.error.HTTPError as error:
            detail = ""

            try:
                detail = error.read().decode("utf-8", "replace")[:200]

            except Exception:  # noqa: BLE001 - 读错误体失败不影响报错
                detail = ""

            raise OfficialError(
                f"官方接口 HTTP {error.code}" + (f"：{detail}" if detail else "")
            ) from None

        except (urllib.error.URLError, OSError) as error:
            raise OfficialError(f"连不上官方接口：{error}") from None

        if not raw.strip():
            return {}

        try:
            return json.loads(raw)

        except json.JSONDecodeError:
            raise OfficialError("官方接口返回的不是 JSON") from None

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", f"{API_BASE}{path}", payload)

    def access_token(self) -> str:
        """网关鉴权帧要用；与发送共用同一份 token 缓存。"""

        return self._access_token()

    def gateway_url(self) -> str:
        """拿 wss 地址（官方 `/gateway` 接口）。"""

        body = self._request("GET", f"{API_BASE}/gateway", None)
        url = str((body or {}).get("url") or "")

        if not url:
            raise OfficialError("官方接口没有返回网关地址")

        return url

    # ---------- 发送 ----------

    def send_private_msg(
        self,
        user_ref,
        text: str,
        *,
        msg_id: str = "",
        msg_seq: int = 1,
    ) -> dict:
        content = str(text or "").strip()

        if not content:
            raise OfficialError("消息内容不能为空")

        openid = self._openid_of(user_ref)
        body: dict = {"content": content, "msg_type": 0}

        if msg_id:
            body["msg_id"] = str(msg_id)
            body["msg_seq"] = int(msg_seq)

        return self._post(f"/v2/users/{openid}/messages", body)

    def _upload_image(self, openid: str, image_path: str) -> str:
        path = Path(image_path).resolve()

        if not path.is_file():
            raise OfficialError(f"图片文件不存在：{path.name}")

        size = path.stat().st_size

        if size <= 0:
            raise OfficialError("图片是空文件")

        if size > MAX_IMAGE_BYTES:
            raise OfficialError(
                f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB 上限（{size // 1024} KB）"
            )

        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        body = self._post(
            f"/v2/users/{openid}/files",
            {"file_type": 1, "file_data": encoded, "srv_send_msg": False},
        )
        file_info = str((body or {}).get("file_info") or "")

        if not file_info:
            raise OfficialError("官方接口没有返回 file_info")

        return file_info

    def send_private_image(
        self,
        user_ref,
        image_path: str,
        caption: str = "",
        *,
        msg_id: str = "",
        msg_seq: int = 1,
    ) -> dict:
        openid = self._openid_of(user_ref)
        file_info = self._upload_image(openid, image_path)
        body: dict = {
            "msg_type": 7,
            "media": {"file_info": file_info},
            "content": str(caption or "").strip(),
        }

        if msg_id:
            body["msg_id"] = str(msg_id)
            body["msg_seq"] = int(msg_seq)

        return self._post(f"/v2/users/{openid}/messages", body)

    def send_rich_items(
        self,
        user_ref,
        items: Iterable[tuple],
        *,
        msg_id: str = "",
        msg_seq: int = 1,
        pause=None,
    ) -> None:
        """把一组图文压进「同一条消息最多回 4 次」的预算内再发。

        规则（可单测）：
        1. 每条图片占一条（配文并入该条 `content`）；
        2. 纯文本各占一条；
        3. 超预算时**先合并文本、再丢多余图片**，绝不静默截断图片；
        4. `msg_seq` 从 `msg_seq` 起递增，避免「同 msg_id + 同 msg_seq」重复失败。
        """

        # `msg_seq > 1` 表示此前已经发过「生成中」等提示；这些提示也占用同一
        # msg_id 的 4 条被动回复预算，因此这里只能发送剩余条数。
        budget = (
            max(1, MAX_REPLIES - int(msg_seq) + 1)
            if msg_id
            else MAX_REPLIES
        )
        messages, dropped = plan_messages(list(items or []), budget=budget)

        if dropped:
            _log.warning("QQ 官方通道：%s 张图片超出被动回复预算被丢弃", dropped)

        sent = 0

        for offset, message in enumerate(messages):
            if offset and pause is not None:
                pause(offset)

            seq = int(msg_seq) + offset

            try:
                if message["kind"] == "image":
                    self.send_private_image(
                        user_ref,
                        message["path"],
                        message.get("caption") or "",
                        msg_id=msg_id,
                        msg_seq=seq,
                    )
                else:
                    self.send_private_msg(
                        user_ref, message["text"], msg_id=msg_id, msg_seq=seq
                    )
            except QQTransportError as error:
                error.sent_count = sent

                raise

            sent += 1

    def get_login_info(self) -> dict:
        return self._request("GET", f"{API_BASE}/users/@me", None)


def plan_messages(items: list, budget: int = MAX_REPLIES) -> tuple[list[dict], int]:
    """把 items 规整成不超过 budget 条消息；返回 (消息列表, 丢弃的图片数)。

    item 形状：
    - `("text", 文本)`
    - `("image", 本地路径, 配文)`
    """

    messages: list[dict] = []

    for item in items:
        if not item:
            continue

        kind = str(item[0])

        if kind == "image" and len(item) >= 2:
            messages.append(
                {
                    "kind": "image",
                    "path": str(item[1]),
                    "caption": str(item[2]).strip() if len(item) > 2 else "",
                }
            )
        elif kind == "text" and len(item) >= 2:
            text = str(item[1]).strip()

            if text:
                messages.append({"kind": "text", "text": text})

    dropped = 0

    while len(messages) > max(1, int(budget)):
        text_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index]["kind"] == "text"
            ),
            None,
        )

        if text_index is None:
            # 没有文本可合并 → 丢最后一张图，并如实计数。
            messages.pop()
            dropped += 1

            continue

        text = messages.pop(text_index)["text"]

        if text_index > 0:
            previous = messages[text_index - 1]

            if previous["kind"] == "image":
                previous["caption"] = (previous["caption"] + "\n" + text).strip()
            else:
                previous["text"] = (previous["text"] + "\n" + text).strip()
        else:
            following = messages[0]

            if following["kind"] == "image":
                following["caption"] = (text + "\n" + following["caption"]).strip()
            else:
                following["text"] = (text + "\n" + following["text"]).strip()

    return messages, dropped
