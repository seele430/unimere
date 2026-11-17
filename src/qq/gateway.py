"""QQ 官方机器人 WebSocket 网关（出站长连，**不需要公网 IP**）。

协议流程（官方 API v2）：

1. `GET /gateway` 拿 wss 地址（鉴权后）；
2. 连上后发 op=2 鉴权帧：`{"op":2,"d":{"token":"QQBot <token>","intents":<1<<25>,"shard":[0,1]}}`；
3. 收 op=10 `HELLO` 拿心跳间隔，此后每 `heartbeat_interval` 发一次 op=1 心跳；
4. 收 op=0 事件，`C2C_MESSAGE_CREATE` 交给回调；op=11 是心跳 ack；
5. 服务端要求重连（op=7）或连接断开 → 指数退避重连。

边界（写死）：

- **不做 RESUME**：重连后重新鉴权，可能重放少量事件；由 `outbox` 的
  `message_id` 主键做幂等兜底（重复投递返回 False），不会重复回复；
- 回调用网关线程同步调用，回调里的异常只记日志，不打断收流；
- 一期只处理单聊（`C2C_MESSAGE_CREATE`），群/频道事件计数后忽略。
"""

from __future__ import annotations

import json
import logging
import threading
import time


#: 群聊 + 单聊事件的 intents 位。
INTENT_GROUP_AND_C2C = 1 << 25

_log = logging.getLogger(__name__)


def default_connect(url: str):
    """真实连接器；测试通过 `connector=` 注入假连接。"""

    from websockets.sync.client import connect

    return connect(url, open_timeout=15)


class OfficialGateway:
    def __init__(
        self,
        client,
        on_message,
        *,
        heartbeat: float = 30.0,
        clock=time.time,
        connector=None,
        sleep=time.sleep,
        max_backoff: float = 60.0,
    ):
        self.client = client
        self.on_message = on_message
        self.heartbeat = float(heartbeat)
        self._clock = clock
        self._connector = connector or default_connect
        self._sleep = sleep
        self.max_backoff = max_backoff
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_seq = 0
        self._session_id = ""
        self.stats = {"events": 0, "c2c": 0, "ignored": 0, "reconnects": 0}

    # ---------- 生命周期 ----------

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False

        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name="qq-official-gateway", daemon=True
        )
        self._thread.start()

        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()

        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def run_forever(self) -> None:
        backoff = 1.0

        while not self._stop.is_set():
            try:
                self._connect_once()
                backoff = 1.0
            except Exception as error:  # noqa: BLE001 - 任何断开都退避重连
                self.stats["reconnects"] += 1
                _log.warning("QQ 官方网关断开：%s（%.0f 秒后重连）", error, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)

    # ---------- 连接 ----------

    def _connect_once(self) -> None:
        url = self.client.gateway_url()
        _log.info("QQ 官方网关连接 %s", url)

        with self._connector(url) as ws:
            self._authenticate(ws)

            while not self._stop.is_set():
                try:
                    raw = ws.recv(timeout=self.heartbeat)

                except TimeoutError:
                    ws.send(json.dumps({"op": 1, "d": self._last_seq}))

                    continue

                if raw is None:
                    raise ConnectionError("网关连接已关闭")

                try:
                    frame = json.loads(raw)

                except json.JSONDecodeError:
                    _log.warning("忽略非法网关帧")

                    continue

                self._dispatch(frame)

    def _authenticate(self, ws) -> None:
        payload = {
            "op": 2,
            "d": {
                "token": f"QQBot {self.client.access_token()}",
                "intents": INTENT_GROUP_AND_C2C,
                "shard": [0, 1],
            },
        }
        ws.send(json.dumps(payload))

    def _dispatch(self, frame: dict) -> None:
        op = frame.get("op")

        if op == 0:
            self._last_seq = int(frame.get("s") or self._last_seq)
            self.stats["events"] += 1
            event_type = str(frame.get("t") or "")

            if event_type == "READY":
                self._session_id = str((frame.get("d") or {}).get("session_id") or "")
                _log.info("QQ 官方网关就绪，session=%s", self._session_id)

                return

            self._handle_event(event_type, frame.get("d") or {})
        elif op == 10:
            interval = (frame.get("d") or {}).get("heartbeat_interval")

            if interval:
                self.heartbeat = max(1.0, float(interval) / 1000.0)
        elif op == 7:
            raise ConnectionError("服务端要求重连")
        # op 11 = 心跳 ack，忽略。

    def _handle_event(self, event_type: str, data: dict) -> None:
        if event_type == "C2C_MESSAGE_CREATE":
            self.stats["c2c"] += 1
            self._on_c2c(data)

            return

        self.stats["ignored"] += 1

    def _on_c2c(self, data: dict) -> None:
        author = data.get("author") or {}
        openid = str(author.get("user_openid") or "").strip()
        text = str(data.get("content") or "").strip()
        message_id = str(data.get("id") or "").strip()

        if not (openid and text and message_id):
            self.stats["ignored"] += 1

            return

        self.client.bind(openid)

        try:
            self.on_message(openid, message_id, text)
        except Exception:  # noqa: BLE001 - 单条处理失败不打断收流
            _log.exception("处理 QQ 官方消息失败：%s", message_id)
