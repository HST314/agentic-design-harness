# 工作台 F5 回归与发布验收

F5 将 RFC v0.3 的 F0–F4 能力收拢为一个可重复、与提交绑定的发布门禁，不新增另一套业务事实源。完整入口为：

```bash
make g5-e2e IMAGE_AGENT_ROOT=../image_agent_mvp
```

候选代码必须先提交且工作树保持干净。门禁依次执行全量质量检查、真实 Image 人工审批闭环、三实例与恢复闭环、Playwright 浏览器回归，以及生产构建连接真实 Image 进程的无浏览器 API Mock 闭环。完成后生成：

- `build/g5-gate-result.json`：候选提交、分支、五个阶段的退出码、耗时与日志摘要。
- `build/phase1-evidence.json`：RFC v0.2 的 18 条验收证据索引。
- `build/workbench-f5-evidence.json`：RFC v0.3 的 15 条验收证据索引。

任何阶段失败、工作树变脏、提交或分支变化、证据文件缺失、日志摘要不匹配，都会拒绝生成通过结论。`make evidence` 只能复验已经通过的当前提交，不能替代门禁。

## 门禁矩阵

| 范围 | 本地/CI 命令 | 发布事实 |
| --- | --- | --- |
| 组件与前端 API | `make frontend-unit` | SVG 图标纪律、URL 编码、命令信封、冲突详情和轮询可见性 |
| 后端 API 与恢复 | `make test` | intake、Master、WorkItem、iframe 链接、崩溃窗口和状态恢复 |
| 构建、契约与供应链 | `make verify` | 生成契约、TypeScript/Pyright、构建、漏洞审计、SBOM、密钥和依赖边界 |
| 浏览器与无障碍 | `make frontend-e2e` | F0–F4 路由、刷新恢复、键盘、低动效、1280/1440/1920 和 WCAG 2.1 A/AA 自动审计 |
| 真实 Image 浏览器闭环 | `make frontend-integration` | 生产前端、真实 Harness、真实 Image 进程和本地确定性 Provider；浏览器不拦截 API |
| 双平台 CI | `quality / release-gate` | Linux/Windows 后端、前端单测、构建和 Chromium 回归全部成功，Linux 再执行真实 Image 闭环 |

CI 的 Image 基线固定为 `image_agent_mvp` 1.8.2、提交 `0e559d0153f479c8abefb14613804b8cde486282`。升级该基线必须显式更新工作流、重新运行真实闭环并审阅契约差异；不得使用浮动分支冒充可复现发布。

## RFC v0.3 证据

机器可读矩阵位于 `workbench-f5-evidence-manifest.json`，严格按 RFC 第 12 节的 1–15 条顺序记录声明、执行命令和证据文件。生成器会校验编号完整性、证据存在性，并为每个文件记录 SHA-256。

真实外部 Provider 不属于默认离线发布门禁。需要验证时使用受控凭据执行 `make real-provider-smoke`；不得把未配置凭据导致的跳过描述为通过。
