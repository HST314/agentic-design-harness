# Agentic Design Harness

单机平面设计多智能体控制平面。系统由 Master Agent 与人工共同编排任务，
通过稳定契约管理彼此隔离的 Image、PPT 及后续专业 Agent。

当前 `main` 已完成 RFC v0.2 的 P1-00 至 P1-07：开工决策冻结、可运行工程
骨架、File State Store、三种计划拓扑的领域命令，以及资产、凭据、配置和
单机进程监管服务。它还不是完整 Phase 1 产品；Adapter、审批/用量、版本化
业务 API 和统一前端属于后续工作包。

## 当前能力

- `docs/adr/`：技术栈、进程、错误、Image 映射和状态提交协议；
- `backend/harness/api`：应用工厂、生命周期、健康/就绪与契约校验入口；
- `backend/harness/storage`：原子 JSON/YAML、校验和 NDJSON、文件锁、八类
  Repository、幂等记录、恢复和索引重建；
- `backend/harness/domain`：统一命令信封、任务/输入/计划命令、冻结状态转换、
  人工/自动启动、取消、授权降级和确定性聚合；
- `backend/harness/services`：受控资产导入/发布、完整凭据对轮询、全局/实例
  配置以及一实例一进程的监管与崩溃恢复；
- `frontend/`：TypeScript/Vite 控制面壳、版本化 API Client、路由与响应式布局；
- `contracts/v1/`：Phase 0 冻结的跨模块事实源；
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

## 启动空服务

```bash
make serve
```

默认监听 `127.0.0.1:18080`：

- `GET /healthz`：进程存活；
- `GET /readyz`：契约注册表有效且唯一写者租约已获得；
- `POST /api/v1/contracts/{schema}/validate`：使用 `contracts/v1` 验证载荷。

可复制 `config/harness.example.yaml` 并通过 `HARNESS_CONFIG` 指定。运行状态写入
`control-data/`，任务工作区写入 `workspace/tasks/`，二者均不会进入版本控制。

## 边界

- Harness 不导入 Image/PPT Agent 的 Python 包，也不与其共享依赖环境；
- Master 和人工只能通过领域命令/API 改变控制平面状态；
- API Key 不进入任务卡、共享目录、事件、日志或响应；
- 当前前端只承载控制面，专业工作流通过 Adapter 提供的 HTTP 深链打开；
- P1-07 的离线进程夹具证明进程、端口和目录隔离；真实 Image Adapter 联调及
  完整 Phase 1 验收仍属于 P1-08 与后续工作包。

完整范围、状态语义与 18 条 Phase 1 验收标准见 [RFC v0.2](docs/rfc-v0.2.md)，
测试追踪见 [Phase 1 验收矩阵](docs/verification/phase1-traceability.md)。
