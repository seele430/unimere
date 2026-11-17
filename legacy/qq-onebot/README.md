# qq-onebot/（2026-11-14 归档）

## 归档原因

NapCat / OneBot 小号通道已停用，主代码统一使用 QQ 开放平台官方机器人：
官方 WebSocket 网关接收 C2C 单聊，官方 API v2 回复。旧通道依赖小号登录态，
存在反复掉线问题；保留两套运行分支也会重复消费消息并增加维护成本。

本目录仅作留档，不可直接导入运行。`onebot.py`、`launch_watcher.py` 以及对应
端点/看门狗测试保留原始 import，便于对照历史实现。

## 最后可用 commit

回滚锚点：`qq-onebot-final`（commit `6d1bde2`）。
该标签指向归档操作所在的原始 HEAD；官方接入及联调修复的后续改动不以该标签为恢复目标。

## 恢复步骤

1. 在归档前分支或新恢复分支上确认标签存在：`git tag --list qq-onebot-final`。
2. 仅恢复旧客户端与看门狗：

   ```bash
   git checkout qq-onebot-final -- src/qq/onebot.py src/qq/launch_watcher.py
   ```

3. 完整恢复旧通道还需同时恢复归档前的 `src/qq/api.py`、`src/main.py`、
   `src/core/qq_notify.py`、`src/core/outbox_worker.py` 及相关测试。最可靠的方式是
   从 `qq-onebot-final` 新建分支，不要把本次其他修复一并覆盖。
4. 恢复后重新配置旧通道环境变量并运行 `scripts/test.ps1 backend`。

归档后运行时只走官方通道，不再提供通过环境变量切回旧通道的分支。
