# Phase 1 安装、恢复与发布运行手册

## 运行前置

本版本只支持 Linux/POSIX 单机运行，启动入口会检查 `fcntl`、`/proc/self/stat`、process group、`killpg` 和 `setsid`。不满足条件时直接失败，不降级为不安全的进程监管。

最低环境：Python 3.10+、Node.js 22+、npm，以及可执行的固定 Image Agent 源码与依赖目录。默认只绑定 `127.0.0.1:18080`；如需跨主机访问，应在受信反向代理后部署并另行配置网络访问控制，不能直接把控制面暴露到公网。

```bash
python3 -m pip install -r requirements-dev.txt
npm --prefix frontend ci
make verify
make g5-e2e IMAGE_AGENT_ROOT=../image_agent_mvp
```

复制 `config/harness.example.yaml`，通过 `HARNESS_CONFIG` 指向配置文件。凭据只能由受控 API 写入 `control-data/secrets/`，不得写入 YAML、环境样例、Git 或任务工作区。

## 启动与健康

```bash
make serve
curl --fail http://127.0.0.1:18080/healthz
curl --fail http://127.0.0.1:18080/readyz
```

`healthz` 只表示进程存活；只有 `readyz=ready` 才表示契约注册和唯一写者租约已就绪。生产运行器应在收到终止信号后允许 Harness 关闭监管线程并释放 writer lease。

## 备份与恢复

一致备份必须同时覆盖 `control-data/` 与 `workspace/tasks/`。

1. 停止新写入，正常退出 Harness；确认 `readyz` 已不可用。
2. 使用支持权限和符号链接语义的工具复制两个根目录到同一备份 revision。
3. 保存当前 Git commit、配置文件（不含密钥）和 Image runtime revision。
4. 恢复到空目录，保持所有者与 `0700/0600` 权限。
5. 使用完全相同的代码、依赖和 Image runtime revision 启动。
6. 启动恢复会截断不完整 NDJSON 尾部、重建投影/索引、恢复凭据游标和 usage 状态，并对活动实例做不重放对账。
7. 检查 `recovery-warnings.ndjson`、`readyz`、任务事件和 Agent job ID；不得以重新提交 start/advance 的方式“修复”恢复。

## 故障排查

- writer lease 冲突：确认没有第二个 Harness 进程使用相同 `control_root`。
- 端口冲突/健康超时：检查实例事件、固定端口范围和 Agent readiness；不要杀死未通过 PID 身份校验的进程。
- 代码 identity 不一致：恢复固定源码/依赖 artifact，或创建替代实例；不能让旧实例静默换代码。
- 资产损坏：校验 manifest、最终文件 SHA-256 和磁盘状态。伪造 manifest 或预置公共文件不能恢复可见性。
- Token 未上报：这是数据完整性状态，不是 0；检查 Adapter usage 源与 cursor。
- PPT 必需节点阻塞：一期的正确状态是 `BLOCKED_UNAVAILABLE`，除非授权修订计划，否则不能强制完成。

## 日志与泄漏控制

日志、异常、事件和 API 响应不得包含 Authorization、Cookie、API Key、Base URL/Key 完整组合或 Agent stdout/stderr 中的密钥。发布前执行 `make verify`；secret scan、稳定错误响应、脱敏凭据测试和依赖漏洞审计均为硬门禁。共享日志时仍需人工复核路径、用户材料和 Provider 返回内容。

## 升级与回滚

升级前完成一致备份和 `make g5-e2e`。先在恢复副本上运行新版本，验证 `readyz`、18 项证据索引与一个离线 Image 闭环，再切换正式目录。

回滚只能回到与备份格式、契约 major、Image artifact 相匹配的 commit。若新版本已经提交了旧版本无法识别的事件，不可直接在原目录降级；应恢复升级前备份。禁止用 Git 回滚替代状态目录回滚。

## 已知限制

- 单机、单写者、文件存储；没有数据库、对象存储或多机调度。
- 无多租户、RBAC、SSO、邮件/IM 通知；部署边界必须由受信网络保证。
- PPT Agent 一期不运行，只保留契约与诚实的不可用状态。
- 前端使用轮询，没有 WebSocket；专业 Image 工作流仍通过隔离工作台深链。
- “无 Harness 人为并发上限”不代表宿主机或 Provider 资源无限。
