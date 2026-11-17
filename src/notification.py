import os
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(Path(os.getenv("PERSONAL_AGENT_DATA_DIR", BASE_DIR / "data")) / ".env")
load_dotenv(BASE_DIR / ".env")  # 兼容旧版根目录配置，不覆盖已有环境变量。


def send_notification(message: str) -> bool:
    server = os.getenv(
        "NTFY_SERVER",
        "https://ntfy.sh"
    ).rstrip("/")

    topic = os.getenv("NTFY_TOPIC")

    if not topic:
        raise ValueError("未配置 NTFY_TOPIC")

    if not message.strip():
        raise ValueError("通知内容不能为空")

    response = requests.post(
        f"{server}/{topic}",
        data=message.encode("utf-8"),
        headers={
            "Content-Type": "text/plain; charset=utf-8"
        },
        timeout=15
    )

    response.raise_for_status()
    return True