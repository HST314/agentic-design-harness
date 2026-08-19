# 契约版本与兼容性规则

## 当前版本

- JSON Schema dialect：Draft 2020-12；
- 当前业务契约版本：`1.0`；
- `$id` 基址：`https://hst314.github.io/agentic-design-harness/contracts/v1/`。

每个跨边界根对象必须包含 `schema_version`。消费者必须先检查版本，再读取业务字段；未知主版本必须返回 `SCHEMA_VERSION_UNSUPPORTED`，不得静默降级。

## 版本变化

契约版本使用 `MAJOR.MINOR`：

- MINOR：只允许新增可选字段、放宽不破坏既有有效数据的约束，或新增消费者可忽略的枚举能力；
- MAJOR：删除/重命名字段、把可选字段变为必填、改变字段含义或单位、收紧既有数据约束、改变状态机语义；
- 文档和示例修正不改变契约版本，但必须在提交记录中说明；
- 同一主版本的 Schema 文件只在新 minor 目录发布，不覆盖已经交付的内容。

## 读写规则

- 生产者只发送其声明版本允许的字段；Schema 默认 `additionalProperties: false`；
- 持久化快照保留写入时的 `schema_version`；迁移生成新快照并留下审计事件；
- API 路径版本与业务 Schema 版本独立；`/api/v1` 不等同于 `schema_version: 1.0`；
- 标识符和时间戳是字符串；时间统一 RFC3339 UTC；Token 和字节数是非负整数；
- 文件字段只能使用任务根目录内的 POSIX 相对路径，禁止绝对路径和 `..` 段；
- `Key + Base URL` 是不可拆分的凭据对，明文 Key 不属于任何公开契约。

## 演进流程

1. 先添加新 Schema 和正反例；
2. 更新状态/错误目录和跨对象语义测试；
3. 验证旧示例继续通过兼容版本；
4. 再升级生产者，最后升级消费者；
5. 删除兼容逻辑必须进入新的主版本。
