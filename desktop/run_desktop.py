"""Unimere 桌面版入口。

一个进程里同时跑：
  - api（uvicorn，线程）
  - scheduler（线程）
  - outbox（线程）
  - pywebview 窗口（主线程，必须）

并处理三件打包特有的事：
  1. 数据目录改成 %APPDATA%\\PersonalAgent（装到 Program Files 也能写）；
  2. edge_tts 的子进程调用：打包后 sys.executable 是 exe 自己，
     所以要在入口拦住 `-m edge_tts` 这种参数，转去跑 CLI；
  3. 前端由后端托管（见 src/main.py 的 _mount_frontend）。

开发模式也能直接跑：python desktop/run_desktop.py
"""

import argparse
import os
import sys
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

APP_NAME = "PersonalAgent"
HOST = "127.0.0.1"
PORT = 8000


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _data_dir() -> str:
    """打包版写 %APPDATA%，开发版使用项目 data 目录（和 run-dev 一致）。"""
    if _is_frozen():
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, APP_NAME)
    else:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

    os.makedirs(path, exist_ok=True)

    return path


ENV_TEMPLATE = """# Unimere 配置
# 填入你自己的 DeepSeek（或任意 OpenAI 兼容）API Key 后重启本程序。

OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.deepseek.com
MODEL=deepseek-chat

# 可选：通知推送
NTFY_SERVER=
NTFY_TOPIC=
"""


def _prepare_env() -> None:
    data = os.environ.get("PERSONAL_AGENT_DATA_DIR") or _data_dir()
    os.makedirs(data, exist_ok=True)

    os.environ.setdefault("PERSONAL_AGENT_DATA_DIR", data)
    os.chdir(data)

    # 打包版没有控制台，把运行日志写进数据目录，
    # 出问题（比如「无法创建会话」）时能直接看到 traceback。
    import logging

    logging.basicConfig(
        filename=os.path.join(data, "app.log"),
        filemode="a",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )

    env_path = os.path.join(data, ".env")

    # 首次启动：生成模板，让用户自己填 key（不把开发者的 key 打进安装包）
    if not os.path.exists(env_path):
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write(ENV_TEMPLATE)

    # 再从数据目录加载：src/agent.py 自己的 load_dotenv 在打包后
    # 会指向 _internal/.env（不存在），所以这里先注入环境变量。
    try:
        from dotenv import load_dotenv  # noqa: PLC0415

        load_dotenv(env_path, override=False)
    except Exception:  # noqa: BLE001
        pass


def _run_edge_tts_cli() -> bool:
    """打包后拦截 `exe -m edge_tts ...`：转成真正的 CLI 调用。

    src/tts_api.py 里用 `[sys.executable, "-m", "edge_tts", ...]` 起子进程，
    打包后 sys.executable 指向 PersonalAgent.exe 自己，不加这个拦截就会
    无限递归/直接失败。
    """

    argv = sys.argv

    if len(argv) >= 4 and argv[1] == "-m" and argv[2] == "edge_tts":
        sys.argv = [argv[0], *argv[3:]]

        import edge_tts.__main__ as cli  # noqa: PLC0415

        cli.main()

        return True

    return False


def _start_api() -> "object":
    import uvicorn  # noqa: PLC0415

    from src.main import app  # noqa: PLC0415

    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        log_level="warning",
        access_log=False,
        # 打包后 uvicorn 的 LOGGING_CONFIG 里引用的 formatter 在冻结环境
        # 解析不到（ValueError: Unable to configure formatter 'default'），
        # 干脆让它别接管日志，用 logging 默认配置即可。
        log_config=None,
    )

    server = uvicorn.Server(config)

    threading.Thread(target=server.run, daemon=True).start()

    # 等 API 真正能响应，再开窗口
    import urllib.request  # noqa: PLC0415

    deadline = time.time() + 30

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://{HOST}:{PORT}/health", timeout=2
            ) as resp:
                if resp.status == 200:
                    return server
        except Exception:  # noqa: BLE001 - 还没起来就继续等
            time.sleep(0.2)

    return server


def _start_workers() -> None:
    """把原来两个独立进程塞进线程。它们内部都是 while True 循环。"""

    try:
        from src import scheduler  # noqa: PLC0415

        threading.Thread(target=scheduler.main, daemon=True).start()
    except Exception:  # noqa: BLE001 - 缺配置时不该挡住窗口
        pass

    try:
        from src.core import outbox_service  # noqa: PLC0415

        threading.Thread(target=outbox_service.main, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def _log(msg: str) -> None:
    """打包版没有控制台，所以把关键步骤写进数据目录的 boot.log。"""
    try:
        path = os.path.join(_data_dir(), "boot.log")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001 - 日志失败不能影响启动
        pass


STARTUP_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif; color: #17243d;
    background: radial-gradient(circle at 30% 20%, #eef1ff, #f7f9fd 55%, #eef2f8); }
  main { text-align: center; }
  .mark { width: 76px; height: 76px; margin: 0 auto 24px; display: grid;
    place-items: center; border-radius: 22px; color: white; font-size: 28px;
    font-weight: 750; letter-spacing: -2px;
    background: linear-gradient(145deg, #123f46, #24656a);
    box-shadow: 0 18px 44px rgba(18, 63, 70, .22); }
  h1 { margin: 0 0 12px; font-size: 28px; }
  p { margin: 0; color: #758099; font-size: 15px; }
  .dot { display: inline-block; width: 7px; height: 7px; margin-right: 9px;
    border-radius: 50%; background: #5db9b4; animation: pulse 1s infinite alternate; }
  @keyframes pulse { to { opacity: .25; transform: scale(.75); } }
</style>
</head>
<body><main><div class="mark">U</div><h1>Unimere｜一泓</h1>
<p><span class="dot"></span>正在准备工作区，请稍候…</p></main></body>
</html>"""


def _initialize_window(window: "object") -> None:
    """后台准备服务，窗口先出现，不再对着桌面等后端导入。"""
    try:
        _start_api()
        _log("api started")
    except Exception as error:  # noqa: BLE001
        _log(f"api FAILED: {error!r}")
        window.load_html(
            "<html lang='zh-CN'><body style='font-family:sans-serif;padding:48px'>"
            "<h2>Unimere 启动失败</h2><p>请查看数据目录中的 boot.log。</p>"
            "</body></html>"
        )
        return

    try:
        _start_workers()
        _log("workers started")
    except Exception as error:  # noqa: BLE001
        _log(f"workers FAILED: {error!r}")

    window.load_url(f"http://{HOST}:{PORT}/")
    _log("frontend loaded")


def main() -> None:
    global PORT

    if _run_edge_tts_cli():
        return

    parser = argparse.ArgumentParser(description="启动 Unimere 桌面版")
    parser.add_argument("--port", type=int, default=PORT, help="本地 API 端口")
    parser.add_argument("--data-dir", default="", help="已有配置和数据目录")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    PORT = args.port
    if args.data_dir:
        os.environ["PERSONAL_AGENT_DATA_DIR"] = os.path.abspath(args.data_dir)

    _log("boot start")

    try:
        _prepare_env()
        _log(f"env ready, data={os.environ['PERSONAL_AGENT_DATA_DIR']}")
    except Exception as error:  # noqa: BLE001
        _log(f"prepare_env FAILED: {error!r}")
        raise

    import webview  # noqa: PLC0415

    _log("creating window")
    window = webview.create_window(
        "Unimere",
        html=STARTUP_HTML,
        width=1320,
        height=860,
        min_size=(980, 640),
    )
    threading.Thread(
        target=_initialize_window,
        args=(window,),
        daemon=True,
    ).start()

    webview.start()

    _log("window closed, exiting")

    # 窗口关掉就结束整个进程（后台线程是 daemon，会跟着退）
    os._exit(0)


if __name__ == "__main__":
    main()
