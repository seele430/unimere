# Unimere 前端

Unimere 的网页端，使用 React 19、TypeScript 和 Vite。包含对话、提醒、任务、产物、活动时间线和平台控制台等界面。

完整启动方式见仓库根目录的 `README.md`。前端开发服务器把 `/api` 代理到 `http://127.0.0.1:8000`，联调时需要先准备后端。

## 开发

```powershell
npm install
npm run dev
```

默认地址为 <http://127.0.0.1:5173>。也可以从仓库根目录运行 `.\scripts\run-dev.ps1`，同时启动前端和后端服务。

## 检查与构建

```powershell
npm run lint
npm run build
```

## 端到端测试

测试位于 `tests/`，使用 Playwright：

```powershell
npx playwright test
npx playwright test tests/console.spec.ts
```

测试配置会启动 Vite 开发服务器；如果 5173 端口已有服务，则复用该服务。涉及后端接口的用例需要先按根目录文档准备运行环境。
