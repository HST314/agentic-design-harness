# 快速开始

这是本仓库唯一的官方本地启动路径。`scripts/dev.py` 会初始化固定 Image Agent submodule、创建隔离 Python 环境、安装锁定依赖、同步前端依赖，然后共同启动后端和前端；无需手工激活虚拟环境，也无需在相邻目录另行摆放 Image Agent。

## 1. 准备环境

| 工具 | 要求 | 检查命令 |
| --- | --- | --- |
| Git | 支持 submodule 的当前稳定版本 | `git --version` |
| Python | 3.10 或更高 | Windows：`py -3 --version`；Linux：`python3 --version` |
| Node.js / npm | Node.js 22 或更高 | `node --version`、`npm --version` |

正式支持 Windows 10/11、Windows Server 2022 和 Linux。macOS 尚未进入 CI 支持矩阵。

## 2. 获取代码

```bash
git clone https://github.com/HST314/agentic-design-harness.git
cd agentic-design-harness
```

普通 clone 即可；启动器会补齐 submodule。后续命令都在包含 `scripts/`、`frontend/` 和 `pyproject.toml` 的仓库根目录运行。

## 3. 一条命令启动

Windows CMD 或 PowerShell：

```bat
py -3 scripts/dev.py
```

Linux：

```bash
python3 scripts/dev.py
```

首次运行会安装依赖，时间取决于网络和磁盘；之后只有锁摘要变化时才重新安装。启动器完成三个检查后报告就绪：后端 `/healthz`、后端 `/readyz` 和 Web 首页。

打开 <http://127.0.0.1:18180/>。常用地址：

| 地址 | 预期结果 |
| --- | --- |
| <http://127.0.0.1:18180/> | React 工作台 |
| <http://127.0.0.1:18080/healthz> | 后端进程存活 |
| <http://127.0.0.1:18080/readyz> | 后端可接受业务请求，状态为 `ready` |
| <http://127.0.0.1:18080/docs> | OpenAPI 交互文档 |
| <http://127.0.0.1:18080/> | `404 Not Found`，这是正常的后端根路由行为 |

按 `Ctrl+C` 会联动关闭两个服务。

## 4. 首次空控制面自检

启动后确认以下事实：

1. `/readyz` 返回 `ready`，页面不显示“服务不可达”。
2. 打开 `/tasks/new` 能看到新任务入口；未配置 Master Gateway 时，Master 会话明确显示不可用，而不是伪造计划。
3. 打开 `/settings` 能看到离线默认配置；未保存真实 Ark 凭据时，控制面仍可使用，但真实 Image 实例会被门禁。
4. `control-data/` 和 `workspace/` 只包含本机运行数据，均不应提交到 Git。

## 5. 分步诊断与双终端调试

启动器可拆分执行：

```bash
python3 scripts/dev.py setup
python3 scripts/dev.py doctor
python3 scripts/dev.py start
```

Windows 将每行开头的 `python3` 替换为 `py -3`。`setup --force` 可强制重建锁定依赖；`doctor --skip-ports` 只跳过端口占用检查，不跳过 submodule、摘要、解释器或依赖检查；`start --check --timeout 60` 在健康检查通过后立即退出，供 CI 使用。

需要分别观察服务日志时，先完成 `setup` 和 `doctor`，然后开启两个终端：

Linux：

```bash
.venv/bin/python -m harness
npm --prefix frontend run dev
```

Windows CMD 或 PowerShell：

```bat
.\.venv\Scripts\python.exe -m harness
npm --prefix frontend run dev
```

前端默认代理到 `http://127.0.0.1:18080`。自定义后端端口时，应使用启动器的 `--backend-port`；手动双终端方式还需在启动 Vite 的同一终端设置 `HARNESS_BACKEND_URL`。

## 6. 下一步

- 在 [配置指南](docs/configuration.md) 中配置 Ark Key Pair、六状态模型路由和显式付费 smoke。
- 在 [Master API](docs/master-api.md) 中了解任务创建、TaskCard 修订与确认启动。
- 在 [Image Agent 集成](docs/image-agent-integration.md) 中了解 submodule、受管模式、交付包和升级回滚。
- 任何启动失败先运行 `doctor`，再按 [故障排查](docs/troubleshooting.md) 中的错误签名处理。
