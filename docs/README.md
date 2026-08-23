# 文档中心

这里仅导航现行文档。历史设计、阶段验收和发布证据由 Git 与 CI artifact 保留，不在仓库中维护第二套 archive。

| 读者 | 先读 | 用途 |
| --- | --- | --- |
| 首次使用者 | [QUICKSTART](../QUICKSTART.md) | 干净环境一键安装、启动和空控制面自检 |
| 架构评审者 | [配置与 Master 资料理解架构重构方案](configuration-architecture-refactor-v1.md) | 三份 YAML、内置 Master、资料解析与用户/管理员边界的目标设计 |
| 系统管理员 | [配置指南](configuration.md) | Ark 凭据、模型路由、运行策略与诊断 |
| Image Agent 维护者 | [Image Agent 集成](image-agent-integration.md) | 受管模式、进程边界、版本锁、双资产与回滚 |
| Master / API 集成者 | [Master API](master-api.md) | 任务、消息、TaskCard 修订、确认启动与错误处理 |
| 代码贡献者 | [贡献指南](../CONTRIBUTING.md) | 分层、测试、契约优先和双仓提交 |
| 单机运维人员 | [运行手册](operations.md) | 备份恢复、容量、发布门禁、升级回滚与日志安全 |
| 故障处理人员 | [故障排查](troubleshooting.md) | 按稳定错误或日志签名定位和恢复 |
| 契约维护者 | [契约指南](contracts.md) | Schema 版本、兼容策略、生成流程和示例 |

所有命令默认从仓库根目录执行。文档中的 JSON 示例由 `scripts/check_docs.py` 解析，配置示例由代码模型校验；本地链接、版本声明和启动命令也由 CI 同步检查。
