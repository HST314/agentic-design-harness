# 配置指南

正式运行时唯一推荐配置入口是 Web `/settings`。普通 YAML 只控制本机目录、端口和集成路径；真实 Provider 凭据不得写入 YAML、TaskCard、文档、日志或 Git。

## 启动配置与优先级

默认配置可直接启动离线控制面。需要调整非敏感启动项时，复制 `config/harness.example.yaml` 到不提交的本机文件，并让 `HARNESS_CONFIG` 指向它。环境变量会覆盖 YAML 中的同名字段。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HARNESS_HOST` | `127.0.0.1` | 后端监听地址 |
| `HARNESS_PORT` | `18080` | 后端监听端口 |
| `HARNESS_CONTROL_ROOT` | `control-data` | 事件、配置投影和受限凭据 |
| `HARNESS_WORKSPACE_ROOT` | `workspace` | 任务输入、私有输出与共享资产 |
| `HARNESS_IMAGE_AGENT_PATH_MODE` | `embedded_only` | Image Agent 路径模式；P6 后默认只接受内嵌源码 |
| `HARNESS_DELIVERY_BUNDLE_MIGRATION_MODE` | `bundle_only` | 交付数据写入模式；P6 后默认只写 Bundle |
| `HARNESS_MASTER_GATEWAY_URL` | 未配置 | Master Gateway 的 HTTP(S) 服务根地址 |

配置文件与相对目录都从仓库根目录解析。日常开发优先使用 `scripts/dev.py`，它会显式选择内嵌 Image Agent 和项目虚拟环境。

P6 已移除“内嵌目录缺失时自动查找相邻旧仓库”的隐式回退。紧急路径回滚必须同时显式设置 `HARNESS_IMAGE_AGENT_PATH_MODE=external_only` 和一个通过 release lock 校验的 `HARNESS_IMAGE_AGENT_ROOT`；只设置模式会失败关闭。交付写入可在受控回滚窗口显式设为 `legacy_only`，但既有 Bundle 候选、AssetManifest 与 BundleManifest 必须保持只读可见。

## Ark Key Pair

1. 打开 <http://127.0.0.1:18180/settings>。
2. 在“Provider 凭据”中填写凭据对 ID、Key ID、Ark Base URL、修订号和 API Key。
3. 保存后确认页面只显示 Key ID、Key 尾号、Base URL 主机提示和 revision；明文 Key 输入会立即清空。

[完整 Ark Key Pair 示例](../config/examples/ark-credential-pair.json)是字段与测试的共同样例。使用时必须替换 `api_key` 占位值，且不要把替换后的文件写回仓库。Key 与 Base URL 是不可拆分的一对；修改同一凭据必须递增 `revision`，同一 `(credential_pair_id, revision)` 的内容不可变。

保存凭据会替换当前启用池。需要保留多个活动凭据时，应在一次受控写入中提交完整集合；运行中的实例继续固定到创建时分配的凭据 revision，除非人工执行显式重分配。

## 六状态模型路由

把 Ark 控制台中的推理、文生图和视觉模型 endpoint ID 填入六个固定状态。Provider 必须全部为 `ark`，能力类型不可互换。

| 状态 | 含义 | 固定能力 |
| --- | --- | --- |
| `intake_clarify` | 需求澄清 | `reasoning_llm` |
| `confirmation_build` | 确认稿构建 | `reasoning_llm` |
| `initial_candidate_generation` | 首轮候选生成 | `text_to_image_model` |
| `self_check_inspection` | 自检审阅 | `vision_language_model` |
| `self_check_rework` | 自检返工 | `text_to_image_model` |
| `human_prompt_rework` | 人工反馈返工 | `text_to_image_model` |

[完整六状态路由示例](../config/examples/ark-image-model-routing.json)由文档门禁校验，避免文档与代码维护两份漂移 JSON。模型值是占位 endpoint ID，不声明某个公开模型名称长期可用；应使用当前 Ark 账户中已部署且与能力匹配的 endpoint。

保存路由使用全局配置 revision 做乐观并发。出现 `REVISION_CONFLICT` 时重新加载设置、核对变化后再保存，不要覆盖他人的新修订。

## Image Agent 运行策略

`/settings` 可修改提问策略、候选并发、默认输出尺寸、响应格式、水印与离线模式。真实 Ark 运行前必须关闭 `offline_mode`。运行中实例无法安全热应用的变化会标记 `restart_required`；使用受控重启后才清除该标记。

安全边界固定如下：

- `candidate_concurrency` 为 1–5；`max_render_retries` 固定为 0，失败不自动产生新的付费调用。
- `response_format` 只允许 `url` 或 `b64_json`。
- 模型参数拒绝任何疑似凭据字段和值；Base URL 只能存在于 Key Pair。
- Harness 只把所选凭据注入对应 Image Agent 子进程环境，不写入运行时配置快照或交付说明。

## 诊断与真实 smoke

先点击“运行配置预检（不生图）”。预检只验证当前配置 revision、六状态完整性、能力/Provider 一致性、启用凭据和运行策略，不向 Ark 发起图片生成。

只有预检为 `READY` 后，才可勾选费用确认并打开付费 smoke 二次确认。付费 smoke 固定生成一张最小诊断图片；失败不会自动重试，再次运行必须重新勾选并确认费用。成功响应只显示模型、数量和耗时，不返回图片 URL、请求正文、完整 Base URL 或 API Key。

隔离的发布验证可以从仓库外部提供 `.env`：

```text
HARNESS_REAL_PROVIDER_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
HARNESS_REAL_PROVIDER_API_KEY=replace-at-runtime
HARNESS_REAL_PROVIDER_TEXT_MODEL=replace-with-endpoint
HARNESS_REAL_PROVIDER_IMAGE_MODEL=replace-with-endpoint
HARNESS_REAL_PROVIDER_VLM_MODEL=replace-with-endpoint
```

先运行 `make real-provider-preflight REAL_PROVIDER_ENV_FILE=/secure/provider.env`，明确授权费用后再运行 `make real-provider-smoke REAL_PROVIDER_ENV_FILE=/secure/provider.env`。该 `.env` 只服务隔离 smoke，不会自动成为 Harness 日常凭据池；文件必须位于仓库外并受操作系统权限保护。

## 失败处理与轮换

- `CREDENTIAL_PAIR_INVALID`：检查 ID、revision、Base URL 和两个不同的环境变量名。
- `CREDENTIAL_PAIR_UNAVAILABLE`：选择已启用且 revision 匹配的 Ark 凭据。
- `VALIDATION_ERROR`：补齐六状态、修正 Provider 或能力类型后重新保存。
- `REVISION_CONFLICT`：重新读取 `/settings`，不要复用旧表单或旧幂等键。
- 真实 smoke 失败：先保存修订并重新预检；确认 Provider 配额、endpoint 状态和网络后再人工授权一次新调用。

疑似泄漏时立即在 Provider 侧吊销对应 Key，使用更高 revision 创建新 Key Pair，并按受控流程重分配或重建实例。不要通过编辑 `control-data/secrets/` 修复凭据。
