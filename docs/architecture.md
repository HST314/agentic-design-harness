# Phase 0 架构边界

## 系统定位

Harness 是单机控制平面，不是 Image Agent 与 PPT Agent 的代码合并层。它统一接收多模态材料、保存计划和任务卡、监管独立进程、维护共享资产、审批、通知、Token 用量及审计。

任务阶段按目标组合，支持：

1. Image-only；
2. PPT-only；
3. Image → PPT。

PPT 是可选的阶段类型，不固定为第二阶段。Phase 1 只形成 Image 运行闭环；PPT 契约可以保存和展示，但运行能力明确为不可用。

## 模块边界

- Task Service：主任务、计划、阶段依赖和聚合状态；
- Instance Service：实例、任务卡、配置和凭据对引用；
- Adapter Registry：不同专业 Agent 的运行边界；
- Process Supervisor：一实例一 OS 进程；
- Asset Service：受控导入、发布、摘要和 manifest；
- Approval / Inbox：固定 Owner 的审批及 FIFO 通知；
- Usage Service：标准化 Token 事件与聚合；
- File State Store：单写者、原子快照和追加事件。

## Phase 0 冻结结果

Phase 0 只固定跨模块数据形状、状态和错误语义，并提供三种计划组合的可执行契约测试。Stage/Instance 的 requirement 生命周期快照保留原始必需性、首次激活和授权降级事实；Instance 创建与激活均锚定 Task 快照时间窗，`UNAVAILABLE` 使用持久化激活事实区分已触发阻塞与未启动占位。TaskCard 参数按 Agent 类型封闭列举，敏感来源值携带统一标记并在公开序列化边界被拒绝，已知凭据格式作为纵深防线。它不包含文件存储实现、API、进程拉起、Adapter、资产复制或前端组件。

核心对象使用 JSON Schema Draft 2020-12，均采用封闭字段集合。扩展必须遵循版本规则，不允许生产者或消费者用未声明字段进行隐式协商。
