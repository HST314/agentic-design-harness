# Agentic Design Harness

单机平面设计多智能体控制平面。系统由 Master Agent 与人工共同编排任务，
通过稳定契约管理彼此隔离的 Image、PPT 及后续专业 Agent。

当前 `main` 已完成 RFC v0.2 的 P1-00 至 P1-08.3、P1-11/P1-12 的 G2 纵向切片：
在 G1.5 底座上提供真实 Image Adapter、版本化任务/计划/实例 API、轮询状态投影
和控制面任务/实例页面。它仍不是完整 Phase 1 产品；审批推进、交付发布、用量、
动态配置、多实例与最终验收属于 G3 及后续工作包。

## 当前能力

- `docs/adr/`：技术栈、进程、错误、Image 映射和状态提交协议；
- `backend/harness/api`：应用工厂、生命周期、健康/就绪与契约校验入口；
- `backend/harness/storage`：原子 JSON/YAML、校验和 NDJSON、文件锁、八类
  Repository、幂等记录、恢复和索引重建；
- `backend/harness/domain`：统一命令信封、任务/输入/计划命令、冻结状态转换、
  人工/自动启动、取消、授权降级和确定性聚合；
- `backend/harness/services`：受控资产导入/发布、完整凭据对轮询、全局/实例
  配置、一实例一进程的监管与崩溃恢复，以及 durable 应用用例编排；
- `backend/harness/adapters`：typed Adapter Protocol、显式 Registry、真实 Image
  TaskCard 映射/隔离启动/HTTP 观测，以及 PPT 不可运行契约占位；
- `backend/harness/api`：任务创建、计划保存、启动确认、任务/实例读取与工作台深链；
- `frontend/`：任务、阶段、实例、进程与能力视图，含响应式布局和无障碍状态反馈；
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

可复制 `config/harness.example.yaml` 并通过 `HARNESS_CONFIG` 指定。运行状态写入
`control-data/`，任务工作区写入 `workspace/tasks/`，二者均不会进入版本控制。

## 边界

- Harness 不导入 Image/PPT Agent 的 Python 包，也不与其共享依赖环境；
- Master 和人工只能通过领域命令/API 改变控制平面状态；
- API Key 不进入任务卡、共享目录、事件、日志或响应；
- 当前前端只承载控制面，专业工作流通过 Adapter 提供的 HTTP 深链打开；
- G2 门禁使用固定 Image Agent 版本完成真实离线单实例启动、Job/Timeline/Snapshot
  轮询、等待状态映射与工作台打开；审批推进、交付发布和完整 Phase 1 验收仍在后续。

完整范围、状态语义与 18 条 Phase 1 验收标准见 [RFC v0.2](docs/rfc-v0.2.md)，
测试追踪见 [Phase 1 验收矩阵](docs/verification/phase1-traceability.md)。
