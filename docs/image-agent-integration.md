# Image Agent 集成

Image Agent 的源码与 Harness 位于同一工作树，但运行边界保持独立。正式路径是 `agents/image_agent_mvp` Git submodule，版本事实源是 `agents/image-agent.lock.json`；本地启动、CI 和 Adapter 都读取同一 lock，不允许追踪浮动分支或各自维护 revision 常量。

## 内嵌与进程边界

`scripts/dev.py setup` 完成以下工作：

1. 初始化固定 submodule 并核对 `.gitmodules`、gitlink 与 lock revision。
2. 校验 Image Agent 源码摘要、依赖清单摘要和隔离依赖内容摘要。
3. 为 Harness 创建 `.venv`，为 Image Agent 准备独立依赖目录。
4. 按 `frontend/package-lock.json` 准备前端依赖。

实例启动时，Harness 从锁定源码生成只读运行时副本，再以独立解释器、端口、工作目录和凭据环境启动子进程。Harness 不 import Image Agent 内部 Python 包；所有调用通过 loopback HTTP Adapter，所有文件通过任务受控目录和摘要验证传递。一个 Agent 的依赖冲突或崩溃不会污染控制面进程。

## 受管模式

TaskCard 由 Master 生成并在主系统审阅。Image Agent 受管页面不显示“新建工程”或 TaskCard 组装表单，而是直接打开当前项目状态；对普通创建端点的请求返回 `MANAGED_BY_HARNESS`。

受管创建只接受 Harness Adapter 发起的请求：

- 来源必须是 loopback；
- 请求必须携带当前运行时生成的单实例控制密钥；
- 控制密钥只存在于受限运行时文件和请求头，不进入浏览器、TaskCard、日志或交付物；
- 项目 ID、实例 ID 和 TaskCard revision 必须与主系统确认事实一致。

React 的 WorkItem 页面只请求 `GET /api/v1/instances/{instance_id}/ui-link`。服务端验证任务归属、当前实例、端口 allowlist、HTML 响应和 frame 策略；不合法 URL 返回稳定错误，不生成空白或任意 iframe。

## 分支级双资产交付

每个冻结分支生成稳定 `bundle_id`，并形成私有 `DeliveryBundleCandidate`：最终图片、`design-note.md`、branch、checkpoint、TaskCard revision 以及两份文件的 MIME、大小和 SHA-256。不同分支不会互相覆盖；同一分支内容变化会形成新候选。

Harness 收集候选时再次打开并复验两份文件。候选只出现在任务交付页，不进入 `resources/shared`。人工可：

- “确认图片与说明并入库”：以同一 `publication_batch_id` 发布图片、Markdown 和 BundleManifest；
- “退回修改”：保留候选、文件与决议，不公开资产；
- 对已发布请求做幂等重放：返回原批次，不复制资产。

发布采用 intent、暂存、全部验证和原子 commit。即使进程在 BundleManifest 准备后崩溃，恢复前也不会出现半公开资产；恢复完成后图片、Markdown 和清单一次性可见。

## 路径与数据迁移

Image Agent 路径模式：

| 模式 | 语义 |
| --- | --- |
| `prefer_embedded` | 默认优先内嵌路径；仅在一个迁移发布周期内兼容已存在的外部部署 |
| `embedded_only` | 只接受内嵌路径；本地启动器和 CI 使用此模式 |
| `external_only` | 仅供迁移期紧急回滚，不是新部署方式 |

任何模式都必须通过相同 release lock、revision 和内容摘要校验，路径回滚不能绕过供应链身份。

交付数据写入模式：

| 模式 | Legacy 写入 | Bundle 写入 | 使用时机 |
| --- | --- | --- | --- |
| `legacy_only` | 是 | 否 | 当前默认与切换前回滚点 |
| `dual_write` | 是 | 是 | 对账期 |
| `bundle_only` | 否 | 是 | 双平台真实验收后的目标 |

切换开关只改变新写入目标，不删除既有候选、AssetManifest 或 BundleManifest。降级版本不能识别的新事件时，应恢复升级前一致备份，而不是在原状态目录直接回滚代码。

## 升级 Image Agent

1. 在 Image Agent 仓库形成独立、可追溯提交并完成其全量测试。
2. 比较“旧锁定提交 → 目标上游提交 → 当前内嵌提交”，明确选择性同步；禁止自动跟随默认分支。
3. 更新主仓 submodule 指针以及 lock 中的 revision、包/契约版本、源码和依赖摘要。
4. 执行 `python scripts/verify_image_agent_lock.py`、Image Agent 全量测试、`make check` 和适用的真实进程 E2E。
5. 在同一变更说明中记录 Image Agent 提交与主仓提交，确保回滚点成对。

版本或内容不一致必须失败关闭。若升级失败，回到上一组已验收的 submodule 指针与 lock，并恢复对应状态备份；不得复制文件覆盖 submodule 或手工改摘要“放行”。

## 集成验证

```bash
python scripts/dev.py doctor
python scripts/verify_image_agent_lock.py
make g2-e2e
make g3-e2e
make check
```

G2 验证受管创建和真实进程边界；G3 验证分支候选、人工门禁与双资产发布。外部 Provider 测试需要单独授权，步骤见[配置指南](configuration.md)。
