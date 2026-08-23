# 配置指南

部署配置只来自仓库根目录的 `.env`、`provider.yaml`、`model_list.yaml` 和 `runtime.yaml`。没有 Web 配置入口、配置 API、动态凭据池、运行中双写或兼容读取；文件变化必须经过重新检查并重启进程才生效。

## 四个事实源

| 文件 | 唯一职责 | 是否可含秘密 |
| --- | --- | --- |
| `.env` | API Key 等秘密变量，以及被 YAML 引用的部署值 | 是；不得提交 |
| `provider.yaml` | Provider 名称、Base URL 和 API Key 环境引用 | 否 |
| `model_list.yaml` | 文本、视觉理解、图像生成模型及能力 | 否 |
| `runtime.yaml` | 服务监听、Master、文档处理、模型选择、Image Agent 与 supervisor 策略 | 否 |

YAML 中 `${NAME}` 必须由 `.env` 或启动进程环境提供。秘密不能写入 YAML、TaskCard、日志、事件或交付说明。

## Provider 与模型

`provider.yaml` 的 Provider ID 必须与 `model_list.yaml` 每个模型的 `provider` 一致。模型按能力分组：

- `text_models`：结构化推理与工具调用；
- `vlm_models`：图像输入与结构化输出；
- `image_models`：文生图和图生图。

`runtime.yaml` 的 `models` 只引用 `model_list.yaml` 中存在且能力匹配的模型 ID。Image Agent 的六个内部阶段默认由三类模型映射；仅在确有需要时使用 `advanced_model_overrides`，覆盖值仍必须指向能力正确的模型。

## 运行与安全边界

- `server.host` 默认应保持 `127.0.0.1`。系统没有 RBAC、SSO 或多租户隔离，不得直接监听公网。
- Image Agent 固定使用 `agents/image_agent_mvp` 和 `agents/image-agent.lock.json`，不存在外部目录回退模式。
- 交付只写 Bundle；不存在 legacy、dual-write 或按开关选择写入目标。
- 任务首次执行时固定无密钥配置快照；对应 API Key 只在进程内解析并注入子进程环境。
- 历史任务、事件、资产和交付仍可读取；旧控制面配置和秘密不会被读取、导入或转换。

## 本地检查

```bash
python3 scripts/dev.py config-check
```

检查包括文件存在性、YAML 结构、环境引用、Provider/模型关联、能力匹配、运行策略和端口范围。该命令不联网、不调用模型、不产生费用。部署完成必须同时满足：

1. `config-check` 退出码为 0；
2. `python3 scripts/dev.py start --check --timeout 60` 成功。

## 秘密轮换

在 Provider 侧创建新 Key，更新根 `.env`，重新执行配置检查，然后受控重启服务。确认新进程健康后吊销旧 Key。不要编辑 `control-data/`，也不要把旧控制面凭据文件导入新配置。

## 显式开发验证

真实 Provider 验证不是产品设置功能。只有开发者明确选择、从仓库外部提供隔离环境文件并确认费用时，才运行：

```bash
make real-provider-preflight REAL_PROVIDER_ENV_FILE=/secure/provider.env
make real-provider-smoke REAL_PROVIDER_ENV_FILE=/secure/provider.env
```

预检不产生图片；smoke 是独立开发门禁，失败不会自动重试。证据不得包含 Key、完整请求/响应正文或临时图片 URL。
