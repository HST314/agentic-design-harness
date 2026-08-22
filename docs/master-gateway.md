# Master Gateway 接入契约

工作台通过 `MasterGateway` 调用真实的编排服务。默认不配置网关；此时 Master 工作区会返回 `MASTER_UNAVAILABLE`，不会伪造回复或计划。

## 配置

```bash
export HARNESS_MASTER_GATEWAY_URL=http://127.0.0.1:19090
export HARNESS_MASTER_GATEWAY_TIMEOUT_SECONDS=10
```

`HARNESS_MASTER_GATEWAY_URL` 必须是 HTTP(S) 地址。Harness 会在该地址后调用以下接口：

- `POST /v1/runs`：提交一条用户消息，正文为 `{task_id, message}`，返回 `{run_id}`。网关必须按 `message.message_id` 幂等。
- `GET /v1/runs/{run_id}`：观察执行，返回 `status`（`running`、`needs_input`、`plan_ready` 或 `failed`），并可携带 `message`、`task_title` 或 `error`。
- `GET /v1/runs/{run_id}/plan`：在 `plan_ready` 后返回计划提案。
- `POST /v1/runs/{run_id}/cancel`：取消仍在执行的 run。

计划提案必须包含 `stages` 和 `work_items`。Harness 会校验任务归属、拓扑、ID 唯一性、依赖无环、计划修订号，以及每个新工作项恰好对应一个活动实例和一个活动看板卡；不合法的提案不会进入持久化状态。

## 恢复与确认

每个主任务只有一个永久 Master 线程。提交消息前会先持久化 `SUBMITTING` 状态；进程重启后会使用同一个消息 ID 重试，因此网关的幂等保证是必需的。已生成的计划按修订号保存，新修订会将旧提案标记为 `superseded`。

手动模式需要用户确认最新提案；自动模式仍会经过相同确认门禁，仅在所有新实例都能映射到已启用且凭据完整的 Provider 时继续。确认意图会先持久化，再按“保存计划与创建实例 → 确认并启动 → 标记提案已确认”的顺序恢复执行。
