# Image Agent 集成

Image Agent 的源码与 Harness 位于同一工作树，但运行边界保持独立。正式路径是 `agents/image_agent_mvp` Git submodule，版本事实源是 `agents/image-agent.lock.json`；本地启动、CI 和 Adapter 都读取同一 lock，不允许追踪浮动分支或各自维护 revision 常量。

## 内嵌与进程边界

`scripts/dev.py setup` 完成以下工作：

1. 初始化固定 submodule 并核对 `.gitmodules`、gitlink 与 lock revision。
2. 校验 Image Agent 源码摘要和依赖清单摘要，并记录实际安装解释器与依赖内容身份。
3. 为 Harness 创建 `.venv`，用实际执行 pip 的解释器为 Image Agent 准备独立依赖目录。
4. 按 `frontend/package-lock.json` 准备前端依赖。

实例启动时，Harness 核对 Image 包名/版本、关键依赖版本、可导入性和导入路径，从锁定源码与本机合法依赖生成内容寻址的只读运行时副本，再以独立端口、工作目录和凭据环境启动子进程。Harness 控制面不直接调用 Image Agent 内部业务接口；所有运行调用通过 loopback HTTP Adapter，所有文件通过任务受控目录和摘要验证传递。Image 环境不可用时，控制面以 `degraded` 状态完成恢复，仅将 Image Adapter 标记为不可用。

## 受管模式

TaskCard 由 Master 生成并在主系统审阅。Image Agent 受管页面不显示“新建工程”或 TaskCard 组装表单，而是直接打开当前项目状态；对普通创建端点的请求返回 `MANAGED_BY_HARNESS`。

受管创建只接受 Harness Adapter 发起的请求：

- 来源必须是 loopback；
- 请求必须携带当前运行时生成的单实例控制密钥；
- 控制密钥只存在于受限运行时文件和请求头，不进入浏览器、TaskCard、日志或交付物；
- 项目 ID、实例 ID 和 TaskCard revision 必须与主系统确认事实一致。

React 的 WorkItem 页面只请求 `GET /api/v1/instances/{instance_id}/ui-link`。服务端验证任务归属、当前实例、端口 allowlist、HTML 响应和 frame 策略；不合法 URL 返回稳定错误，不生成空白或任意 iframe。

## 运行配置控制面

Harness 是实例运行配置的唯一控制面。任务创建时冻结无秘密的任务配置 revision；首个持久启动意图与系统默认 rebase 共用任务锁，意图一旦被接受便锁定任务基线。系统默认发布只 rebase 从未启动的任务，并为其未启动 Image 实例生成新 revision；实例覆盖原样保留。若模型已从当前批准列表移除，任务进入 `CONFIG_REVIEW_REQUIRED`，不得静默清除覆盖。

实例查询、预览与确认入口为：

- `GET /api/v1/instances/{instance_id}/runtime-settings`；
- `POST /api/v1/instances/{instance_id}/runtime-setting-proposals`；
- `POST /api/v1/instances/{instance_id}/runtime-setting-proposals/{proposal_id}/confirm`。

proposal 只接受白名单业务字段，携带实例 base revision、任务 expected revision 与幂等键。显式同步未启动 Image 工作项时，客户端必须回传预览得到的完整实例 ID 集合；确认会在任务锁内重新计算并逐一校验 base，集合变化返回 `SYNC_SCOPE_CHANGED`，不会部分同步。

未启动实例确认后直接切换本地不可变 revision，返回 `APPLIED_BEFORE_START`。已运行实例只在 Image Agent 报告“无活动 job、无未决事务、无未知模型调用结果、存在有效 checkpoint”时应用；否则返回 HTTP 202 和 `WAITING_SAFE_POINT`。安全应用使用 `CONFIRMED → MATERIALIZED → CHILD_BRANCH_CREATED → INSTANCE_POINTER_COMMITTED → APPLIED` saga，远端分支调用不持有任务锁，恢复时以同一幂等键重放。Harness 只在验证 branch/checkpoint/config hash receipt 后切换实例指针；任一摘要或归属不一致均失败关闭。

运行时 revision 位于实例 `runtime-config/revisions/`，`state.json` 只保存当前及待应用指针，远端应用 receipt 单独不可变保存。Provider Key 与 URL 不进入 revision、proposal、saga、事件或 API 响应；启动时才从进程配置解析到子进程环境。

## 分支级双资产交付

每个冻结分支生成稳定 `bundle_id`，并形成私有 `DeliveryBundleCandidate`：最终图片、`design-note.md`、branch、checkpoint、TaskCard revision 以及两份文件的 MIME、大小和 SHA-256。不同分支不会互相覆盖；同一分支内容变化会形成新候选。

Harness 收集候选时再次打开并复验两份文件。候选只出现在任务交付页，不进入 `resources/shared`。人工可：

- “确认图片与说明并入库”：以同一 `publication_batch_id` 发布图片、Markdown 和 BundleManifest；
- “退回修改”：保留候选、文件与决议，不公开资产；
- 对已发布请求做幂等重放：返回原批次，不复制资产。

发布采用 intent、暂存、全部验证和原子 commit。即使进程在 BundleManifest 准备后崩溃，恢复前也不会出现半公开资产；恢复完成后图片、Markdown 和清单一次性可见。

## 固定路径与交付

Image Agent 只从 `agents/image_agent_mvp` 启动，并且必须通过 release lock、revision 和内容摘要校验。不存在相邻目录搜索、外部运行模式或路径兼容开关。

新交付只形成 Bundle 候选并原子发布图片、Markdown 与 BundleManifest。不存在旧格式写入、双写或写入目标开关；历史任务、事件、资产与已经发布的交付仍可读取。目标代码无法读取现有状态时，应恢复升级前一致备份，而不是修改运行配置绕过边界。

## 升级 Image Agent

1. 在 Image Agent 仓库形成独立、可追溯提交并完成其全量测试。
2. 比较“旧锁定提交 → 目标上游提交 → 当前内嵌提交”，明确选择性同步；禁止自动跟随默认分支。
3. 更新主仓 submodule 指针以及 lock 中的 revision、包/契约版本、源码和依赖摘要。
4. 执行 `python scripts/verify_image_agent_lock.py`、Image Agent 全量测试、`make check` 和适用的真实进程 E2E。
5. 在同一变更说明中记录 Image Agent 提交与主仓提交，确保回滚点成对。

版本或内容不一致时，Image Adapter 必须失败关闭并给出 `setup --force` 与 `doctor` 诊断；控制面继续提供恢复、查询和其他可用能力。若升级失败，回到上一组已验收的 submodule 指针与 lock，并恢复对应状态备份；不得复制文件覆盖 submodule 或手工改摘要“放行”。

## 集成验证

```bash
python scripts/dev.py doctor
python scripts/verify_image_agent_lock.py
make g2-e2e
make g3-e2e
make p6-acceptance
make check
```

G2 验证受管创建和真实进程边界；G3 验证分支候选、人工门禁与双资产发布。P6 命令生成不含秘密的平台证据并明确记录 Ark 是否被凭据/费用确认阻塞；其中本地确定性 HTTP fixture 只证明真实 Image Agent 进程与契约，不得作为 Ark 验收。外部 Provider 测试需要单独授权，步骤见[配置指南](configuration.md)。
