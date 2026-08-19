# Agentic Design Harness

单机平面设计多智能体控制平面的总仓库。系统以 Master Agent 与人工共同编排任务，通过稳定契约接入彼此隔离的 Image、PPT 及后续专业 Agent。

当前仓库完成 RFC v0.2 的 Phase 0：总仓初始化与契约冻结。运行时编排、子进程监管、持久化服务和 Web 前端属于 Phase 1，不在本次交付范围内。

## Phase 0 交付

- `docs/`：RFC v0.2、架构边界、版本和兼容性规则；
- `contracts/v1/schemas/`：核心对象与任务计划 JSON Schema；
- `contracts/v1/examples/`：Image-only、PPT-only、Image → PPT 三种合法组合；
- `contracts/v1/catalogs/`：状态、转换、稳定错误码和公开 TaskCard 凭据检测策略目录；
- `backend/`、`frontend/`：为 Phase 1 保留的明确边界；
- `tests/`：Schema、目录和跨对象语义契约测试。

## 自测

需要 Python 3.10+ 及 pip。统一入口会把锁定的测试依赖安装到仓库内隔离目录，不依赖系统 `venv` 包：

```bash
make test
```

默认解释器为 `python3`，可用 `make test PYTHON=/path/to/python3` 覆盖；隔离依赖目录也可用 `TEST_DEPS=/path/to/test-deps` 覆盖。

测试不仅验证示例能通过 JSON Schema，还验证阶段依赖、实例归属、任务卡引用、三种计划拓扑白名单、阶段/任务聚合状态、状态转换金表、凭据边界、UTC、版本兼容性、相对路径安全和 Token 汇总不变量。

## 版本规则

当前契约版本为 `1.0`，使用 JSON Schema Draft 2020-12。所有跨模块契约根对象必须携带 `schema_version`。详见 [契约说明](contracts/README.md) 与 [版本规则](docs/contract-versioning.md)。
