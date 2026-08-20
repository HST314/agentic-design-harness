# Contracts v1

`v1/schemas` 是跨模块数据形状的事实源，使用 JSON Schema Draft 2020-12：

- `main-task.schema.json`
- `stage.schema.json`
- `agent-instance.schema.json`
- `task-card.schema.json`
- `delivery.schema.json`
- `asset-manifest.schema.json`
- `approval-request.schema.json`
- `inbox-item.schema.json`
- `token-usage-event.schema.json`
- `task-plan.schema.json`（组合对象，用于计划提交和三种拓扑测试）
- `error-response.schema.json`

`v1/catalogs/status-codes.json` 固定主任务、阶段、实例、审批和交付状态及合法转换；`error-codes.json` 固定机器可处理错误码。状态目录与 Schema 枚举由测试保持同步，并与 `tests/golden/status-transitions-v1.0.json` 金表逐字段比对，任何新增跳转都必须显式升级契约。

## 关键不变量

- 所有对象属于同一个 `task_id`；
- 阶段 `position` 唯一且连续，依赖只能指向更早阶段；
- 实例、任务卡必须引用同一计划中的阶段；
- Image-only 只有 Image 阶段；PPT-only 只有 PPT 阶段；Image → PPT 的 PPT 明确依赖 Image；
- 主任务和阶段状态必须与依赖、必需实例状态按 RFC v0.2 的固定优先级一致；`PARTIAL` 只接受原必需项已激活且带授权降级记录的快照，初始 optional PPT 不构成 `PARTIAL`；
- Stage/Instance 必须保存 `requirement_lifecycle`：原始 `required`、首次激活时间，以及可空的授权降级主体、时间和生效计划修订；Instance 创建时间必须满足 `task.created_at <= instance.created_at <= task.updated_at`，首次激活必须落在 Task 创建/更新时间窗内且不能早于 Instance 创建，授权降级不能早于激活或晚于 Task 快照；
- `UNAVAILABLE` 是否已触发以持久化的 `first_activated_at` 事实为准，不从当前 Task 终态反推；`BLOCKED_UNAVAILABLE` 的阻塞项必须携带激活事实，仍等待前置依赖或人工确认启动前取消的占位可保持 `first_activated_at = null`，且不得据此提前聚合为阻塞；
- 任务卡只能引用 `asset_id + manifest_relpath`，不能传宿主机绝对路径；
- TaskCard 1.0 的公开参数采用 Agent 类型正向白名单：Image 仅允许 `aspect_ratio` / `variants`，PPT 仅允许 `slide_count` / `planned_asset_role`；TaskCard 1.1 为 Image 新增可选 `usage_context`、`category_id`、`category_version`，其他对象仍为 1.0；未知参数和跨 Agent 参数均拒绝；
- TaskCard 的 `objective` / `instructions` 在 Schema 层拒绝常见明文 Key 形态；组合契约按 `credential-detection-policy.json` 递归扫描整个 TaskCard（含字段名和嵌套值）。来自凭据对、环境变量、密钥存储或受信请求密钥字段的值必须携带不可直接 JSON 序列化的 `sensitive_value` 内部标记，公开序列化器遇到标记即拒绝；Provider 格式与显式凭据赋值规则作为纵深防线。策略不再仅凭字符分布或裸十六进制格式猜测秘密，因此 base36/hex Key 由敏感来源标记或显式凭据赋值上下文阻断，业务单号、32/64 位十六进制素材 ID、摘要文件名和普通长文件名不会被无上下文误拒；
- 已发布资产由最终文件实测 MIME、大小和 SHA-256，且记录发布来源；
- API Key 明文不会出现在任何公开契约中；
- 所有时间戳使用 RFC3339 的 UTC `Z` 表示，非 UTC 偏移量不被接受；
- `total_tokens = input_tokens + output_tokens`，缓存和推理 Token 是分类信息，不重复加入总数。

JSON Schema 负责单对象形状，`tests/test_contracts.py` 负责跨对象和拓扑不变量。
