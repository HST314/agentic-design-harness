# Master API

控制面默认位于 `http://127.0.0.1:18080`，业务 API 使用 `/api/v1`；OpenAPI 为 `/openapi.json`，交互文档为 `/docs`。不要直接修改 `control-data/`、任务工作区或 Image Agent 私有目录。

## Master 执行方式

- React Master 工作区通过控制面 API 提交用户消息；`MasterOrchestrator` 在后端进程内执行模型调用、素材检索并保存 PlanProposal。
- 受信编排方可直接调用 `/api/v1` 创建任务、提交计划和处理审批。

Master 使用任务创建时固定的无密钥配置快照。Provider API Key 仅在调用时从当前进程配置解析，不写入任务快照、消息、计划、日志或事件。未加载有效的三文件配置时，Master 会明确失败，不会伪造回复或计划。

## 进程内编排与素材工具

Master 通过 Ark/OpenAI-compatible Chat Completions 客户端请求结构化输出。文本模型可以调用以下受限工具；所有结果均携带素材、页码、区块或区域定位信息：

| 工具 | 语义 |
| --- | --- | --- |
| `list_assets` | 列出输入素材的类型、页数、解析状态和摘要 |
| `read_asset_blocks` | 按区块 ID 或连续页码读取带来源的文本块 |
| `search_asset` | 在解析文本中检索并返回来源定位 |
| `inspect_asset_region` | 让 VLM 检查图片或 PDF 页面中的归一化区域 |
| `get_asset_warnings` | 返回加密、损坏、页数超限、扫描质量或截断警告 |

TXT、Markdown 和数字型 PDF 由确定性解析器处理；图片与扫描型 PDF 按 `runtime.yaml` 的 `document_processing.visual_analysis` 策略使用 VLM。坏文件、加密 PDF 和页数超限均返回可审计业务警告，不会静默跳过。

每个主任务只有一个永久 Master 线程。控制面先持久化 `SUBMITTING`，进程内 run 由 `(task_id, message_id)` 确定并可安全重放；模型与 VLM 调用使用稳定的幂等键，并将 token、模型、Provider、请求 ID 和配置哈希写入用量审计。计划必须包含闭合、无环且 ID 唯一的 stages、work_items 和 execution_cards；每个活动 WorkItem 恰好映射一个当前实例与任务卡。

## 写操作信封

业务写请求携带 `envelope`：

```json
{
  "idempotency_key": "revise_card_campaign_02",
  "actor_type": "human",
  "actor_id": "human_operator",
  "expected_revision": 3
}
```

同一幂等键只可重放相同载荷；不同载荷返回 `IDEMPOTENCY_CONFLICT`。`expected_revision` 必须来自最新读取，过期返回 `REVISION_CONFLICT`。收到冲突后重新读取并重新审阅，不能盲目递增或覆盖。

## 推荐业务流程

1. `POST /api/v1/task-intakes` 创建首次输入会话，或由受信 Master 使用 `POST /api/v1/tasks` 创建已有 manifest 的任务。
2. `POST /api/v1/task-intakes/{task_id}/assets` 上传素材；服务端探测 MIME、限制大小并生成受控 manifest。
3. `POST /api/v1/task-intakes/{task_id}/submit` 锁定上传会话并启动永久 Master 线程。
4. `POST /api/v1/tasks/{task_id}/master/messages` 追加用户消息；轮询 messages 和 latest PlanProposal。
5. 人工在计划页逐卡审阅 `execution_cards`，必要时保存卡片修订。
6. 人工确认准确的 proposal、task 和所有 card revisions；Harness 才创建并启动实例。
7. 通过 approvals/inbox 处理 Image Agent 过程审批；通过 delivery-bundles 页面逐分支确认或退回双资产候选。
8. 通过 files、usage 和 events 验证共享资产、成本完整性和审计记录。

## TaskCard 修订与确认

修改卡片：

```text
PATCH /api/v1/tasks/{task_id}/plan-proposals/{proposal_revision}/task-cards/{card_id}
```

请求必须同时携带 `expected_proposal_revision`、`expected_card_revision`、完整可编辑字段和人类命令信封。服务端验证输入资产 manifest、输出 MIME/role、参数白名单和敏感字段；成功后创建新 card revision 和新 PlanProposal revision，旧提案变为只读 `SUPERSEDED`。

确认计划：

```text
POST /api/v1/tasks/{task_id}/plan-proposals/{proposal_revision}/confirm
```

请求携带 `task_expected_revision` 和每个 `card_id` 的 `expected_card_revisions`。task、proposal 或任一卡片变化都会拒绝启动并要求重新审阅。确认请求先持久化 Start Operation 并返回，后台执行器再恢复性执行“保存计划与创建实例 → 启动可运行实例 → 标记提案已确认”；调用方不应把 HTTP 请求存活时间当作启动事务。

查询与恢复启动：

```text
GET  /api/v1/tasks/{task_id}/start-operations/latest
GET  /api/v1/start-operations/{operation_id}
POST /api/v1/start-operations/{operation_id}/retry
```

前两个端点只读持久化进度。仅 `RETRYABLE_FAILED` 可重试；请求必须携带当前 `task_expected_revision`、新的幂等键和 human/master actor。重复相同请求返回同一 operation，不创建第二条启动链。

旧的 `PUT /api/v1/tasks/{task_id}/plan` 与 `POST /api/v1/tasks/{task_id}/confirm-start` 仍供受信 API 集成使用，但正式 Web 流程以 PlanProposal 确认为准。

## 审批与交付

`GET /api/v1/tasks/{task_id}/approvals` 和 `GET /api/v1/inbox?owner=human` 返回冻结 Owner 的待办。使用 `POST /api/v1/approvals/{approval_id}/resolve` 提交一次决议；Actor、approval revision 和幂等键必须匹配。

`GET /api/v1/tasks/{task_id}/delivery-bundles` 返回候选、已发布清单和对应 `DELIVERY_REVIEW`。预览端点只接受 `asset=image|design_note`，每次在同一文件描述符上复验摘要并返回私有、不缓存响应。批准交付会原子发布两份资产，拒绝只记录决议；重放返回原 publication batch。

## 分页、错误与安全

列表端点使用 `limit`、`cursor` 和 `order=asc|desc`。游标绑定端点、过滤条件与排序方向，不能跨列表复用；数据变化时采用键集语义，不使用数字 offset。

预期错误符合 `contracts/v1/schemas/error-response.schema.json`。调用方按 `error.code` 分支：

- `REVISION_CONFLICT`：重新读取并重新决策。
- `MASTER_RUN_FAILED`：检查任务配置快照、素材警告和 Provider 调用错误后，以相同消息幂等恢复。
- `MANAGED_BY_HARNESS`：不要绕过 Harness 直接向受管 Image Agent 创建任务。
- `ADAPTER_UNAVAILABLE`：保留 PPT 的不可用状态，不伪造完成。
- `ASSET_CORRUPTED`：停止引用并按 manifest/磁盘恢复流程处理。
- `BUDGET_GATE_DENIED`：等待人工一次性预算授权。

API Key 不得进入消息、TaskCard、命令、事件或文件 manifest。资源访问必须使用 API 返回的受控预览/下载地址，不得根据相对路径拼接宿主机绝对路径。
