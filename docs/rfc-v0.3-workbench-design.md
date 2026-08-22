# Agentic Design Harness 工作台框架设计 v0.3

**状态：** Proposed

**日期：** 2026-08-22

**适用范围：** RFC v0.2 的前端信息架构、任务创建、Master 交互、子任务投影与专业工作台接入扩展

## 1. 结论

本方案把当前只读控制台升级为可创建任务、持续与 Master 协作、审阅计划、查看子任务看板并进入专业 Agent 工作台的桌面端产品框架。

本轮以框架为先，边界如下：

- 左侧固定主任务历史；中间承载新任务、Master、看板、计划和专业工作台；右侧按需展开状态、审批和资源。
- 每个主任务保留永久 Master 会话；子 Agent 不新增聊天，继续使用现有工作流页面。
- 点击 Image 子任务，在 Harness 内 iframe 嵌入现有 Image Agent 工作台；后续专业细节直接丰富该工作台。
- 首期只上传图片、PDF、TXT/MD，且只允许首次创建阶段上传。
- 子任务卡代表稳定逻辑工作项，重试、重启和未来替换 Agent 收纳为卡片下的 attempt/实例历史。
- 看板只投影领域状态，不能通过拖拽改变运行状态。
- 先以 3–5 秒轮询刷新，并为事件流预留边界；本轮只保证桌面端。

方案不改变 RFC v0.2 已验收的进程隔离、资产发布、审批、用量、预算和契约安全边界。

## 2. 已确认决策

| 编号 | 决策 |
| --- | --- |
| 1 | 端到端覆盖前端、Master 契约、上传、任务投影和必要后端改造 |
| 2 | 左侧历史 + 中间工作区 + 右侧按需抽屉 |
| 3 | 默认首页为 Prompt 与附件的新任务页 |
| 4 | 历史按时间分组，支持搜索、置顶、重命名、归档；底部放收件箱/设置 |
| 5 | 每个主任务保留永久 Master 会话 |
| 6 | 创建 DRAFT → 分析/澄清 → 计划预览 → 确认后创建子任务 |
| 7 | 每个任务可选确认后运行/自动运行，默认人工确认 |
| 8 | Master 自动生成标题，允许用户重命名 |
| 9 | 看板只展示当前主任务的子任务 |
| 10 | 卡片代表逻辑子任务，attempt/实例历史收纳在卡片内 |
| 11 | 默认状态看板，并提供阶段/依赖计划视图 |
| 12 | 默认展示少量业务状态，原始状态和错误码进入详情 |
| 13 | Harness 内 iframe 嵌入当前 Image Agent 工作台 |
| 14 | 子 Agent 不做聊天，直接使用现有工作流界面 |
| 15 | 首期上传图片、PDF、TXT/MD |
| 16 | 只允许首次创建上传，后续引用已有资源 |
| 17 | 先 3–5 秒轮询，为 SSE/WebSocket 留接口 |
| 18 | 本轮只保证桌面端 |

## 3. 目标与非目标

### 3.1 目标

1. 用户不依赖 API 即可创建主任务并提交真实附件。
2. DRAFT 从创建开始可恢复、可返回、可取消，浏览器不是唯一事实源。
3. Master 澄清、计划建议、用户补充和计划确认形成持久化线程。
4. 当前主任务的逻辑子任务以可扫描看板展示，并可切换阶段依赖视图。
5. Image 子任务从统一框架直接进入对应现有工作台。
6. 新增写操作继续遵守 actor、幂等键、expected revision 和审计纪律。

### 3.2 非目标

- 不建设通用子 Agent 聊天协议或统一时间流。
- 不在 Harness 重做 Image Agent 已有创作流程。
- 不支持 DOCX、PPTX、XLSX、音频或视频上传。
- 不支持首次创建完成后的追加上传。
- 不承诺移动端适配或触屏看板。
- 不通过拖拽直接改变任务、阶段或实例状态。
- 不在本轮直接建设 SSE/WebSocket。
- 不把 PPT 占位伪装成可运行工作台。

## 4. 信息架构与路由

### 4.1 全局结构

```text
┌────────────────────┬───────────────────────────────────────┬───────────────┐
│ 主任务历史 264px    │ 当前工作区                            │ 按需抽屉 360px │
│                    │                                       │               │
│ + 新任务            │ 新任务 / Master / 看板 / 计划 /       │ 资源 / 审批 /  │
│ 搜索                │ Image Agent 内嵌工作台                │ 状态 / attempt │
│ 今天                │                                       │               │
│ 近 7 天             │                                       │               │
│ 更早                │                                       │               │
│                    │                                       │               │
│ 收件箱 / 设置       │                                       │               │
└────────────────────┴───────────────────────────────────────┴───────────────┘
```

- 左侧栏跨页面稳定存在，进入 Image 工作台后也不消失。
- 中间区只保留一个主上下文，避免全局任务与当前任务层级混淆。
- 抽屉默认关闭，用 query 保存打开对象，刷新可恢复。

### 4.2 建议路由

| 路由 | 用途 |
| --- | --- |
| `/tasks/new` | 默认首页和新任务创建 |
| `/tasks/:taskId/master` | Master 永久会话、澄清、计划预览 |
| `/tasks/:taskId/board` | 当前主任务状态看板 |
| `/tasks/:taskId/plan` | Stage/依赖计划视图 |
| `/tasks/:taskId/work-items/:workItemId` | 子任务入口；Image 类型进入内嵌工作台 |
| `/tasks/:taskId/resources` | 资源完整页 |
| `/tasks/:taskId/approvals` | 审批页 |
| `/tasks/:taskId/usage` | 用量与预算页 |
| `/tasks/:taskId/events` | 审计事件页 |
| `/inbox` | 人工 FIFO 收件箱 |
| `/settings` | 全局配置与凭据 |

抽屉使用 `?drawer=resources|approvals|status&target=<id>`，不能替代可深链完整页面。

### 4.3 左侧历史

- 顶部固定“新任务”和搜索。
- 主任务按“今天 / 近 7 天 / 更早”分组，置顶项位于时间分组前。
- 每项显示标题、业务状态和更新时间；状态不能只靠颜色表达。
- 行菜单提供重命名、置顶、归档。
- 归档只影响导航，不改变任务执行终态；归档任务可搜索并恢复。
- 底部固定收件箱、设置和服务状态。

## 5. 关键页面与流程

### 5.1 新任务页

```text
┌───────────────────────────────────────────────────────────────┐
│ 创建新的设计任务                                              │
│ Master 将先分析目标与材料，再生成计划供确认。                  │
│ ┌───────────────────────────────────────────────────────────┐ │
│ │ Prompt（可见标签）                                        │ │
│ │ 例如：为秋季发布会生成三套主视觉方向……                    │ │
│ └───────────────────────────────────────────────────────────┘ │
│ [添加图片/PDF/TXT/MD]  [文件队列与逐文件说明]                 │
│ 启动方式 ● 确认后运行  ○ 自动运行                   [发送]    │
└───────────────────────────────────────────────────────────────┘
```

创建流程：

1. 本地校验 Prompt、文件类型、大小和数量。
2. 创建可恢复的 `DRAFT` 与上传会话，侧栏立即出现该任务。
3. 最多并发上传 3 个文件，逐文件显示进度、失败重试和取消。
4. 全部附件成功或被移除后提交 intake；提交后关闭上传入口。
5. Master 生成标题并分析材料，需要澄清时进入 Master 工作区。
6. Master 保存 PlanProposal。人工模式等待确认；自动模式校验通过后自动提交并启动。

刷新、断网或部分失败不能产生不可见任务。重新进入 DRAFT 时恢复服务端已上传事实；浏览器文件句柄不能恢复时，明确要求重新选择失败文件。

### 5.2 Master 工作区

- 顶部显示标题、业务状态、Board/Plan 切换和启动策略。
- 中间按时间展示用户消息、Master 澄清、计划版本和提交结果。
- 底部只保留文本输入；根据 16B，创建提交后不显示上传按钮。
- 已上传材料通过“引用已有资源”插入消息，不创建新资产。
- 计划卡展示阶段、逻辑子任务、依赖、Agent 和预期交付。
- 人工模式提供“确认并运行”和“要求调整”；调整通过新消息生成 PlanProposal revision，不直接编辑 JSON。
- 自动模式展示计划与启动记录，但不设置人工确认闸门。
- 未配置真实 MasterGateway 时显示 `MASTER_UNAVAILABLE`，不得伪造回复或计划。

### 5.3 子任务看板

```text
┌───────────────────────────────────────────────────────────────────┐
│ 秋季发布会主视觉       [看板] [计划] [筛选] [资源] [3 个待处理] │
├───────────────┬───────────────┬───────────────┬───────────────────┤
│ 待办 4        │ 运行中 2      │ 待审批 1      │ 已结束 6          │
│               │               │               │ [已完成 | 异常]  │
│ KV 方向 B     │ KV 方向 A     │ KV 方向 C     │ KV 方向 D         │
│ Image · S1    │ Image · S1    │ Image · S1    │ Image · S1        │
│ 等待启动      │ 生成 2/4      │ 等待选主图    │ 3 个交付物        │
└───────────────┴───────────────┴───────────────┴───────────────────┘
```

| 看板位置 | 原始状态示例 | 用户文案 |
| --- | --- | --- |
| 待办 | `CREATED`, `PENDING`, `READY`, `AWAITING_START_CONFIRMATION` | 待规划 / 待启动 / 等待前置 |
| 运行中 | `STARTING`, `RUNNING` | 启动中 / 运行中 |
| 待审批 | `WAITING_APPROVAL` | 等待人工审批，并显示审批类型 |
| 已结束 → 已完成 | `SUCCEEDED`, `SKIPPED` | 已完成 / 已跳过 |
| 已结束 → 异常 | `FAILED`, `FAILED_TO_START`, `BLOCKED_UNAVAILABLE`, `UNAVAILABLE`, `CANCELLED` | 失败 / 不可用 / 已取消 |

聚合继续遵守 RFC v0.2：活动工作优先于审批。并行实例仍在运行时，主任务不能只因另一实例待审批而显示为“待审批”。

卡片至少展示逻辑标题、短 ID、Agent、Stage、业务状态、依赖摘要、当前 attempt/实例摘要、更新时间、交付或异常提醒。菜单只提供合法领域命令，不提供“移动到某列”。

### 5.4 计划视图

- 按 Stage position 从左到右展示阶段泳道和 `depends_on`。
- 展示 required/optional、Agent 可用性和逻辑子任务。
- 与看板使用同一 WorkItem 入口，不产生另一套详情。
- PPT 占位显示“能力未接入”，不能进入伪工作台或标记成功。

### 5.5 Image Agent 内嵌工作台

```text
┌ 主任务历史 ─────┬──────────────────────────────────────────────┐
│                 │ 秋季发布会 / KV 方向 A [运行中] [新标签]    │
│                 ├──────────────────────────────────────────────┤
│                 │                                              │
│                 │        Image Agent 现有工作台 iframe         │
│                 │                                              │
└─────────────────┴──────────────────────────────────────────────┘
```

- Harness 只保留上下文栏、返回看板、业务状态和受控的新标签回退。
- URL 必须来自 `/api/v1/instances/{id}/ui-link` 并通过 Adapter allowlist，前端不接受用户 URL。
- iframe 使用跨源隔离，并按实际工作流最小化权限；若未来同源代理，不能同时开放可移除 sandbox 的危险组合。
- Image Agent 需允许 Harness 来源的 `frame-ancestors`，并验证下载、弹窗和资源加载。
- 初期不读取跨域 iframe DOM。未来同步导航/状态时新增带 origin 校验和协议版本的 `postMessage`。
- 加载失败、无 `ui_url` 或 Agent 不可用时展示真实原因和重试/返回操作，不能显示空白框。
- 专业会话、审批和细节状态继续在 Image Agent 页面建设，Harness 不维护易漂移副本。

## 6. 数据模型

### 6.1 现有事实源

`MainTask`、`Stage`、`AgentInstance`、`TaskCard v1.1`、`Approval`、`Asset`、`Usage` 和事件继续作为运行事实源。现有 TaskCard 包含 `instance_id`，是单实例执行载荷，不直接承担多实例逻辑卡的 UI 语义。

### 6.2 新增 WorkItem

新增控制面 `WorkItem`，前端仍称“子任务卡”：

```json
{
  "schema_version": "1.0",
  "work_item_id": "work_kv_direction_a",
  "task_id": "task_launch_campaign",
  "stage_id": "stage_image",
  "title": "KV 方向 A",
  "agent_type": "image",
  "required": true,
  "depends_on": [],
  "current_instance_id": "instance_kv_a_01",
  "instance_ids": ["instance_kv_a_01"],
  "task_card_ids": ["card_kv_a_01"]
}
```

- WorkItem ID 在重试、重启和替换 Agent 时稳定。
- 每个执行实例仍拥有自己的 TaskCard；替换时追加实例和执行卡历史。
- 当前 P1 的进程重启和模型 attempt 可继续落在同一实例下，从 RetryBudget/运行记录投影到详情。
- WorkItem 状态不可直接写，由 Stage、Instance、Approval、Adapter 可用性和 attempt 计算。

### 6.3 新增对象

| 对象 | 关键字段 | 说明 |
| --- | --- | --- |
| `TaskIntake` | prompt、upload_session、asset_ids、status、revision | 首次创建与上传的可恢复状态 |
| `MasterThread` | task_id、latest_sequence、revision | 每个主任务唯一永久线程 |
| `MasterMessage` | role、kind、content、asset_refs、created_at | 用户、Master 和系统结果，追加式存储 |
| `PlanProposal` | revision、stages、work_items、execution_cards、status | 可多轮修订，确认后才提交执行 Plan |
| `TaskNavigationMetadata` | pinned_at、archived_at、display_order | 展示属性，不污染执行状态 |
| `WorkItemProjection` | business_status、raw_status、active_instance、attempts、alerts | 服务端生成的 UI 读模型 |

重命名更新 `MainTask.title` 并审计；置顶、归档写导航元数据。归档绝不能把运行任务改成虚假 `ARCHIVED` 状态。

## 7. API 设计

### 7.1 复用

- 复用任务列表/详情、执行计划提交、确认启动、实例 UI link、资源、审批、用量、事件和配置接口。
- `POST /api/v1/tasks` 继续服务 Master/API 集成，不能被 Web intake 破坏。

### 7.2 新增

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/v1/task-intakes` | 创建 DRAFT、空 input manifest 和上传会话 |
| `GET /api/v1/task-intakes/{taskId}` | 恢复首次创建状态 |
| `POST /api/v1/task-intakes/{taskId}/assets` | multipart 流式上传首期资料 |
| `DELETE /api/v1/task-intakes/{taskId}/assets/{assetId}` | 提交前移除附件 |
| `POST /api/v1/task-intakes/{taskId}/submit` | 锁定上传并创建首条消息 |
| `GET/POST /api/v1/tasks/{taskId}/master/messages` | 读取或追加永久线程消息 |
| `GET /api/v1/tasks/{taskId}/plan-proposals/latest` | 读取最新建议 |
| `POST /api/v1/tasks/{taskId}/plan-proposals/{revision}/confirm` | 校验 revision 后提交并按策略启动 |
| `GET /api/v1/tasks/{taskId}/work-items` | 当前主任务看板投影 |
| `GET /api/v1/tasks/{taskId}/work-items/{workItemId}` | attempt、实例、审批和交付摘要 |
| `PATCH /api/v1/tasks/{taskId}/presentation` | 重命名、置顶、归档/恢复 |

### 7.3 上传默认限制

- JPEG、PNG、WebP、PDF、纯文本、Markdown；校验扩展名、声明 MIME 和服务端探测 MIME。
- 单图片 20 MiB、单 PDF 50 MiB、单文本 5 MiB。
- 单任务最多 20 个文件、总计 200 MiB；前端最多 3 个并发上传。
- 描述每文件最多 4,000 字；后端流式落盘并重算 SHA-256。
- `submit` 后上传会话关闭，后续消息只能引用 `asset_id + manifest`。
- 现有 Base64 导入继续服务脚本；Web UI 使用 multipart，避免大 PDF 内存与体积放大。

### 7.4 MasterGateway

```text
submit_message(task_id, message_id, asset_refs) -> run_id
observe_run(run_id) -> RUNNING | NEEDS_INPUT | PLAN_READY | FAILED
load_plan(run_id) -> PlanProposal
cancel_run(run_id) -> result
```

- Gateway 不直接写计划或实例状态；PlanProposal 必须经 Schema 和领域命令校验。
- 同一 message/run 使用幂等键，服务重启后恢复观察，不能静默重复模型调用。
- Master 不可用时保持真实 DRAFT/PLANNING 语义并显示错误。
- 自动运行不能绕过预算、凭据、Adapter 可用性和人工专属审批。

## 8. 前端工程架构

当前 `frontend/src/main.ts` 同时负责路由、应用壳、数据加载、HTML 拼接和写操作，难以承载长期线程、上传队列和看板。本轮建议迁移为 React + TypeScript + Vite：

```text
frontend/src/
├── app/                 # App、Router、Providers
├── api/                 # 现有 Client、生成契约、queries
├── layout/              # AppShell、历史侧栏、抽屉
├── features/
│   ├── task-intake/
│   ├── master-thread/
│   ├── task-board/
│   ├── task-plan/
│   ├── agent-workbench/
│   ├── resources/
│   └── approvals/
├── components/
├── styles/
└── main.tsx
```

- React/React DOM 承载组件和受控表单。
- React Router 承载深链、嵌套路由和抽屉 query。
- TanStack Query 承载服务端状态、轮询、取消请求和 revision-aware cache。
- 不引入全局客户端状态库；草稿输入、侧栏和筛选使用局部 state/context。
- 首期不引入拖拽、富文本、动画库或大型组件库。
- 保留现有 API Client、生成契约和 CSS token，业务组件不得散落原始色值。

迁移采用路由级 strangler：先落 AppShell、新任务、Master、Board；再把资源、审批、用量、事件、设置迁入 React。任一阶段都保持可构建、可回归，不能维护两个可写事实源。

## 9. 刷新、并发与错误

- 运行中的 Board/WorkItem 每 3 秒轮询；稳定或等待人工时每 5 秒。
- 侧栏每 10 秒刷新，窗口重新聚焦时立即刷新。
- 页面隐藏时暂停高频轮询，恢复时重新校验。
- 写成功后按 task/work-item query key 精确失效。
- `REVISION_CONFLICT` 后重新读取并要求用户重新确认，不盲目重放。
- 列表响应增加 `projection_revision/ETag`，未变化时返回 304 或轻量响应。
- 未来 SSE/WebSocket 只通知 query 失效，领域事实仍从版本化 GET 读取。

## 10. 安全、可访问性与性能

- 文件、URL、标题和 Master 内容均视为不可信，输出必须转义。
- 资源预览走受控接口，不拼接宿主机路径。
- iframe URL 使用 Adapter allowlist、协议和实例归属校验，拒绝任意公网或跨任务 URL。
- Prompt、上传和计划确认均有可见标签、就地错误和完整异步反馈。
- 键盘可到达侧栏、卡片、抽屉及 iframe 前后导航出口，保留 Skip Link。
- 正文对比度至少 4.5:1；状态使用文字 + 图标 + 颜色。
- 独立图标按钮命中区至少 44×44px，并提供 `aria-label`。
- 长列表按列分页或虚拟化，缩略图懒加载并预留尺寸。
- 动效限于 150–200ms 并遵循 `prefers-reduced-motion`。
- 保证 1280/1440/1920px；小于 1180px 显示桌面宽度提示，本期不承诺移动交互。

## 11. 实施阶段

### F0：契约与应用壳

新增 WorkItem、TaskIntake、MasterMessage、PlanProposal、导航元数据契约；建立 React/Vite 壳、路由、Query Provider、设计 token 和可访问性基线。

**门禁：** 旧任务和已验收功能可用，现有构建、契约和真实栈 E2E 继续通过。

### F1：创建任务与首次上传

实现可恢复 DRAFT、multipart 上传队列、逐文件说明、提交锁、侧栏搜索/时间分组/置顶/重命名/归档。

**门禁：** 用户完全通过前端创建任务；刷新或单文件失败不丢失已提交事实。

### F2：Master 与计划确认

接入真实 MasterGateway、永久线程、PlanProposal revision 和确认/自动运行。

**门禁：** 从 Prompt 到保存计划和启动实例不依赖外部 API 操作，命令可审计、可恢复。

### F3：看板与计划视图

实现 WorkItem 投影、状态看板、终态筛选、Stage 依赖、详情抽屉和自适应轮询。

**门禁：** 并行运行、审批、失败、不可用和完成投影符合 RFC 聚合顺序。

### F4：内嵌 Image Agent

实现 iframe 容器、allowlist、CSP/frame 验证、错误和新标签回退。

**门禁：** Image WorkItem 打开对应实例工作台；PPT 明确不可用；跨任务 URL 被拒绝。

### F5：回归与发布

补齐组件、API、浏览器、真实栈、可访问性和恢复测试，更新文档与证据矩阵。

**门禁：** Linux/Windows CI、构建、契约、浏览器回归和真实 Image 闭环全部通过。

## 12. 验收标准

1. 根路径进入新任务页，左侧可搜索和切换历史主任务。
2. Prompt 为空、类型不支持、超限和 MIME 不匹配均就地报错。
3. 图片/PDF/TXT/MD 可首次多文件上传，逐文件显示说明、进度、重试和取消。
4. DRAFT 刷新可恢复；提交 intake 后追加上传入口关闭。
5. Master 澄清和计划版本持久化；人工模式确认前不创建运行实例。
6. 自动模式只跳过确认，不绕过预算、凭据、审批和 Adapter 可用性。
7. 置顶、重命名、归档/恢复不改变任务运行状态。
8. 看板只显示当前任务的逻辑 WorkItem；重试和替换不产生并列重复卡。
9. 看板映射可测试，并行运行优先于等待审批。
10. 计划视图正确展示 Stage、依赖、required/optional 和 PPT 边界。
11. 点击 Image WorkItem 在 Harness 内打开现有工作台；失败时显示原因并受控回退。
12. 子 Agent 页面没有 Harness 聊天输入；专业细节只在 Image Agent 工作台扩展。
13. 运行中页面 3 秒、稳定页面 5 秒内反映状态变化，隐藏页暂停高频轮询。
14. 写请求带幂等键、Actor 和 expected revision，冲突不覆盖新状态。
15. 1280、1440、1920px 无遮挡；键盘、焦点、错误宣告和对比度满足设计系统。

## 13. 可配置默认值

- 最佳宽度 1440px，最低保证 1280px；左栏 264px、抽屉 360px、看板列约 300px。
- 单任务最多 20 个文件/200 MiB，最多 3 个并发上传。
- Image/PDF/TXT/MD 以外类型前后端双重拒绝。
- 看板状态由系统计算，首期不支持手工排序和拖拽。
- React 按路由渐进迁移，不修改已冻结的 Agent 执行 TaskCard v1.1 语义。
