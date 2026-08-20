# 契约版本与兼容性规则

## 当前版本

- JSON Schema dialect：Draft 2020-12；
- 默认业务契约版本：`1.0`；TaskCard 消费者额外支持 `1.1`；
- `$id` 基址：`https://hst314.github.io/agentic-design-harness/contracts/v1/`。

每个跨边界根对象必须包含 `schema_version`。消费者必须先检查版本，再读取业务字段；版本按文档类型的支持清单精确分派，任何未明确列入支持清单的完整版本必须返回 `SCHEMA_VERSION_UNSUPPORTED`，不得静默降级。

TaskCard 1.1 是 P1 Image Adapter 的 consumer-first 兼容 minor：它只新增可选的 `parameters.usage_context`、`category_id` 与 `category_version`。TaskCard 1.0 Schema 保持冻结；Harness 同时读取 1.0/1.1，其他根对象仍只读取 1.0。

## 版本变化

契约版本使用 `MAJOR.MINOR`：

- MINOR：只允许新增可选字段、放宽不破坏既有有效数据的约束，或新增不改变状态机语义的枚举能力；
- MAJOR：删除/重命名字段、把可选字段变为必填、改变字段含义或单位、收紧既有数据约束、改变状态机语义；
- 文档和示例修正不改变契约版本，但必须在提交记录中说明；
- 同一主版本的 Schema 文件只在新 minor 目录发布，不覆盖已经交付的内容。

## 读写规则

- 生产者只发送其声明版本允许的字段；Schema 默认 `additionalProperties: false`；
- 持久化快照保留写入时的 `schema_version`；迁移生成新快照并留下审计事件；
- API 路径版本与业务 Schema 版本独立；`/api/v1` 不等同于 `schema_version: 1.0`；
- 标识符和时间戳是字符串；时间统一使用 RFC3339 UTC 的规范 `Z` 表示（例如 `2026-08-19T16:00:00Z`），不接受 `+00:00` 或其他偏移量；Token 和字节数是非负整数；
- 文件字段只能使用任务根目录内的 POSIX 相对路径，禁止绝对路径和 `..` 段；
- `Key + Base URL` 是不可拆分的凭据对，明文 Key 不属于任何公开契约。

## 接收与滚动升级策略

本契约采用**精确版本分派**，而不是假定同一主版本可以由旧 Schema 前向读取：

- 消费者只接收 `supported_schema_versions` 明确列出的完整版本；未知 minor 与未知 major 均返回 `SCHEMA_VERSION_UNSUPPORTED`；
- 每个 minor 保留独立、不可覆盖的 Schema。消费者按文档声明的版本选择对应 Schema，因此 `additionalProperties: false` 不会与新增可选字段冲突；
- 新消费者必须同时保留上一受支持 minor 的 Schema，并用原版本 Schema 验证旧快照；旧消费者不会读取新 minor；
- 滚动升级顺序固定为“先消费者、后生产者”：先部署同时支持旧/新 minor 的消费者，再允许生产者写新 minor；回滚生产者仍可继续发送旧 minor；
- 状态集合、合法转换或聚合优先级的变化属于状态机语义变化，必须升级 MAJOR，不得伪装成 minor。

## 演进流程

1. 先添加新 Schema 和正反例；
2. 更新状态/错误目录和跨对象语义测试；
3. 验证旧示例继续通过兼容版本；
4. 先升级消费者并确认其同时接受旧/新 minor，再升级生产者；
5. 删除兼容逻辑必须进入新的主版本。
