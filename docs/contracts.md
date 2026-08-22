# 契约指南

`contracts/v1` 是 Harness、前端、Master 和专业 Agent 之间公开数据形状的事实源。Schema 使用 JSON Schema Draft 2020-12；Catalog 固定版本、状态、错误码和敏感字段策略；`examples` 提供可执行正例。

## 当前版本

| 文档族 | 当前版本 | 精确支持版本 |
| --- | --- | --- |
| 默认业务对象 | `1.0` | `1.0` |
| TaskCard | `1.1` | `1.0`、`1.1` |
| TokenUsageEvent | `1.1` | `1.0`、`1.1` |
| DeliveryBundleCandidate | `1.0` | `1.0` |
| BundleManifest | `1.0` | `1.0` |

机器可读事实源为 `contracts/v1/catalogs/schema-versions.json`。API 路径版本 `/api/v1` 与对象的 `schema_version` 相互独立；每个跨边界根对象必须声明自己的版本。

## 目录与生成

```text
contracts/v1/schemas/    对象 JSON Schema
contracts/v1/catalogs/   版本、状态、错误码与凭据检测策略
contracts/v1/examples/   通过当前 Schema 与跨对象规则的业务示例
frontend/src/api/generated-contracts.ts  从 Schema 生成的前端类型
```

更新 Schema 后运行：

```bash
python scripts/generate_frontend_contracts.py
python scripts/generate_frontend_contracts.py --check
```

不要手工编辑生成的 TypeScript。`make check` 会验证生成结果没有漂移，单元/契约测试会编译全部 Schema、验证示例和状态目录，并检查跨对象不变量。

`config/examples` 保存非敏感运行配置样例，不属于业务 JSON Schema；`scripts/check_docs.py` 会对 Ark Key Pair、六状态模型路由和文档中的 JSON 代码块做语义/语法校验。

## 兼容策略

版本格式为 `MAJOR.MINOR`，并采用精确支持列表：

- MINOR 只允许新增可选字段或放宽不破坏既有有效数据的约束。
- 删除/重命名字段、可选变必填、改变含义或单位、收紧约束、改变状态机语义必须升级 MAJOR。
- 未列入 `supported` 的 minor 和 major 都返回 `SCHEMA_VERSION_UNSUPPORTED`，不得静默降级。
- 每个 minor 保留独立 Schema；旧快照继续使用写入时版本验证。
- 滚动升级固定“先消费者、后生产者”：先部署同时支持旧/新版本的消费者，再允许生产者写新版本。

TaskCard 1.1 在 1.0 基础上增加 Image 的 `usage_context`、`category_id` 和 `category_version` 可选参数；TokenUsageEvent 1.1 增加 Provider、调用类型、request ID、原始 usage 和非 Token 计费单位。不能把图片计费单位估算成文本 Token。

## 跨对象不变量

JSON Schema 负责单对象形状，服务与契约测试负责以下关系：

- task、stage、work item、instance、TaskCard 和资产引用必须属于同一 `task_id`。
- Stage/WorkItem 拓扑闭合、无环且位置稳定；每个活动 WorkItem 只有一个当前实例。
- TaskCard 只引用 `asset_id + manifest_relpath`，拒绝绝对路径、`..` 和未验证输入。
- Image TaskCard 参数采用正向白名单；未知参数、跨 Agent 参数和凭据形态失败关闭。
- PlanProposal 修订精确绑定 card/task revisions；旧提案被 supersede 后不可确认。
- AssetManifest 记录最终文件实测 MIME、大小、SHA-256 和来源，不信任扩展名或外部路径。
- DeliveryBundleCandidate 精确绑定 branch、checkpoint、TaskCard revision、图片与 Markdown 摘要；BundleManifest 只在同一 publication batch commit 后可见。
- API Key 明文与内部 `sensitive_value` 标记都不能进入公开契约。
- 时间戳使用 RFC3339 UTC `Z`；Token、字节和计费数量使用非负整数。

## 变更流程

1. 在新文件中添加 Schema/版本，不覆盖已交付版本。
2. 更新 `schema-versions.json`、相关状态/错误 catalog 和正反例。
3. 增加跨对象与旧版本回归测试。
4. 生成前端类型并更新所有消费者。
5. 先发布兼容消费者，确认读旧写旧与读新能力，再切换生产者。
6. 更新本指南与相关 API 文档；CI 生成验证报告 artifact。

状态枚举、合法转换或聚合优先级的改变属于状态机语义变化。凭据检测策略的调整必须同时包含真实秘密正例和业务 ID/摘要误报反例，不能只靠字符串熵或长度猜测秘密。
