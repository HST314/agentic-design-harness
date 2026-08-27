# 最新 main 与 PPT Agent 集成分支代码审查报告

日期：2026-08-27  
审查基线：`fc16623c4525bf06b3a7532fb1ac0b81f2d4d24f`  
最新 `main`：`238cfd42dcbf3feb4f496e4f8e7ace2882fb09d9`  
PPT 集成分支：`backend/ppt-agent-integration@113886d`

## 结论

**Verdict：Request changes（当前不应直接合并）。**

最新 `main` 的前端整合和 Image Agent 存活探针修复本身通过了本轮可执行的静态检查、单元测试、构建与 supervisor 集成测试；PPT 集成分支的核心适配、运行时证明和只读边界测试也通过。但两条分支目前仍存在两个融合阻断点：

1. 最新前端仍把所有 PPT WorkItem 硬编码为“能力不可用”，因此即便后端集成成功，用户也无法启动或进入 PPT 工作台。
2. `agents/image_agent_mvp` 在两条分支形成了互不包含的提交历史；任选一侧 revision 都会丢失另一侧能力，必须先在 Image Agent 仓库形成同时包含两边能力的新 release commit。

除此之外，最新 `main` 新增的 React 收件箱存在周期性 N+1 请求和错误吞噬问题；Agent 工作台还删除了已存在的键盘进出与连接状态提示，需要补做浏览器可访问性验证。

## 变更规模与职责差异

| 维度 | 最新 `main`（相对共同基线） | PPT 集成分支（相对共同基线） |
| --- | --- | --- |
| 规模 | 23 文件，`+786/-316` | 57 文件，`+3201/-347` |
| 主要目标 | 工作台壳层与 Inbox React 化、布局压缩、Image 存活探针稳定性 | PPT 子模块、Adapter、运行时证明、只读输入边界、控制面 API、契约与验收 |
| Agent release | Image revision `9972bcc`，package `1.8.7` | Image revision `e39e04b`，package `1.8.6`；新增 PPT revision `042deab` |
| 前端状态 | 保持 PPT “不可用”边界 | 只提供 PPT 前端接入契约，未实现最终 React 交互 |
| 运行时状态 | Image 使用 `/healthz` 存活探针；深度 readiness 保留 `/api/health`；超时/阈值可配置 | 新增 PPT 受管进程、证明、沙箱与交付；仍基于共同基线的旧 supervisor 探针策略 |

## 阻断性发现

### Critical：融合后 PPT 后端可用，但前端仍会无条件拒绝 PPT

位置：

- `frontend/src/features/agent-workbench/AgentWorkbenchPage.tsx:145-148`
- `frontend/src/features/agent-workbench/AgentWorkbenchPage.tsx:279-288`
- `frontend/e2e/agent-workbench.spec.ts` 中的 PPT unavailable 用例
- `docs/ppt-agent-frontend-api.md:58-123`

当前工作台链接查询只在 `item.agent_type === "image"` 时启用；随后又用 `item.agent_type === "ppt"` 直接进入“PPT 工作台暂不可用”分支。最新 `main` 的 E2E 仍明确验证该旧边界。与此同时，PPT 集成分支已经提供受控 `ui-link`、PPT 手动启动、Image `manual_finished` 门禁及相应前端契约。

这会造成明确的端到端断裂：后端能准备、启动和验证 PPT 实例，但用户无法从任务工作台触发或进入它。

要求：

1. 将工作台链接逻辑泛化为受支持的 `image | ppt`，继续只信任 Harness 返回的 `ui_url`、`embeddable` 与 `link_status`。
2. 在 Image 卡片增加可逆 `manual_finished` 控件，并明确它只控制 PPT 启动门禁。
3. 在 PPT 卡片增加受服务端门禁约束的启动入口，展示 `unfinished_instance_ids`。
4. 把当前“PPT 永远不可用”E2E 替换为 READY、STARTING、门禁失败、FRAME_BLOCKED、仅 PPT 任务五组行为测试。

### Critical：Image Agent gitlink 已分叉，不能通过选择某一侧解决冲突

位置：

- `agents/image_agent_mvp`
- `agents/image-agent.lock.json`
- `tests/unit/test_image_lock.py`

两侧 revision 均从 `d65c169` 前进，但互不包含：

- 最新 `main`：`9972bcc`，新增零 I/O `/healthz`。
- PPT 集成分支：`01f9472 -> e39e04b`，新增生成式交付说明及结构校验。

真实合并演练产生 3 个未解决路径：上述 gitlink、release lock 和 lock 单测。若选择 `9972bcc`，PPT 集成所依赖的交付说明能力会丢失；若选择 `e39e04b`，最新 `main` 的 Image 存活探针端点会丢失。

要求：先在 Image Agent 仓库创建同时包含两组提交的新 release commit，验证后再同步更新：

1. `agents/image_agent_mvp` gitlink；
2. `agents/image-agent.lock.json` 的 revision、package version、source hash 与 dependency hashes；
3. `tests/unit/test_image_lock.py` 的固定 revision；
4. Harness 的 lock check、Image 交付回归和存活探针回归。

禁止仅使用 `ours`/`theirs` 解决此冲突。

## 必须修复

### Required：React Inbox 每 10 秒触发最多 101 个请求，并隐藏所有详情错误

位置：`frontend/src/api/queries.ts:110-128`

`inboxQuery` 先读取最多 100 条 Inbox，再对每个带 `approval_id` 的条目并发请求审批详情，且每 10 秒重复一次。单个前端会产生最多 `1 + N` 个请求；多个操作员同时打开 Inbox 时会线性放大控制面负载。

同一逻辑还用无类型 `catch` 把 401、403、5xx、网络错误和真实的“不存在”全部转成 `null`。顶层 query 因此显示成功，界面只提示“审批详情暂时不可用”，监控和用户都无法区分权限、服务故障与数据缺失。

建议结构性修复：

- 首选由 Inbox 列表端点批量返回待审批摘要，消除 N+1；或只在卡片展开/被 deep-link 选中时加载详情。
- 若暂时保留 fan-out，必须设置并发上限，并用 `Promise.allSettled`/显式错误类型保留每条失败原因。
- 只对可预期的 404/410 映射为“详情不存在”；认证、授权和服务端错误必须进入可见错误状态。
- 增加 100 条 Inbox、部分详情 5xx、请求取消和轮询重入测试。

### Required：融合时必须完整保留最新 `main` 的探针语义

位置：

- `backend/harness/adapters/image.py`
- `backend/harness/core/config_kernel.py`
- `backend/harness/services/supervisor.py`
- `backend/harness/services/supervisor_lifecycle.py`
- `config/runtime.yaml`

PPT 集成分支仍是共同基线的旧策略：0.25 秒超时、连续 3 次失败即判崩溃、Image 存活探针使用深度 `/api/health`。最新 `main` 改为 Image `/healthz`、启动期 `/api/health` readiness、默认 2 秒超时、5 次失败阈值，并记录 `last_health_failure`。

三方合并演练显示 supervisor 代码可自动合并并同时保留 PPT 启动与新探针字段，但这不等于功能已经闭环：Image `/healthz` 仍依赖上一条所述的子模块 release 融合。最终分支必须同时验证 Image 与 PPT 的 STARTING、RUNNING、连续失败、恢复和 reconcile 路径。

## 风险与改进建议

### Consider：Agent 工作台删除了明确的键盘进出点和连接状态播报

最新 `main` 删除了：

- “跳到 Image Agent 工作台”按钮；
- “返回工作台操作栏”按钮；
- `aria-live` 连接验证状态；
- 对应的 E2E 键盘焦点断言。

iframe 仍可通过浏览器默认 Tab 顺序访问，但对跨域专业工作台而言，明确的进入/退出路径是更可靠的键盘恢复机制。应在浏览器环境重新执行 WCAG A/AA、Tab/Shift+Tab、iframe 加载失败和 focus mode 回归；若无法证明同等可达性，应恢复等价的跳转/返回控件，而不是仅删除测试。

### Consider：React Inbox 与 legacy Inbox 形成两套实现

`frontend/src/features/inbox/InboxPage.tsx` 新增 265 行 React 实现，但 `frontend/src/main.ts` 中的 `renderInbox`、`renderInboxItem`、`wireInboxActions` 仍保留。旧壳层从实例页触发 legacy 导航时还可能进入旧实现，增加行为漂移和重复请求风险。

建议把 Inbox 行为集中到 React 路由；legacy 壳层只负责跳转，不再维护第二套审批表单与状态更新逻辑。删除前应先确认所有 legacy 深链已被 React Router 覆盖。

## 可直接保留的改动

以下内容在本轮审查中未发现阻断问题，融合时应优先保留：

- 最新 `main` 的 topbar/任务标签、React Inbox 路由和紧凑工作区布局方向；
- `/healthz` 与 `/api/health` 的 liveness/readiness 分离；
- probe timeout、failure threshold 和 `last_health_failure` 诊断信息；
- PPT Adapter 的受证明运行时、只读共享输入镜像、实例私有空输入和写沙箱；
- PPT release lock、依赖锁校验、契约字段和控制面 API；
- Image 人工结束门禁的可逆语义，以及服务端对未结束实例列表的权威校验。

## 推荐融合顺序

1. 在 Image Agent 仓库合成新的 release commit，同时包含 `/healthz` 与交付说明生成/校验。
2. 以最新 `main` 为新集成基线，合入 PPT 后端分支，手工解决 gitlink、lock JSON 和 lock 单测。
3. 核验自动合并后的 supervisor 同时保留 PPT 分支逻辑和最新探针配置。
4. 在同一集成分支实现 PPT React 工作台、Image `manual_finished` 控件和 PPT 启动门禁交互。
5. 修复 Inbox N+1/错误建模；确认并收敛 legacy Inbox。
6. 执行完整 `make check`、PPT acceptance、Image 真实受管交付回归和前端浏览器 E2E，再进入合并评审。

## 本轮验证证据

| 分支 | 验证项 | 结果 |
| --- | --- | --- |
| 最新 `main` | TypeScript `tsc --noEmit` | 通过 |
| 最新 `main` | Vitest | 6 files / 20 tests 通过 |
| 最新 `main` | Vite production build | 通过 |
| 最新 `main` | `tests.integration.test_supervisor` | 16 tests 通过 |
| PPT 集成分支 | TypeScript、Vitest、Vite build | 通过（6 files / 20 tests） |
| PPT 集成分支 | PPT adapter、attestation、readonly boundary | 6 tests 通过 |
| 三方合并演练 | 自动融合检查 | 3 个未解决路径；supervisor 自动合并 |
| 最新 `main` | Playwright 相关前端 E2E | **未验证**：本机缺少 Playwright Chromium 可执行文件，20 个用例均在浏览器启动前终止；不能计为代码失败或通过 |

## 最终质量门

满足以下条件后再把结论改为 Approve：

- [ ] PPT WorkItem 不再被前端硬编码阻断，并覆盖受控 ui-link 全状态。
- [ ] Image Agent 新 release 同时包含两条分支能力，lock 与 gitlink 一致。
- [ ] Inbox 不再周期性 N+1，详情错误可观测。
- [ ] Image/PPT supervisor 探针、恢复和 reconcile 回归通过。
- [ ] 浏览器 E2E 与 WCAG 自动审计在具备浏览器运行文件的环境中通过。
- [ ] 完整 `make check` 与 PPT acceptance 通过。
