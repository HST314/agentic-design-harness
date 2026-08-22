# Web 控制台

这是 React/TypeScript/Vite 控制面界面。F0 使用 React Router 与 TanStack Query 建立工作台应用壳，新工作台路由和既有控制台采用路由级渐进迁移：

- `/` 与 `/tasks/new` 进入工作台新任务壳；
- `/tasks/:taskId/master|board|plan` 使用统一主任务历史、上下文栏和按需抽屉；
- `/tasks/:taskId/work-items/:workItemId` 对 Image WorkItem 打开受控 iframe 工作台，对 PPT 显示真实不可用边界；
- 既有 `/tasks`、任务详情、实例、收件箱与设置路由继续承载已验收功能。

专业 Agent 工作台仍只接受 Adapter 提供的受控入口，不在前端接收任意用户 URL。实例归属、进程端口 allowlist 和 frame 策略由后端验证，具体边界见 [Image Agent 内嵌工作台](../docs/agent-workbench.md)。

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
npm --prefix frontend run test:unit
npm --prefix frontend run build
npm --prefix frontend run test:e2e
```

组件与 API 单测和源码相邻，以 `*.test.ts(x)` 命名；浏览器测试位于 `frontend/e2e/`，其中工作台核心路由还执行 WCAG 2.1 A/AA 自动审计。浏览器回归需要已安装的 Playwright Chromium。`npm run preview` 有意不固定 host/port；本地 Playwright 使用 `preview:local`，生产栈测试运行器会注入随机地址。
