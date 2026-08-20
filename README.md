# Agentic Design Harness

单机平面设计多智能体控制平面。系统由 Master Agent 与人工共同编排任务，
通过稳定契约管理彼此隔离的 Image、PPT 及后续专业 Agent。

当前 `main` 已完成 RFC v0.2 的 G3 人工介入与交付闭环：在 G2 真实 Image
单实例纵切上增加冻结 Owner 的审批、FIFO 收件箱、异步推进对账、最终交付发现、
受控发布和资源浏览。它仍不是完整 Phase 1 产品；Token 用量、运行时配置、
多实例预算并发与最终产品验收属于 G4 及后续工作包。

## 当前能力

- `docs/adr/`：技术栈、进程、错误、Image 映射和状态提交协议；
- `backend/harness/api`：应用工厂、生命周期、健康/就绪与契约校验入口；
- `backend/harness/storage`：原子 JSON/YAML、校验和 NDJSON、文件锁、八类
  Repository、幂等记录、恢复和索引重建；
- `backend/harness/domain`：统一命令信封、任务/输入/计划命令、冻结状态转换、
  人工/自动启动、取消、授权降级和确定性聚合；
- `backend/harness/services`：受控资产导入/发布、冻结审批与 FIFO 通知、完整凭据对
  轮询、全局/实例配置、一实例一进程的监管与崩溃恢复，以及 durable 应用用例编排；
- `backend/harness/adapters`：typed Adapter Protocol、显式 Registry、真实 Image
  TaskCard 映射/隔离启动/HTTP 观测，以及 PPT 不可运行契约占位；
- `backend/harness/api`：任务/实例、审批/收件箱、审批路由和安全资源读写接口；
- `frontend/`：任务、实例、审批收件箱和资源浏览，含响应式布局、键盘操作、
  防重复提交和无障碍状态反馈；
- `contracts/v1/`：Phase 0 冻结的跨模块事实源及 consumer-first TaskCard 1.1；
- `tests/`：契约、单元、集成和崩溃注入测试，其中原 P0 46 条保持全绿。

PPT-only 与 Image→PPT 可以被正确建模。必需 PPT 只有在其前置条件满足并被
激活后才持久化为 `BLOCKED_UNAVAILABLE`；从一开始就是 optional 的 PPT 可
跳过且不会伪造 `PARTIAL`。

## 本地验证

要求 Python 3.10+、Node.js 22+ 和 npm。Python 与 npm 依赖均由锁文件固定。

```bash
make test

cd frontend
npm ci
cd ..

make check
```

`make check` 依次执行运行时及 P0 契约测试、Ruff、compileall、secret scan、
Agent import 边界检查、前端类型检查与生产构建。浏览器层测试位于
`frontend/e2e/`，安装 Playwright Chromium 后运行：

```bash
cd frontend
npm run test:e2e
```

真实 Image Agent 的 G2 离线门禁需要相邻的固定版本源码，并在独立目标目录安装
其锁定依赖。它不调用外部模型服务：

```bash
make g2-e2e IMAGE_AGENT_ROOT=../image_agent_mvp

# G3 人工审批 → 幂等推进 → 受控发布 → 主任务完成门禁
make g3-e2e
```

## 启动空服务

```bash
make serve
```

默认监听 `127.0.0.1:18080`：

- `GET /healthz`：进程存活；
- `GET /readyz`：契约注册表有效且唯一写者租约已获得；
- `POST /api/v1/contracts/{schema}/validate`：使用 `contracts/v1` 验证载荷。
- `POST /api/v1/tasks`、`PUT /api/v1/tasks/{id}/plan`、
  `POST /api/v1/tasks/{id}/confirm-start`：G2 写用例；
- `GET /api/v1/tasks`、`GET /api/v1/tasks/{id}`、`GET /api/v1/instances/{id}`：
  控制面读取与 Image 状态刷新；
- `GET /api/v1/instances/{id}/ui-link`：返回进程监管生成的本地工作台深链。
- `GET /api/v1/inbox`、`GET /api/v1/approvals/{id}`、
  `POST /api/v1/approvals/{id}/resolve`：FIFO 通知与带 revision 的审批决议；
- `PUT /api/v1/instances/{id}/approval-mode`：只改变后续审批路由，既有 Owner 不迁移；
- `GET /api/v1/tasks/{id}/files` 及其 `preview` / `download`：只读取已提交且实时校验
  通过的输入和公共交付。

可复制 `config/harness.example.yaml` 并通过 `HARNESS_CONFIG` 指定。运行状态写入
`control-data/`，任务工作区写入 `workspace/tasks/`，二者均不会进入版本控制。

## 边界

- Harness 不导入 Image/PPT Agent 的 Python 包，也不与其共享依赖环境；
- Master 和人工只能通过领域命令/API 改变控制平面状态；
- API Key 不进入任务卡、共享目录、事件、日志或响应；
- 当前前端只承载控制面，专业工作流通过 Adapter 提供的 HTTP 深链打开；
- G2 门禁继续使用固定 Image Agent 版本验证真实离线启动和观测；G3 门禁覆盖人工
  审批只推进一次、交付摘要复验、Asset Service 可见性提交及必需资产齐备后完成。

完整范围、状态语义与 18 条 Phase 1 验收标准见 [RFC v0.2](docs/rfc-v0.2.md)，
测试追踪见 [Phase 1 验收矩阵](docs/verification/phase1-traceability.md)。
