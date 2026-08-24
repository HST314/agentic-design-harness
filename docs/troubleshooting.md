# 故障排查

先回到仓库根目录并运行诊断：Windows 使用 `py -3 scripts/dev.py doctor`，Linux 使用 `python3 scripts/dev.py doctor`。保留第一条失败信息；后续错误通常只是它的连锁结果。

## 快速索引

| 错误或现象 | 常见原因 | 恢复动作 |
| --- | --- | --- |
| `Required command ... was not found` | Git、Python、Node 或 npm 未进入 PATH | 安装 [QUICKSTART](../QUICKSTART.md) 中的最低版本并重新打开终端 |
| `Image Agent lock ...` / `submodule ...` | submodule 未初始化、gitlink 或内容摘要漂移 | 运行 `scripts/dev.py setup`；有本地修改时先人工核对，不要覆盖 |
| `Harness Python environment is missing or stale` | `.venv` 缺失或锁摘要变化 | 运行 `scripts/dev.py setup`；必要时使用 `setup --force` |
| `Frontend node_modules is missing or stale` | `package-lock.json` 与安装摘要不一致 | 运行 `scripts/dev.py setup`，让启动器执行锁定 `npm ci` |
| `No module named harness` | 使用了错误解释器或未完成 setup | 使用仓库 `.venv` 解释器，见下节 |
| `address already in use` | 18080、18180 或 Image Agent 端口被占用 | 确认已知进程后正常停止，或给启动器传新端口 |
| 前端“服务不可达” / `ECONNREFUSED` | 后端未 ready 或代理端口错误 | 依次检查 `/healthz`、`/readyz` 和 Vite 代理目标 |
| 后端根地址返回 404 | `/` 没有页面路由 | 正常；打开 18180 Web 或 18080 `/docs` |
| `writer lease` | 两个 Harness 共用 `control_root` | 只保留一个进程，或使用互不重叠的数据根目录 |
| `MASTER_RUN_FAILED` | 配置快照、素材解析、模型结构化输出或 Provider 调用失败 | 检查事件中的稳定错误、素材 warning 和三文件配置后，使用同一消息幂等重试 |
| `CONFIG_ERROR` | 根配置缺失、环境引用未解析或模型能力不匹配 | 运行 `scripts/dev.py config-check`，修正第一条错误后重启 |
| `MANAGED_BY_HARNESS` | 直接向受管 Image Agent 创建工程 | 回到 Master/计划页创建并确认 TaskCard |
| `IMAGE_RUNTIME_ATTESTATION_FAILED` | Image 源码、依赖、lock 或只读缓存与发布身份不一致 | 停止切流；恢复匹配同一提交的锁定依赖并重新运行 setup，禁止手改摘要或绕过验签 |
| `CONTROL_PLANE_NOT_READY` | ready 前请求了实例启动，或启动后台执行器未运行 | 等待 `/readyz`；若持续失败，检查启动验签、恢复日志和进程管理器 |
| `REVISION_CONFLICT` | 页面或调用方持有旧 revision | 重新读取、重新审阅、使用新幂等键提交 |
| `ASSET_CORRUPTED` | MIME、大小、SHA 或文件身份变化 | 停止发布，检查磁盘和来源，恢复可信备份或重新生成候选 |

## Python 与依赖

Windows：

```bat
.\.venv\Scripts\python.exe -c "import sys, harness; print(sys.executable); print(harness.__file__)"
```

Linux：

```bash
.venv/bin/python -c "import sys, harness; print(sys.executable); print(harness.__file__)"
```

第一行必须指向当前仓库 `.venv`，第二行指向当前仓库 `backend/harness`。不要根据提示符是否出现 `(.venv)` 猜测解释器。`ensurepip` 或 pip 不可用时，安装操作系统的 Python venv/pip 组件后重新运行 setup；不要改用全局 site-packages 绕过锁定依赖。

## submodule 与 release lock

查看状态：

```bash
git submodule status agents/image_agent_mvp
python scripts/verify_image_agent_lock.py
```

前缀 `-` 表示未初始化，前缀 `+` 表示 checkout 与主仓 gitlink 不同。普通缺失由 `scripts/dev.py setup` 修复；若目录存在本地提交或修改，先按 [Image Agent 集成](image-agent-integration.md)核对双仓历史。不要执行会丢弃未知修改的强制清理，也不要手工编辑 lock 摘要。

## 端口、健康与前端代理

Windows：

```bat
netstat -ano | findstr :18080
netstat -ano | findstr :18180
```

Linux：

```bash
ss -ltnp | grep -E ':18080|:18180'
```

确认 PID 属于已知 Harness/Vite 后，在原终端按 `Ctrl+C` 正常停止。不要仅凭端口号强制终止未知进程。

诊断顺序：

1. <http://127.0.0.1:18080/healthz>
2. <http://127.0.0.1:18080/readyz>
3. <http://127.0.0.1:18180/>

health 成功但 ready 失败时，检查配置路径、契约目录、状态目录权限、Image lock 和 writer lease。前两项成功而页面失败时，再检查 Vite 日志与 `HARNESS_BACKEND_URL`。

## Master、TaskCard 与受管实例

- Master run 长时间 `SUBMITTING`：检查任务配置快照、素材 warning、Provider 错误与对应持久化 run；使用原 `message_id` 恢复，不要创建第二个永久线程。
- 计划确认冲突：重新读取最新 proposal、task 和所有 card revisions；旧提案已 `SUPERSEDED` 时不可启动。
- 计划确认后页面显示 `STARTING`：这是持久化异步启动的正常状态。可读取 `/api/v1/tasks/{task_id}/start-operations/latest` 或 `/api/v1/start-operations/{operation_id}` 查看逐实例进度，不要再次确认计划。
- 页面显示 `START_FAILED`：错误已写入实例和 Start Operation。先按稳定错误码修复根因，再使用“恢复启动”或 `POST /api/v1/start-operations/{operation_id}/retry`；重试必须携带当前 task revision 和新的幂等键，且只允许 `RETRYABLE_FAILED`。
- Master 同步提示锁等待：GET 是只读快照，不应等待启动准备。若仍出现锁超时，记录返回的准确锁名和 `waited_seconds`，检查是否有旧版本进程共用状态目录。
- Image Agent 页面没有新建表单：这是受管模式的正确行为。任务必须从主系统创建。
- UI link 被拒绝或 frame blocked：检查实例是否为当前 WorkItem、Agent 是否 ready，以及返回头 `X-Frame-Options`/CSP；不要把任意 URL 直接塞给 iframe。

## 交付候选与恢复

- 候选显示 `CORRUPTED`：预览或发布前复验失败。保留候选和审计记录，修复来源后生成新候选，不覆盖旧文件。
- 只有图片或只有 Markdown 可见：这不应发生。立即停止写入，保留 `application-intents` 和恢复 warning，使用相同代码/lock 重启恢复；仍不一致则恢复完整备份。
- 重复点击确认：幂等请求应返回同一 batch。若客户端使用了相同幂等键但不同决议，会返回冲突，应重新读取审批状态。
- 已退回候选仍存在：这是预期审计语义；退回不删除候选或私有文件。

## 根配置与 Provider

- 配置检查失败：确认 `.env`、`provider.yaml`、`model_list.yaml`、`runtime.yaml` 都位于仓库根目录，修正报告的第一条错误。
- 环境引用未解析：检查 `.env` 中变量名与 `${NAME}` 完全一致；不要把秘密直接写入 YAML。
- 模型能力不匹配：核对模型所属分组、Provider ID 和 `runtime.yaml` 的模型引用。
- Provider 调用失败：检查 endpoint、配额和网络；系统不会自动执行新的付费 smoke。需要外部真实验证时，由开发者按[配置指南](configuration.md)重新明确授权。

## 提交诊断材料

提供操作系统/终端、Python/Node 版本、失败的原始命令、doctor 第一条错误、health/ready 结果、相关稳定错误码和最小日志片段。提交前删除 API Key、Authorization、Cookie、完整 Provider URL、用户素材、临时图片 URL和本机敏感路径。
