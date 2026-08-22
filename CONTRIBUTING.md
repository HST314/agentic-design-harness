# 贡献指南

提交变更前先用 [QUICKSTART](QUICKSTART.md) 完成一键安装，并阅读与变更相关的现行文档。不要从历史提交中的 RFC 推断当前行为；代码、契约、测试和本目录中的现行文档才是事实来源。

## 开发环境

```bash
python3 scripts/dev.py setup
python3 scripts/dev.py doctor
```

Windows 将 `python3` 替换为 `py -3`。依赖由 `requirements-dev.txt`、Image Agent lock/依赖清单和 `frontend/package-lock.json` 固定；不要手工编辑安装目录或用未锁定的安装结果替代 lock 更新。

## 代码边界

- 后端依赖方向保持 `api → services/domain → storage/adapters`，`core` 只承载稳定横切能力。
- Harness 不 import 专业 Agent 内部模块；跨边界只能使用稳定契约、HTTP、隔离进程和受控文件。
- API 层只处理协议、认证边界、错误序列化和生命周期；领域不直接拼 HTTP 或任意文件路径。
- 所有持久化写入必须可恢复，带 Actor、revision 和幂等语义；禁止原地修改已发布资产或已确认修订。
- 前端使用 React Router、TanStack Query 和生成契约类型；后端仍是权限、revision、文件完整性和费用门禁的最终裁决者。
- UI 变更必须覆盖键盘、可见焦点、加载状态、`role=alert`/`aria-live`、错误恢复以及 light/dark 主题。

## 契约优先

修改公开对象时按以下顺序：

1. 更新 `contracts/v1/schemas`、catalog 或示例，并明确兼容性影响。
2. 补充正反例和跨对象不变量测试。
3. 运行 `python scripts/generate_frontend_contracts.py` 更新生成类型。
4. 更新生产者、消费者和文档。
5. 运行 `python scripts/generate_frontend_contracts.py --check` 确认无漂移。

契约版本采用精确支持列表；不要让消费者“猜测兼容”未知 minor。完整规则见[契约指南](docs/contracts.md)。

## Image Agent 双仓提交

Image Agent 功能先在 `agents/image_agent_mvp` 对应独立仓库形成提交并通过其测试，再在主仓更新 submodule 指针和 `agents/image-agent.lock.json` 的 revision、源码摘要及依赖摘要。

一次升级必须在 PR 中同时记录 Image Agent commit 与主仓 commit。禁止直接复制源码、让 submodule 跟随浮动分支、只改摘要或只提交主仓 gitlink。升级和回滚步骤见 [Image Agent 集成](docs/image-agent-integration.md)。

## 本地验证

日常完整检查：

```bash
make check
make typecheck
git diff --check
```

`make check` 覆盖 Python 测试、Ruff、compileall、密钥扫描、Agent import 边界、Image lock、文档/示例、前端契约、前端类型、单测和生产构建。发布候选再运行：

```bash
make verify
```

按变更范围追加：

| 范围 | 命令 |
| --- | --- |
| React 浏览器交互 | `npm --prefix frontend run test:e2e` |
| 受管 Image 真实进程 | `make g2-e2e` |
| 分支双资产交付 | `make g3-e2e` |
| 多实例、配置、凭据、预算 | `make g4-e2e` |
| 完整离线发布门禁 | `make g5-e2e` |
| 真实 Provider | 先预检，再按[配置指南](docs/configuration.md)显式授权费用 |

Windows 不要求 GNU Make；CI 会在 Windows/Linux 执行后端、前端和启动器矩阵。本机若只运行了部分命令或真实链路因环境跳过，必须在 PR 中如实说明，不能把 skip 描述为通过。

## 文档与安全

- README 只保留定位、能力、架构、成熟度和入口；安装放在 `QUICKSTART.md`，配置放在 `docs/configuration.md`。
- 新文档必须进入 `docs/README.md` 的受众导航，使用相对链接并从仓库根目录给出命令。
- JSON 不在文档和代码各维护一份；可执行示例放入 `config/examples` 或 `contracts/v1/examples` 并由测试读取。
- 不提交 API Key、Cookie、Authorization、完整敏感 URL、用户素材、本机状态、测试报告或人工生成的发布证据。
- `python scripts/check_docs.py` 校验文档集合、本地链接、JSON、命令入口与版本；CI 证据由 workflow 生成并上传 artifact。

## 提交与 PR

保持变更聚焦，提交标题使用简短祈使式描述，例如 `docs: consolidate operator guidance`。PR 至少写明问题、用户可见结果、代码/契约迁移影响、验证命令和未覆盖范围。提交前确认工作树只包含预期文件，submodule 状态可追溯，生成文件已同步且没有敏感信息。
