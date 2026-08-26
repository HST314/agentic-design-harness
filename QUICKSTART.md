# 快速开始

这是唯一的本地部署路径。部署完成的判定只有两项：根配置检查通过，并且服务健康启动。

## 1. 准备环境

| 工具 | 要求 | 检查命令 |
| --- | --- | --- |
| Git | 支持 submodule 的当前稳定版本 | `git --version` |
| Python | 3.10 或更高 | Windows：`py -3 --version`；Linux：`python3 --version` |
| Node.js / npm | Node.js 22 或更高 | `node --version`、`npm --version` |

支持 Windows 10/11、Windows Server 2022 和 Linux。macOS 尚未进入 CI 支持矩阵。

## 2. 获取代码并准备配置

```bash
git clone https://github.com/HST314/agentic-design-harness.git
cd agentic-design-harness
cp .env.example .env
```

Windows 可使用 `Copy-Item .env.example .env`。在 `.env` 中替换真实秘密，并按部署需要审阅 `config/provider.yaml`、`config/model_list.yaml`、`config/runtime.yaml` 和 `config/image_agent_runtime.yaml`。五个配置事实源的职责和字段见[配置指南](docs/configuration.md)。

## 3. 安装锁定依赖

Linux：

```bash
python3 scripts/dev.py setup
```

Windows：

```bat
py -3 scripts/dev.py setup
```

启动器会补齐固定 submodule，并分别准备 Harness、Image Agent 和前端依赖。

## 4. 配置检查

Linux：

```bash
python3 scripts/dev.py config-check
```

Windows：

```bat
py -3 scripts/dev.py config-check
```

检查只解析本地文件、解析环境引用并验证跨文件能力，不调用 Provider，也不产生费用。失败时修正第一条错误后重新运行；不要绕过检查启动。

## 5. 启动

Linux：

```bash
python3 scripts/dev.py start
```

Windows：

```bat
py -3 scripts/dev.py start
```

完成上述准备后，也可用组合命令再次执行完整检查并启动：

```bash
python3 scripts/dev.py
```

Windows：

```bat
py -3 scripts/dev.py
```

启动器先显示 `[prepare]` 并验签、预热 Image Runtime，再启动后端与前端；冷缓存构建不占用后续健康检查的 45 秒窗口。只有 `/healthz`、`/readyz` 和 Web 首页都通过后才报告就绪。打开 <http://127.0.0.1:18180/>；按 `Ctrl+C` 联动关闭服务。

## 6. 分步诊断

```bash
python3 scripts/dev.py doctor
python3 scripts/dev.py start --check --timeout 60
```

Windows 将每行开头的 `python3` 替换为 `py -3`。`setup --force` 可重建锁定依赖；`doctor --skip-ports` 只跳过端口检查；`start --check --timeout 60` 在健康检查通过后退出，适合自动化验证。

常用地址：

| 地址 | 预期结果 |
| --- | --- |
| <http://127.0.0.1:18180/> | React 工作台 |
| <http://127.0.0.1:18080/healthz> | 后端进程存活 |
| <http://127.0.0.1:18080/readyz> | 后端可接受业务请求；`ready` 表示 Image 可用，`degraded` 表示仅 Image 被禁用 |
| <http://127.0.0.1:18080/docs> | OpenAPI 文档 |

配置检查通过且服务健康启动后，控制面部署即完成；需要 Image 任务时还应确认 `/readyz` 为 `ready`。启动后可从侧栏“全局设置”预览并发布无秘密的运行默认值。接下来从[创建第一个任务](docs/user-guide.md)开始；`degraded` 或启动失败按[故障排查](docs/troubleshooting.md)处理。
