# Unimere｜一泓

> 万流入泓，诸事归一。
>
> Many flows. One clear space.

[展示页](https://seele430.github.io/) · [演示视频](docs/Personal-Agent-Demo.mp4) · [QQ 接入说明](docs/qq-official.md) · [English](README.en.md)

Unimere 是我给自己用的本地工作台，把网页对话、提醒、定时任务、产物和 QQ 消息放在同一个地方。后端使用 FastAPI 和 SQLite，前端使用 React。

数据库、任务记录和产物默认保存在本机。调用模型、搜索、Edge TTS 等外部服务时，会向对应服务发送请求。

## 功能

- 对话：流式输出，支持提醒、沙箱读写、搜索、图生图等工具，保留工具调用记录。
- Bot 管理：最多创建 3 个 Bot，分别设置行为指令、头像和音色。
- 提醒与任务：定时提醒、应用内推送、邮件和推送通道，以及定时任务、执行历史和健康检查。
- QQ 官方机器人：通过官方 API v2 和出站 WebSocket 网关接收、回复白名单 C2C 单聊，支持意图路由、接力任务和图文混排，不需要公网 IP。
- 产物：登记、预览、下载和版本对比，文件带 SHA-256。
- 活动时间线：把产物、任务执行和模型调用整理为只读事件流。
- 额度与监控：记录模型用量和预算，提供站点健康检查与告警。
- 语音与视频：Edge TTS、faster-whisper CPU 转写、视频要点与关键帧提取。
- 图生图：调用本机 ComfyUI，限制显存、采样步数和并发。
- 平台控制台：查看节点、沙箱和执行情况，支持创建沙箱、提交或取消执行、cordon 和 drain。

## 运行

环境要求：Python 3.12、Node.js，以及一个 DeepSeek API Key。

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

cd frontend
npm install
cd ..

# 在 data\.env 中配置：OPENAI_API_KEY=sk-xxxx

.\scripts\run-dev.ps1
```

脚本会启动 API（8000）、调度器、outbox 和前端（5173）。打开 <http://127.0.0.1:5173>，首次使用时创建站长账号。

停止服务：

```powershell
.\scripts\run-dev.ps1 -Stop
```

演示视频录制于较早版本，界面和功能可能与当前代码不同。

## 使用边界

### 单用户

项目按一台机器、一个站长账号设计。创建账号后没有公开注册入口，也没有 `/auth/register` 或 `/auth/invite` 路由。

需要对外提供平台接口时，配置 `PERSONAL_AGENT_PLATFORM_TOKEN`。反向代理的来源改写与信任设置见 [平台接口约定](docs/platform-contract.md)。

### 远程访问只读

远程守卫先鉴权，再检查 HTTP 方法和路径白名单。写方法会被拒绝，手机端用于查看产物和活动记录，不提供桌面端的写操作。

自签 TLS、一次性登录链接、失败封禁和日志脱敏的实现见 [远程访问威胁模型](docs/threat-model-remote.md)。目前已验证 Android 直连 TLS；nginx/Caddy 反向代理、iOS 证书流程和弱网重连尚未覆盖。

### 删除前预演

数据库记录和文件清理由不同模块处理，默认只生成清理计划。真正删除需要显式确认；CLI 中 `--apply` 必须和 `--confirm` 一起使用。

### GPU 限制

ComfyUI 通过 `comfy-agent/scripts/start-comfyui-safe.ps1` 启动，使用低显存参数，并在调用前检查空闲显存。图生图限制为单张、采样步数不超过 30、超时 120 秒，结束后停止服务。视频转写固定使用 CPU。

这些限制源于本机 6 GB 显存环境下出现过的 `VIDEO_TDR_FAILURE`，详细配置见 [GPU 保护说明](docs/gpu-protection.md)。

## 平台指标口径

- HTTP 202 只表示请求已接收，成功与否按执行终态统计，取消不计入成功率分母。
- `hold` 是控制决策，不代表容量已经变化。
- 实际容量按处于 `accepting` 状态的有效租约计算，不按节点记录数计算。
- 实时数据、进程内采样和 What-if 仿真分开显示；未连接控制器时，只显示计算结果，不表示已经驱动真实容量。

## 测试

后端测试通过受保护运行器执行，SQLite 路径会重定向到临时目录，网络连接受 socket 守卫限制：

```powershell
.\scripts\test.ps1 backend

# 查看测试清单或检查隔离声明
.\.venv\Scripts\python.exe scripts\run_tests.py list
.\.venv\Scripts\python.exe scripts\run_tests.py audit
```

部分 mock provider 用例需要回环连接，不能在 socket 守卫下运行，因此单独执行并保存日志，不计入受保护运行器的通过数。

前端测试：

```powershell
cd frontend
npx playwright test
npx playwright test tests/console.spec.ts
```

其他常用检查：

```powershell
# 端到端验证
.\.venv\Scripts\python.exe scripts\verification\verify_e2e.py

# GPU 保护与文档一致性
.\.venv\Scripts\python.exe scripts\verification\verify_gpu_guard.py
.\.venv\Scripts\python.exe scripts\verification\check_docs_consistency.py
```

## 目录

```text
unimere/
├── src/                 # FastAPI、模型调用、工具和各业务模块
├── frontend/            # React + TypeScript 前端与 Playwright 测试
├── tests/               # 后端测试，清单见 tests/registry.py
├── scripts/             # 起停、测试、备份和验证脚本
├── comfy-agent/         # ComfyUI 安全启动与调用脚本
├── docs/                # 使用文档、设计记录和历史归档
├── legacy/              # 已停用代码，仅作留档
└── data/                # 本地配置、数据库和日志，不纳入版本控制
```

## 已知限制

- 本机 CPU 使用 faster-whisper large-v3 转写长音频时，现有关键词命中测试未达到 95%，详见 [视频转写限制](docs/video-asr-limits.md)。
- 场景回放只记录扩缩容事件，不记录每次 `hold` 决策。
- 控制台命令按空格切分，不解析引号和转义；复杂命令需要走 `/v1` 接口。
- 这是个人项目，不提供多租户、移动端写操作或完整的公网部署方案。

早期 `/v1` 认证与限流问题的修复记录见 `docs/journal.md` 和 `docs/platform-contract.md`。
