# 运行手册

本文面向单机运维和发布人员。首次安装使用 [QUICKSTART](../QUICKSTART.md)；Ark 凭据与模型使用[配置指南](configuration.md)；稳定错误签名见[故障排查](troubleshooting.md)。

## 支持边界与启动

支持 Windows 与 Linux 的单进程、单写者、本地文件系统部署。Linux 使用文件锁、`fsync`、进程组和 Landlock 写入白名单；Windows 使用字节锁、刷新写入、原子替换、Job Object 和托管 Python 子进程写路径审计。两者都校验进程创建身份、防止 PID 复用，并在取消或 Harness 退出时终止完整 Agent 进程树。

```bash
python3 scripts/dev.py doctor
python3 scripts/dev.py start
```

默认只绑定 `127.0.0.1`。当前没有登录、RBAC 或多租户隔离；不得直接监听公网。确需跨主机访问时，应放在受信反向代理、身份认证和网络 ACL 后，并把这些控制视为部署方责任。

## 健康与生命周期

| 探针 | 含义 | 自动化使用 |
| --- | --- | --- |
| `/healthz` | 后端进程存活 | 只用于判断进程是否响应 |
| `/readyz` | 恢复已完成、持有唯一 writer lease、后台执行器存活；`ready` 表示 Image 可用，`degraded` 表示仅 Image 被禁用 | 前端、业务调用和流量切换的唯一就绪依据 |

后端根路径 `/` 返回 404 是正常行为。进程管理器应发送正常终止信号，等待 Harness 停止监管线程、回收 Agent 进程树并释放 writer lease；不要以强制结束作为日常关闭方式。

开发启动器会在创建后端与前端子进程之前核对 Image Agent revision、源码与依赖锁集合，并预热内容寻址的只读制品；因此 Windows 冷缓存复制不计入子进程健康检查窗口。后端仍会在 writer lease 和业务写入之前使用实际运行解释器复核包名/版本、可导入性、导入路径和缓存内容，并记录 `image_runtime_preparation_started/completed`、缓存命中及阶段耗时。Image 环境校验失败时记录 `IMAGE_RUNTIME_ATTESTATION_FAILED`，控制面继续恢复并以 `degraded` 就绪，仅禁用 Image Adapter；不得通过跳过 lock、改写摘要或直接从可变源码启动 Image 任务。

启动恢复还会扫描未完成的运行配置 saga。`WAITING_SAFE_POINT` 是正常等待状态：应先观察实例推进到无活动 job 的 checkpoint，控制面会在实例观察或下一次批准推进前重试。`FAILED`、`CONFIG_INTEGRITY_FAILED`、`INSTANCE_CONFIG_LOCKED` 需要人工核对 revision、Image 分支与 checkpoint，不能通过编辑 `state.json`、复制 YAML 或重复发送不同幂等键绕过。`GET /api/v1/runtime-settings/metrics` 提供 rebase 结果、同步范围变化、分支创建失败、摘要不一致、确认到应用延迟以及最早等待时间；对应配置事件也出现在任务公开审计流中，且不包含凭据。

计划确认只提交持久化 Start Operation，不在 Master 请求锁内等待进程就绪。后台执行器按 `QUEUED → RUNNING → COMMITTED` 推进；逐实例阶段可从 `PREPARING`、`PROCESS_STARTING`、`AGENT_STARTING` 到 `RUNNING`。重启会迁移旧 `PREPARED` 意图并从已持久化副作用边界恢复，不重复发送已接受的启动命令。

## 状态与文件布局

- `control-data/`：事件日志、快照、索引、审批、恢复意图和 usage cursor。
- `workspace/tasks/`：输入资产、实例私有工作目录、交付候选、共享资产与 manifests。
- `.runtime/`：可重建的 Image Agent 隔离依赖和只读运行时制品，不属于业务备份。
- `agents/image-agent.lock.json`：运行时代码与依赖身份；必须与备份记录的主仓提交配套。

禁止直接编辑上述业务状态。公开资产是否可见由 manifest 和 publication batch commit 决定，不能靠预置文件或伪造索引恢复。

## 备份与恢复

一致备份必须同时覆盖 `control-data/` 与 `workspace/tasks/`。

1. 停止新写入并正常关闭 Harness，确认 `/readyz` 不再可用。
2. 在同一备份 revision 中复制两个根目录，保留权限、符号链接语义和时间信息。
3. 记录主仓 commit、Image Agent lock 摘要以及三份根 YAML 的摘要；秘密另按安全流程备份。
4. 恢复到空目录，并使用匹配的代码、依赖和 Image lock 启动。
5. 启动会截断不完整 NDJSON 尾部、重建投影与索引、恢复 usage cursor、对账 publication intent、Start Operation 与运行配置 saga，并观察活动 Agent；只以原幂等键恢复已记录副作用。
6. 检查 `recovery-warnings.ndjson`、`readyz`、任务事件、实例 job ID 和 BundleManifest 可见性。

不要通过重新发送 start、advance、approval 或 delivery confirm 来“修复”恢复；这可能制造重复副作用。恢复 warning 需要先定位数据与代码身份，再决定恢复备份或使用专用幂等入口。

## 容量与磁盘

容量承诺只覆盖本地 SSD、一个控制面进程和参考机器 4 个独占 x86_64 vCPU / 16 GiB 内存。`config/single-machine-capacity-slo.json` 是阈值事实源：最多 1,000 个保留任务、每任务 25 个控制事件、100 个活动任务、30 个并发专业 Agent 实例、10 GiB 控制面元数据。

参考包络内的门槛：任务创建与更新 P95 各不超过 500 ms，任务索引读取 P95 不超过 250 ms，干净关闭后的冷恢复不超过 30 s，控制面元数据平均不超过 1 MiB/任务。任务资产受独立文件/任务大小限制，不计入存储基准。

CI 回归：

```bash
make capacity-benchmark
```

参考机器资格测试：

```bash
PYTHONPATH=backend:.test-deps python scripts/benchmark_storage_recovery.py --profile qualification --output build/capacity-qualification.json
```

资格测试使用空的本地 SSD 目录并关闭其他高 I/O 负载。超过支持包络或连续两次资格失败时，应先优化增量索引与事件查找；仍不满足再评估数据库、对象存储和持久队列，不得把单机 SLO 外推为多机承诺。

## 发布门禁与 CI 证据

最低发布候选检查：

```bash
make check
make typecheck
make verify
```

GitHub Actions 在 Windows/Linux 运行后端、前端、启动器和 P6 专项矩阵，在 Linux 对固定 Image runtime 执行真实进程闭环，并生成 SBOM、文档检查和 `p6-platform-<OS>` 证据。P6 证据覆盖默认切换、双分支双资产、崩溃恢复、幂等、Key 脱敏和真实 Image Agent 进程；本地确定性 Provider 只属于进程/契约证据，不计为 Ark 通过。报告、浏览器结果和发布证据作为 CI artifact 保存，不手工提交到 `docs/`。

真实 Ark 验收只在开发者明确选择时独立执行本地配置预检、显式费用确认和一次最小生成；日志与证据不能包含 Key、完整请求/响应正文或图片临时 URL。未提供凭据导致的 skip 不是通过。

## 升级、迁移与回滚

升级前完成一致备份、`make verify`、适用的 Image E2E，并确认 submodule/lock 成对。先在恢复副本运行新版本，验证 ready、TaskCard revision、受管实例、至少两个分支候选、双资产原子发布和幂等重放，再切换正式目录。

部署只有锁定内嵌 Image Agent 与 Bundle 写入路径，没有外部目录、旧交付或双写开关。代码回滚必须匹配状态格式、契约 major、主仓 commit 和 Image lock。若目标版本无法读取现有事件，禁止在原状态目录直接降级；应恢复升级前一致备份。Git 回滚不能替代数据回滚。

## 日志与安全

- 日志、事件、异常和 API 响应不得包含 Authorization、Cookie、API Key、Key/Base URL 完整组合或 Agent stdout/stderr 中的凭据。
- 根 `.env` 权限只授予运行账户；备份、工单和聊天中同样按秘密处理。
- 共享诊断材料前删除用户素材、Provider 返回内容、完整本机路径和临时下载 URL。
- 发布前运行 `python scripts/secret_scan.py .`；发现疑似泄漏时先吊销 Provider Key，再更新 `.env`、检查配置并受控重启。

## 已知限制

当前为文件存储、单写者、前端轮询的本地控制面；无数据库、对象存储、多机调度、高可用、RBAC、SSO 或外部通知。PPT Agent 尚未接入运行时，必需 PPT 节点应保持 `BLOCKED_UNAVAILABLE`，不能强制伪造完成。
