# Master API 调用指南

控制平面默认监听 `http://127.0.0.1:18080`。OpenAPI 位于 `/openapi.json`，交互文档位于 `/docs`。所有业务写操作都必须通过 `/api/v1`，不能直接改 `control-data/` 或任务工作区。

## 命令信封

每个写请求携带 `envelope`：

```json
{
  "idempotency_key": "save_campaign_plan_01",
  "actor_type": "master",
  "actor_id": "master_default",
  "expected_revision": 1
}
```

- 同一个幂等键只可重放完全相同的命令；换载荷会返回 `IDEMPOTENCY_CONFLICT`。
- `expected_revision` 来自最近一次任务、审批或配置读取；过期时返回 `REVISION_CONFLICT`，调用方应重新读取并重新判断，不能盲目覆盖。
- 人工专属操作（凭据池、全局配置、归档、预算越权）不能由 Master 伪装执行。

## 推荐主流程

1. `POST /api/v1/tasks` 创建主任务。
2. `POST /api/v1/tasks/{task_id}/assets` 导入输入。`content_base64` 只用于当前请求，响应仅返回受控 manifest。
3. 以返回的 `asset_id + manifest_relpath` 生成任务卡。
4. `PUT /api/v1/tasks/{task_id}/plan` 在一个可恢复用例中保存计划、创建实例并分配完整凭据对。
5. 人工策略调用 `POST /api/v1/tasks/{task_id}/confirm-start`；自动策略也可调用该入口启动尚未启动的 `READY` 实例。
6. 轮询 `GET /api/v1/instances/{instance_id}`。只读轮询不会重复发送 Agent 命令。
7. 从 `GET /api/v1/inbox?owner=human` 或任务审批页处理冻结 Owner 的审批；通过 `POST /api/v1/approvals/{approval_id}/resolve` 提交一次决议。
8. 通过任务的 `files`、`usage` 与 `events` 端点验证交付、Token 和审计证据。

任务取消使用 `POST /api/v1/tasks/{task_id}/cancel`；它先停止可取消的子进程，再提交任务状态。实例级 `start`、`restart`、`cancel`、`archive` 都使用 `/api/v1/instances/{instance_id}/{operation}`，并要求命令信封。

## 稳定分页

任务、审批、收件箱、资源和事件列表使用相同参数：`limit`（1–200）、`cursor` 和 `order=asc|desc`。响应包含：

```json
{
  "items": [],
  "page": {
    "limit": 50,
    "order": "desc",
    "has_more": false,
    "next_cursor": null
  }
}
```

游标绑定端点、过滤条件和排序方向，不能跨列表复用。列表变化时采用键集语义，不依赖易漂移的数字 offset。

## 错误处理

预期错误统一返回 `contracts/v1/schemas/error-response.schema.json`。调用方按 `error.code` 判断，而不是解析自然语言。未知异常只返回 `INTERNAL_ERROR + trace_id`；响应不会包含 traceback、明文 Key 或内部快照。

常见处理：

- `REVISION_CONFLICT`：重新读取对象并重新生成命令。
- `ADAPTER_UNAVAILABLE`：保留 PPT 的 `UNAVAILABLE/BLOCKED_UNAVAILABLE`，不要伪造成功。
- `BUDGET_GATE_DENIED`：等待人工一次性预算审批。
- `ASSET_CORRUPTED`：停止引用该资产并检查磁盘/恢复记录。
- `PROCESS_START_FAILED`：查看实例事件与脱敏日志，再使用受控重启。

## 凭据与文件边界

API Key 不得进入任务卡、命令载荷、事件或前端状态。`GET /api/v1/key-pool` 和实例详情只返回 Key ID、尾号、Base URL 提示和 revision。资源下载使用服务返回的流；不要根据 manifest 拼接宿主机路径。
