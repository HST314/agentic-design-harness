# 配置指南

部署配置由仓库根目录的 `.env` 和 `config/` 下四个 YAML 文件组成。系统不兼容读取根目录旧 YAML，避免出现两个事实源。

## 配置事实源

| 文件 | 唯一职责 | 是否可含秘密 | Web 可编辑 |
| --- | --- | --- | --- |
| `.env` | API Key 等秘密变量，以及被 YAML 引用的部署值 | 是；不得提交 | 否 |
| `config/provider.yaml` | Provider 名称、Base URL 和 API Key 环境引用 | 否 | 否 |
| `config/model_list.yaml` | 文本、视觉理解、图像生成模型及能力 | 否 | 否 |
| `config/runtime.yaml` | Harness 服务、Master、文档处理、模型选择与 supervisor 策略 | 否 | 部分字段 |
| `config/image_agent_runtime.yaml` | Image Agent 提问、数据库、候选、出图、自检和高级模型默认值 | 否 | 是 |

YAML 中 `${NAME}` 必须由 `.env` 或启动进程环境提供。秘密不能写入 YAML、任务卡、日志、事件或交付说明。全局设置 API 不返回 Provider URL 或 Key，也不提供凭据写入能力。

## 全局设置发布

工作台侧栏的“全局设置”包含“Harness 设置”和“子 Agent 设置”两个标签。一次发布按以下顺序执行：

1. 使用当前全局 revision 预览并校验完整配置；
2. 展示字段级差异；
3. 在单写者锁内更新两个 runtime 文件并生成新的配置 revision；
4. 新任务立即继承，未启动任务和实例立即同步；
5. 运行中 Image Agent 在最近安全检查点自动建立配置分支；
6. 已完成实例和历史分支保持不变。

页面会显示已应用、等待安全点和失败数量。若 revision 已变化，使用“载入最新修订并保留未保存修改”合并当前表单后重新预览。监听地址、端口和 supervisor 属于进程边界，只读展示；手工修改后需要受控重启。

从任一历史阶段重建业务分支时，Image Agent 始终绑定当前最新配置。配置应用会比较预览时记录的活动工程 revision 和配置哈希，确保工程分支基线一致。

## Provider 与模型

`config/provider.yaml` 的 Provider ID 必须与 `config/model_list.yaml` 每个模型的 `provider` 一致。模型按能力分组：

- `text_models`：结构化推理与工具调用；
- `vlm_models`：图像输入与结构化输出；
- `image_models`：文生图和图生图。

`config/runtime.yaml` 的 `models` 只引用模型清单中存在且能力匹配的模型 ID。Image Agent 六个阶段默认继承这三类映射；`config/image_agent_runtime.yaml` 的高级覆盖仍必须引用能力正确的模型。

默认的品类约束库和风格方向库均为 `off`，即不使用数据库。可在子 Agent 设置中改为人工选择或自动选择。

## 运行与安全边界

- `server.host` 默认保持 `127.0.0.1`。系统没有 RBAC、SSO 或多租户隔离，不得直接监听公网。
- Image Agent 固定使用 `agents/image_agent_mvp` 和 `agents/image-agent.lock.json`，不存在外部目录回退模式。
- 交付只写 Bundle；不存在 legacy、dual-write 或按开关选择写入目标。
- 任务配置 revision 和实例配置 revision 都不含秘密；API Key 只在进程内解析并注入调用边界。
- 历史任务、事件、资产、交付和已完成配置保持可审计，不会因全局发布被改写。

## 本地检查

```bash
python3 scripts/dev.py config-check
```

检查包括文件存在性、YAML 结构、环境引用、Provider/模型关联、能力匹配、运行策略和端口范围。该命令不联网、不调用模型、不产生费用。部署完成必须同时满足：

1. `config-check` 退出码为 0；
2. `python3 scripts/dev.py start --check --timeout 60` 成功。

## 秘密轮换

在 Provider 侧创建新 Key，更新根目录 `.env`，重新执行配置检查，然后受控重启服务。确认新进程健康后吊销旧 Key。不要编辑 `control-data/`，也不要把旧凭据导入新配置。

## 显式开发验证

真实 Provider 验证不是产品设置功能。只有开发者明确选择、从仓库外部提供隔离环境文件并确认费用时，才运行：

```bash
make real-provider-preflight REAL_PROVIDER_ENV_FILE=/secure/provider.env
make real-provider-smoke REAL_PROVIDER_ENV_FILE=/secure/provider.env
```

预检不产生图片；smoke 是独立开发门禁，失败不会自动重试。证据不得包含 Key、完整请求/响应正文或临时图片 URL。
