# Web 控制台

这是 TypeScript/Vite 控制面界面，提供任务导航、FIFO 审批收件箱、审批路由、受控资源、Token/费用/重试预算和脱敏配置管理。专业 Agent 工作台仍通过 Adapter 提供的深链打开。

首次启动请先阅读[安装与启动指南](../docs/getting-started.md)。前端依赖 `18080` 上的后端 API；仅启动 Vite 会导致界面请求失败。

从仓库根目录运行：

```bash
npm --prefix frontend ci
npm --prefix frontend run dev
```

浏览器访问 <http://127.0.0.1:18180/>。默认情况下，Vite 将 `/api`、`/healthz` 和 `/readyz` 代理到 `http://127.0.0.1:18080`；可在启动前通过 `HARNESS_BACKEND_URL` 覆盖目标。

质量检查：

```bash
npm --prefix frontend run check
npm --prefix frontend run build
npm --prefix frontend run test:e2e
```

浏览器测试位于 `frontend/e2e/`，需要已安装的 Playwright Chromium。`npm run preview` 有意不固定 host/port；本地 Playwright 使用 `preview:local`，生产栈测试运行器会注入随机地址。
