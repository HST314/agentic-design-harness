# Master API

控制面默认位于 `http://127.0.0.1:18080`，业务 API 使用 `/api/v1`；OpenAPI 为 `/openapi.json`，交互文档为 `/docs`。不要直接修改 `control-data/`、任务工作区或 Image Agent 私有目录。

## 两种 Master 接入方式

- React Master 工作区通过 `MasterGateway` 提交用户消息、观察 run 并保存 PlanProposal。
- 受信编排方可直接调用 `/api/v1` 创建任务、提交计划和处理审批。

未配置 `HARNESS_MASTER_GATEWAY_URL` 时，工作区返回 `MASTER_UNAVAILABLE`，不会伪造回复或计划。网关地址必须是不含凭据、查询串或片段的 HTTP(S) 服务根地址。

## Master Gateway 契约

Harness 在网关根地址后调用：

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| `POST` | `/v1/runs` | 提交 `{task_id, message}`，按 `message.message_id` 幂等，返回 `{run_id}` |
| `GET` | `/v1/runs/{run_id}` | 返回 `running`、`needs_input`、`plan_ready` 或 `failed` |
| `GET` | `/v1/runs/{run_id}/plan` | `plan_ready` 后返回计划提案 |
| `POST` | `/v1/runs/{run_id}/cancel` | 取消仍在执行的 run |

每个主任务只有一个永久 Master 线程。Harness 在调用网关前持久化 `SUBMITTING`，重启后使用同一 message ID 恢复，因此网关必须实现请求幂等。计划必须包含闭合、无环且 ID 唯一的 stages、work_items 和 execution_cards；每个活动 WorkItem 恰好映射一个当前实例与任务卡。

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

请求携带 `task_expected_revision` 和每个 `card_id` 的 `expected_card_revisions`。task、proposal 或任一卡片变化都会拒绝启动并要求重新审阅。确认意图先持久化，再恢复性执行“保存计划与创建实例 → 启动可运行实例 → 标记提案已确认”。

旧的 `PUT /api/v1/tasks/{task_id}/plan` 与 `POST /api/v1/tasks/{task_id}/confirm-start` 仍供受信 API 集成使用，但正式 Web 流程以 PlanProposal 确认为准。

## 审批与交付

`GET /api/v1/tasks/{task_id}/approvals` 和 `GET /api/v1/inbox?owner=human` 返回冻结 Owner 的待办。使用 `POST /api/v1/approvals/{approval_id}/resolve` 提交一次决议；Actor、approval revision 和幂等键必须匹配。

`GET /api/v1/tasks/{task_id}/delivery-bundles` 返回候选、已发布清单和对应 `DELIVERY_REVIEW`。预览端点只接受 `asset=image|design_note`，每次在同一文件描述符上复验摘要并返回私有、不缓存响应。批准交付会原子发布两份资产，拒绝只记录决议；重放返回原 publication batch。

## 分页、错误与安全

列表端点使用 `limit`、`cursor` 和 `order=asc|desc`。游标绑定端点、过滤条件与排序方向，不能跨列表复用；数据变化时采用键集语义，不使用数字 offset。

预期错误符合 `contracts/v1/schemas/error-response.schema.json`。调用方按 `error.code` 分支：

- `REVISION_CONFLICT`：重新读取并重新决策。
- `MASTER_UNAVAILABLE`：配置真实网关，或明确使用受信直接 API 流程。
- `MANAGED_BY_HARNESS`：不要绕过 Harness 直接向受管 Image Agent 创建任务。
- `ADAPTER_UNAVAILABLE`：保留 PPT 的不可用状态，不伪造完成。
- `ASSET_CORRUPTED`：停止引用并按 manifest/磁盘恢复流程处理。
- `BUDGET_GATE_DENIED`：等待人工一次性预算授权。

API Key 不得进入消息、TaskCard、命令、事件或文件 manifest。资源访问必须使用 API 返回的受控预览/下载地址，不得根据相对路径拼接宿主机绝对路径。
