# 贡献指南

感谢你改进 Agentic Design Harness。提交变更前，请先阅读项目的[当前边界](README.md#当前边界与路线)和相关设计文档，避免把未支持能力描述为已完成。

## 开发环境

按[安装与启动指南](docs/getting-started.md)创建 `.venv` 并安装 Python、前端依赖。开发依赖使用带哈希的 `requirements-dev.txt`，前端依赖使用锁定的 `frontend/package-lock.json`；不要手工修改生成的锁文件。

## 变更原则

- 一个提交或 PR 解决一个清晰问题，避免混入无关重构。
- 后端依赖方向保持 `API → application/domain → storage/adapters`。
- Harness 不导入专业 Agent 的内部包，跨边界只使用稳定契约、HTTP、进程和受控文件。
- 新增或修改公开结构时先更新 `contracts/v1`，再生成前端类型并补充兼容测试。
- 不提交 API Key、Cookie、Provider 凭据、用户素材、运行状态或本机配置。
- 文档必须区分“当前已支持”“需要额外配置”和“路线规划”，示例命令应从仓库根目录可执行。

## 本地验证

Linux 开发者可运行：

```bash
make check
```

完整发布前门禁为：

```bash
make verify
```

Windows 不要求 GNU Make。使用项目虚拟环境执行：

```bat
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m ruff check backend/harness scripts tests
.\.venv\Scripts\python.exe -m pyright backend/harness
.\.venv\Scripts\python.exe -m compileall -q backend/harness scripts tests
.\.venv\Scripts\python.exe scripts\secret_scan.py .
.\.venv\Scripts\python.exe scripts\check_agent_import_boundary.py backend\harness
.\.venv\Scripts\python.exe scripts\generate_frontend_contracts.py --check
npm --prefix frontend run check
npm --prefix frontend run build
```

真实 Agent、浏览器和 Provider 门禁需要额外环境，按[运行手册](docs/operations.md)选择 G2–G5 范围。不要把真实链路的跳过结果描述为通过。

## 提交说明

提交标题使用简短、祈使式描述，例如 `docs: clarify local startup`。PR 描述至少说明：

- 变更解决的问题；
- 用户可观察到的行为；
- 执行过的验证命令及结果；
- 未覆盖的真实 Agent 或外部 Provider 范围。

提交 PR 前确认工作树只包含预期文件、所有链接有效、生成文件已同步，并且没有敏感信息。
