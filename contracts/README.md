# Contracts v1

`v1/schemas` 是跨模块数据形状的事实源，使用 JSON Schema Draft 2020-12：

- `main-task.schema.json`
- `stage.schema.json`
- `agent-instance.schema.json`
- `task-card.schema.json`
- `delivery.schema.json`
- `asset-manifest.schema.json`
- `approval-request.schema.json`
- `token-usage-event.schema.json`
- `task-plan.schema.json`（组合对象，用于计划提交和三种拓扑测试）
- `error-response.schema.json`

`v1/catalogs/status-codes.json` 固定主任务、阶段、实例、审批和交付状态及合法转换；`error-codes.json` 固定机器可处理错误码。状态目录与 Schema 枚举由测试保持同步，并与 `tests/golden/status-transitions-v1.0.json` 金表逐字段比对，任何新增跳转都必须显式升级契约。

## 关键不变量

- 所有对象属于同一个 `task_id`；
- 阶段 `position` 唯一且连续，依赖只能指向更早阶段；
- 实例、任务卡必须引用同一计划中的阶段；
- Image-only 只有 Image 阶段；PPT-only 只有 PPT 阶段；Image → PPT 的 PPT 明确依赖 Image；
- 主任务和阶段状态必须与依赖、必需实例状态按 RFC v0.2 的固定优先级一致；
- 任务卡只能引用 `asset_id + manifest_relpath`，不能传宿主机绝对路径；
- TaskCard 的公开参数名禁用常见凭据相关词，常见明文凭据值也会被语义校验拒绝；凭据只能通过实例的 `credential_pair_ref` 引用；
- 已发布资产由最终文件实测 MIME、大小和 SHA-256，且记录发布来源；
- API Key 明文不会出现在任何公开契约中；
- 所有时间戳使用 RFC3339 的 UTC `Z` 表示，非 UTC 偏移量不被接受；
- `total_tokens = input_tokens + output_tokens`，缓存和推理 Token 是分类信息，不重复加入总数。

JSON Schema 负责单对象形状，`tests/test_contracts.py` 负责跨对象和拓扑不变量。
