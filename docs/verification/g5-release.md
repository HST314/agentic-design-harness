# G5 产品与发布验收

G5 的唯一完整入口是：

```bash
make g5-e2e IMAGE_AGENT_ROOT=../image_agent_mvp
```

它依次执行全量 Python/契约/故障测试、Ruff、编译、secret scan、Agent import 边界、前端组件/API 单测、类型检查与构建、Pyright、Python/npm 依赖漏洞审计、G3 人工审批交付闭环、G4 三真实进程完整交付与进程丢失恢复、Playwright 产品与无障碍验收，以及生产构建连接真实 Image 进程的无浏览器 API Mock 闭环。最后同时生成 `build/phase1-evidence.json` 与 `build/workbench-f5-evidence.json`。

候选提交必须先提交且 worktree 保持干净。门禁把五阶段命令、退出码、起止时间、输出摘要和完整日志分别写入 `build/g5-gate-result.json` 与 `build/g5-gate.log`；证据生成器只接受当前 `HEAD` 上全部阶段退出码为 0 且日志摘要匹配的结果。单独执行 `make evidence` 不会制造验收结论。

## A–G 场景

| 场景 | 可执行证据 |
| --- | --- |
| A：人工确认、3 张 Image 卡、3 实例并行 | `tests/e2e/test_g4_multi_image_agent.py` |
| B：自动/受控启动、凭据 1→2→3→1、局部→全局配置 | 同上；`tests/integration/test_configuration.py` |
| C：审批 Owner 冻结、FIFO、深链和并行聚合 | `tests/integration/test_approvals.py`；`frontend/e2e/shell.spec.ts` |
| D：候选受控发布、摘要复验、asset_id + manifest | `tests/e2e/test_g3_real_image_agent.py`；`tests/integration/test_assets.py` |
| E：Token、未上报、并发预算、人工单次越权 | `tests/integration/test_usage_budget.py`；`tests/integration/test_g4_api.py` |
| F：进程丢失、独立取消、Harness 恢复且不重放 | `tests/e2e/test_g4_multi_image_agent.py`；`tests/crash/test_state_store_recovery.py` |
| G：PPT-only / Image→PPT 的 required / optional 分支 | `tests/integration/test_domain_commands.py`；`tests/test_contracts.py` |

18 条逐项声明、命令和证据文件在 `phase1-evidence-manifest.json`。生成器要求恰好包含 1–18，并将证据文件 SHA-256、当前 commit、真实门禁结果与日志 SHA-256 写入输出，避免“口头验收”或源码存在即视为通过。

RFC v0.3 工作台的 15 条逐项证据与双平台 CI/真实 Image 浏览器门禁见[工作台 F5 回归与发布](workbench-f5-release.md)。
