# QQ 官方机器人接入说明

> 2026-11-14：QQ 入口已统一为官方机器人。旧 NapCat / OneBot 客户端与看门狗
> 归档在 `legacy/qq-onebot/`，运行时不提供通道切换分支。

官方通道使用 WebSocket 网关接收 C2C 单聊、API v2 回复。机器人是独立身份，
不占用个人 QQ 号；网关是本机主动建立的出站连接，不需要公网 IP 或内网穿透。

## 一、一次性准备

1. 打开 [QQ 开放平台](https://q.qq.com/)，用手机 QQ 扫码注册/登录；
2. 创建机器人，在机器人页面取得 **AppID** 与 **AppSecret**；
3. 消息接收方式选择 WebSocket；
4. 用手机 QQ 搜索并添加机器人为好友（C2C 单聊要求用户侧主动添加）。

## 二、配置项

在项目 `data/.env` 配置：

```dotenv
PA_QQ_ENABLED=1
PA_QQ_APP_ID=你的AppID
PA_QQ_APP_SECRET=你的AppSecret
PA_QQ_OWNER_OPENID=
PA_QQ_NOTIFY=0
```

| 变量 | 说明 |
|---|---|
| `PA_QQ_ENABLED` | `1` 时处理 QQ 入站消息 |
| `PA_QQ_APP_ID` | 官方机器人 AppID |
| `PA_QQ_APP_SECRET` | 官方机器人 AppSecret |
| `PA_QQ_OWNER_OPENID` | 白名单：只处理这一个 openid；首次可先留空 |
| `PA_QQ_NOTIFY` | 任务完成主动通知开关，默认 `0` 关闭 |

### 首次获取 openid

`PA_QQ_OWNER_OPENID` 留空时，消息会被丢弃，日志打印收到的 openid：

```text
QQ 官方：未配置 PA_QQ_OWNER_OPENID，消息已丢弃；收到 openid=xxxxxxxx
```

用手机给机器人发一条消息，把日志中的 openid 填回 `.env` 并重启。
没配白名单就不处理任何消息，这是安全默认值。

## 三、启动与验收

1. 用 `scripts/run-dev.ps1` 或惯用的 uvicorn 命令启动；
2. 日志出现 `QQ 官方网关已启动（出站长连）` 与 `QQ 官方网关就绪`；
3. 手机 QQ 私聊机器人发「现在几点」→ 应收到回复；
4. 发「杭州三日游攻略，图文结合」→ 一条生成提示加不超过 3 条图文，
   总计不超过 4 条被动回复，每页真实照片与配文在同一条消息中；
5. 非白名单 openid → 丢弃、不入队、不回复；重复 `msg_id` → 只入队一次。

2026-11-14 联调已通过：官方凭证与网关、普通单聊、三页真实配图、回复序号、
非白名单拦截和重复消息去重。主动通知保持关闭。

## 四、官方通道的硬限制

| 限制 | 数值 | 影响 |
|---|---|---|
| 被动回复窗口 | 用户发消息后 60 分钟 | 超时再回会报 `40034005` |
| 被动回复次数 | 同一条用户消息最多 4 次 | 提示与正式回复共享递增 `msg_seq` 和条数预算 |
| 主动消息 | 单聊每机器人每天对同一用户 2 条，全账户每天 200 条 | 主动通知默认关闭 |
| 图片上限 | 10 MB（`file_data` base64） | 超限直接拒绝，不发半张 |
| 群聊 | 未接入 | 只处理 C2C 单聊 |

代码里的对应处理：

- `src/qq/official.py` 的 `MAX_REPLIES = 4` 与 `send_rich_items()` 控制回复预算；
- `src/qq/worker.py` 让生成提示、文本、海报和图文共用递增 `msg_seq`；
- 攻略页固定 750×1100，照片先下载到本地，再以 base64 上传，图文同条发送；
- 照片检索失败时允许占位降级，不阻塞整份攻略。

## 五、代码结构与回滚

| 文件 | 职责 |
|---|---|
| `src/qq/transport.py` | 官方发送协议 + `official_uid()` 身份映射 |
| `src/qq/official.py` | token 缓存、文本、图文混排、图片 base64 上传 |
| `src/qq/gateway.py` | 出站 WebSocket 网关：鉴权、心跳、C2C 事件分发 |
| `src/qq/api.py` | 白名单、幂等入队与网关生命周期 |
| `src/qq/worker.py` | 通过 `QQTransport` 发送并控制回复预算 |
| `src/core/qq_notify.py` | 可选的官方主动通知，默认关闭 |

openid 是字符串，而 `qq_outbox.sender` 是 INTEGER。系统用 `official_uid(openid)`
做稳定哈希，不改表结构；客户端在收到事件时 `bind()` 保存反查关系。

旧通道回滚锚点为 `qq-onebot-final`，归档说明见 `legacy/qq-onebot/README.md`。
恢复旧通道属于代码回滚，不是修改环境变量后重启。
