# 文档中心

## 使用与集成

- [安装与启动指南](getting-started.md)：首次安装、Windows/Linux 命令、双终端启动和配置。
- [常见问题排查](troubleshooting.md)：404、Python 环境、前端代理、端口和就绪状态。
- [Master API 调用指南](master-api-guide.md)：标准编排流程、命令信封、分页与错误处理。
- [Master Gateway 接入契约](master-gateway.md)：真实编排服务的 HTTP 契约、幂等与确认恢复语义。

## 运维与发布

- [运行手册](operations.md)：安全边界、备份恢复、升级回滚和真实 Agent 门禁。
- [单机容量 SLO](single-machine-capacity-slo.md)：支持边界、容量指标与基准方法。
- [G5 发布验收](verification/g5-release.md)：Phase 1 离线发布门禁。
- [Phase 1 验收矩阵](verification/phase1-traceability.md)：需求、测试与证据的对应关系。
- [工作台 F5 回归与发布](verification/workbench-f5-release.md)：RFC v0.3 的组件/API/浏览器/真实栈、双平台 CI 与证据矩阵。

## 架构与契约

- [契约版本规则](contract-versioning.md)：JSON Schema 的兼容性和升级纪律。
- [RFC v0.2](rfc-v0.2.md)：Phase 1 设计基线。
- [工作台框架设计 v0.3](rfc-v0.3-workbench-design.md)：主任务创建、Master 工作区、子任务看板、内嵌专业工作台与迁移计划。
- [契约目录说明](../contracts/README.md)：Schema、Catalog、示例和生成流程。
- [后端边界](../backend/README.md)：后端分层与依赖约束。
- [前端说明](../frontend/README.md)：Web 控制台职责和开发命令。

文档命令默认从仓库根目录执行；路径或终端有特殊要求时，文档会单独注明。
