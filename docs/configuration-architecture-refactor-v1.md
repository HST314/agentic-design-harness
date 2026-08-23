# 配置与 Master 资料理解架构重构方案 v1

> 状态：需求决策稿
> 适用范围：Harness、内嵌 Image Agent、Web 设计工作台
> 目标读者：开发者、部署管理员、后端与前端实现人员
> 破坏性变更：是；不迁移、不兼容旧配置体系

## 1. 结论先行

本次重构采用以下最终方向：

1. 仓库根目录的 `provider.yaml`、`runtime.yaml`、`model_list.yaml` 是唯一非敏感配置来源，`.env` 只保存秘密。
2. 删除“必须另行部署 MasterGateway 才能使用”的产品前提。Master 编排器内置在 Harness 中，直接通过 OpenAI-compatible API 调用 `runtime.yaml` 选定的文本模型。
3. Master 使用文本推理模型负责澄清、拆解、规划和生成结构化 PlanProposal；图片、扫描件和 PDF 视觉内容由资料理解工具链调用 VLM，解析结果再交给 Master。Gateway 与多模态转换没有因果关系。
4. `model_list.yaml` 按 `text_models`、`vlm_models`、`image_models` 三类维护模型池；P0 只实现 Ark，三类接口均按 OpenAI-compatible 契约调用，其他供应商以后再扩展。
5. `runtime.yaml` 只引用模型 ID。默认配置只有 Master、文本推理、视觉理解、图像生成四个选择，Image Agent 六状态映射放在高级覆盖中。
6. 配置缺失或引用错误时进程拒绝启动，终端逐项输出文件、字段、缺失环境变量和修复提示；不得自动切换 fake/offline 模型。
7. 旧控制面配置、凭据池、外部 Gateway 配置和双事实源全部删除，不提供迁移工具、兼容读取或长期双写。
8. 普通设计师界面不出现 Provider、API Key、YAML、模型 endpoint、六状态路由、离线模式、诊断或真实出图 smoke。部署管理员一次配置后，设计师只使用任务、素材、计划、审批和交付能力。
9. 管理员可选 Web 配置页只映射 `runtime.yaml`，必须有真实身份认证和管理员授权；在权限系统完成前不向产品界面开放。
10. 删除产品内“真实生成一张图”的测试功能。开发阶段使用单元测试、契约测试和 fake Provider；如确需真实供应商验收，应在产品外由开发者显式执行，不进入用户界面和正常部署流程。

## 2. Gateway、Master 与多模态到底是什么

### 2.1 当前 Gateway 的真实职责

当前 `MasterGateway` 是一个外部 HTTP 服务边界，不是文件解析器，也不是把文本模型变成多模态模型的转换器。Harness 目前只会调用外部服务的以下契约：

- `POST /v1/runs`：提交消息并取得 run ID；
- `GET /v1/runs/{run_id}`：轮询规划状态；
- `GET /v1/runs/{run_id}/plan`：读取计划；
- `POST /v1/runs/{run_id}/cancel`：取消运行。

这种 Gateway 在以下场景有价值：Master 是另一支团队维护的独立服务、需要独立扩缩容、使用不同语言或运行环境、或需要跨网络复用。但当前项目是同仓、同服务器、单一供应商优先的产品，额外 HTTP 跳转只增加了一个没有随仓实现、无法通过现有配置完成的部署前提。

因此，本方案删除外部部署依赖，但保留清晰的内部接口边界：

- `MasterOrchestrator`：编排澄清、资料工具、结构化计划和恢复；
- `TextModelClient`：调用文本推理模型；
- `VisionModelClient`：调用 VLM；
- `ImageModelClient`：调用图像生成模型；
- `DocumentProcessor`：确定性解析和视觉解析调度。

这些都是进程内 Python 接口，不要求管理员部署第四个服务，也不出现在普通用户界面。

### 2.2 Master 为什么仍然可以是 text model

“Master”是系统角色，不等同于某一种模型能力。它负责：

- 理解用户目标和约束；
- 判断还缺什么信息；
- 调用资料读取工具；
- 把工作拆成 Agent 可执行的 TaskCard；
- 输出符合 JSON Schema 的 PlanProposal；
- 根据用户反馈修订计划。

这些任务的主要输入可以是规范化后的文字和结构化工具结果，因此 P0 的 Master 选用 `text_models`。这并不意味着整个系统没有原生多模态：

- `vlm_models` 是原生图文输入模型，用于理解图片、页面渲染结果、图表和视觉版式；
- `image_models` 是图像生成模型；
- `text_models` 用于 Master 和 Image Agent 的文本推理状态；
- 确定性解析器负责读取 TXT/Markdown 和可提取文字的 PDF。

系统是“文本 Master + 多模态工具”的组合，而不是让一个模型包办所有文件类型。未来若要让 Master 本身直接接收图片，可新增能力标签和路由策略，但不属于 P0，也不能再次引入另一套配置来源。

### 2.3 当前仓库实际上如何处理资料

当前实现只完成了素材接入和安全存储，没有完成内容理解：

1. 上传入口接受 JPEG、PNG、WebP、PDF、TXT 和 Markdown；
2. 服务端校验扩展名、MIME、大小和文件摘要；
3. 原文件与 manifest 被写入任务工作区；
4. Master 消息只携带用户 Prompt 和 `asset_id + manifest_relpath`；
5. Harness 没有内置 PDF 文本提取、页面渲染、OCR、版面解析或 VLM 资料理解步骤；
6. 外部 MasterGateway 客户端也只传引用，不负责读取和解析字节。

因此当前的“上传资料”并不等于“Master 已经读懂资料”。外部 Gateway 即使存在，也还需要自行解决工作区访问、文件读取、解析和结果追溯问题；这些契约目前没有在仓库中闭合。

### 2.4 业界通常怎么做

生产系统通常使用混合资料理解流水线，而不是一律把原文件直接塞给文本模型：

1. 对数字原生文档先做确定性文本和结构提取；
2. 对扫描页、图片、图表和复杂版式使用 OCR、版面解析或 VLM；
3. 把结果统一成带页码、区域、来源摘要和置信度的内部结构；
4. Master 按需读取摘要和局部内容，避免每次把完整大文件重复送入模型；
5. 计划中的关键判断保留来源定位，便于用户核对。

即使供应商提供“直接传 PDF”的原生能力，底层也常采用文本提取与页面图像联合处理。Anthropic 官方 PDF 说明明确描述了“逐页转图 + 逐页提取文本 + 联合理解”；Google 的 Layout Parser 则将 OCR、版面结构、表格/图片描述和语义分块组合成文档理解管线。火山方舟也把文本、图片、视频的多模态输入组织为模型 message。由此可见，“专用解析/工具 + 多模态模型 + 推理模型”是常见工程方式，但是否拆成独立网络 Gateway 是部署选择，不是多模态的必要条件。

参考：

- [Anthropic PDF support](https://docs.claude.com/en/docs/build-with-claude/pdf-support)
- [Google Document AI Layout Parser](https://docs.cloud.google.com/document-ai/docs/layout-parse-chunk)
- [火山方舟大模型多模态理解处理器](https://www.volcengine.com/docs/6492/2165096?lang=zh)

## 3. 目标架构

```text
开发者 / 部署管理员
  ├─ .env                  # 秘密，不提交
  ├─ provider.yaml         # Provider 连接，只引用环境变量
  ├─ model_list.yaml       # 可用模型池
  └─ runtime.yaml          # 当前模型选择与全部运行控制
                │
                ▼
       ConfigLoader + ConfigValidator
       启动前一次性解析、交叉校验、生成不可变 ConfigSnapshot
                │
        ┌───────┴────────┐
        ▼                ▼
  MasterOrchestrator   AgentSupervisor
        │                │
        │                └─ 自动物化 Image Agent 配置
        │                   不再人工维护第二份事实源
        ▼
  AssetUnderstandingService
        ├─ 文本/PDF确定性解析
        ├─ 页面渲染与结构化
        └─ VLM 视觉理解
                │
                ▼
      OpenAICompatibleProviderAdapter
       ├─ text_models
       ├─ vlm_models
       └─ image_models

普通设计师
  └─ 任务输入 → 上传素材 → 与 Master 沟通 → 审阅计划 → 审批 → 获取交付
     看不到任何部署配置、密钥、endpoint 或测试入口
```

### 3.1 单一事实源

| 内容 | 唯一来源 | 谁可修改 | 普通设计师可见性 |
| --- | --- | --- | --- |
| API Key | `.env` | 部署管理员 | 不可见 |
| Provider URL | `provider.yaml` | 部署管理员 | 不可见 |
| 可选模型池 | `model_list.yaml` | 开发者/部署管理员 | 不可见 |
| 当前模型与运行参数 | `runtime.yaml` | 部署管理员；可选管理员页 | 不可见 |
| 单个任务使用的配置 | 创建任务时的 `ConfigSnapshot` | 系统生成 | 仅展示业务相关结果 |
| Image Agent 私有配置 | Harness 从快照自动物化 | 系统生成 | 不可见 |

禁止以下事实源继续存在：

- `control-data/config/global.yaml` 作为主配置；
- Web 凭据池和凭据 revision；
- `HARNESS_MASTER_GATEWAY_URL`；
- 人工维护的 `agents/image_agent_mvp/configs/model_config.yaml`；
- 环境变量对普通运行参数的覆盖；
- 新旧配置双写或回退读取。

## 4. 四个根目录文件

### 4.1 `.env`

```dotenv
# 只保存秘密。仓库只提交 .env.example，不提交本文件。
ARK_API_KEY=replace-with-real-secret
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
```

规则：

- `.env` 必须在 `.gitignore` 中；
- `.env.example` 只列变量名和无秘密示例；
- 日志、错误响应、配置查询 API 和前端永远不返回解析后的秘密；
- 进程启动时加载 `.env`，随后解析 YAML 中的 `${ENV_NAME}`；
- 不支持默认值语法、命令替换、嵌套变量或任意 Shell 展开。

### 4.2 `provider.yaml`

```yaml
schema_version: "1.0"

providers:
  ark:
    base_url: ${ARK_BASE_URL}
    api_key: ${ARK_API_KEY}
```

P0 契约：

- 只实现 `ark`；
- 每个 Provider 只有 `base_url` 与 `api_key`；
- 所有接口按 OpenAI-compatible 契约调用，不增加 `type` 或 `protocol` 字段；
- `base_url` 必须是无用户名、密码、查询串和 fragment 的 HTTP(S) 服务根地址；生产环境只允许 HTTPS；
- `api_key` 必须由环境变量完整替换，禁止 YAML 明文；
- 不允许前端修改或读取该文件内容。

文本/VLM 使用 OpenAI-compatible Chat/Responses 形态，图像生成使用 OpenAI-compatible Images 形态；具体 endpoint 由模型所属类别确定，不再给管理员增加协议选择。

### 4.3 `model_list.yaml`

```yaml
schema_version: "1.0"

text_models:
  - id: ark-text-primary
    label: 主文本推理模型
    provider: ark
    model: deepseek-v4-flash-ga-260731
    capabilities:
      - structured_output
      - tool_calling
    parameters: {}

vlm_models:
  - id: ark-vlm-primary
    label: 主视觉理解模型
    provider: ark
    model: doubao-seed-evolving
    capabilities:
      - image_input
      - structured_output
    parameters: {}

image_models:
  - id: ark-image-primary
    label: 主图像生成模型
    provider: ark
    model: doubao-seedream-5-0-260128
    capabilities:
      - text_to_image
      - image_to_image
    parameters: {}
```

字段契约：

| 字段 | 规则 |
| --- | --- |
| `id` | 全文件唯一、稳定，由 `runtime.yaml` 引用 |
| `label` | 管理员可读名称，不用于 Provider 请求 |
| `provider` | 必须引用 `provider.yaml.providers` 中的键；P0 只能为 `ark` |
| `model` | Ark 中真实可调用的 endpoint/model ID |
| `capabilities` | 枚举值；启动时校验其满足引用位置要求 |
| `parameters` | 非敏感 Provider 参数；拒绝 key、token、secret、URL 类字段 |

同一模型只登记一次，不因流程状态重复登记。流程状态引用模型 ID，不复制 endpoint ID。

### 4.4 `runtime.yaml`

```yaml
schema_version: "1.0"

server:
  host: 0.0.0.0
  port: 18080
  log_level: INFO

models:
  master: ark-text-primary
  text_reasoning: ark-text-primary
  vision_understanding: ark-vlm-primary
  image_generation: ark-image-primary

master:
  model_timeout_seconds: 180
  max_tool_rounds: 8
  max_clarification_questions: 3
  require_plan_confirmation: true

document_processing:
  max_files_per_task: 20
  max_total_bytes: 209715200
  max_pdf_pages: 100
  text_chunk_chars: 6000
  visual_analysis: auto
  require_source_citations: true

image_agent:
  question_preference: proactive
  candidate_concurrency: 5
  default_output_size: 2560x1440
  response_format: url
  watermark: false
  advanced_model_overrides:
    intake_clarify: null
    confirmation_build: null
    initial_candidate_generation: null
    self_check_inspection: null
    self_check_rework: null
    human_prompt_rework: null

supervisor:
  port_range_start: 18100
  port_range_end: 18199
  startup_timeout_seconds: 30
  shutdown_grace_seconds: 5
```

模型继承规则是固定契约：

| Image Agent 状态 | 默认来源 | 高级覆盖允许类别 |
| --- | --- | --- |
| `intake_clarify` | `models.text_reasoning` | `text_models` |
| `confirmation_build` | `models.text_reasoning` | `text_models` |
| `initial_candidate_generation` | `models.image_generation` | `image_models` |
| `self_check_inspection` | `models.vision_understanding` | `vlm_models` |
| `self_check_rework` | `models.image_generation` | `image_models` |
| `human_prompt_rework` | `models.image_generation` | `image_models` |

`null` 表示继承四个默认选择，不表示禁用。高级覆盖值必须引用对应类别中的模型 ID。

## 5. 配置加载、校验与生效

### 5.1 固定加载顺序

```text
.env
  → provider.yaml 环境变量替换
  → model_list.yaml 模型与 Provider 关联校验
  → runtime.yaml 模型引用与参数校验
  → 生成 ConfigSnapshot
  → 启动后端、前端与 AgentSupervisor
```

不存在旧 YAML、控制面事件或环境变量覆盖层。除 `.env` 的秘密替换外，文件内容就是最终值。

### 5.2 启动失败策略

任意必填项缺失时不启动 HTTP 服务，终端必须一次列完所有错误，而不是修完一个再暴露下一个。例如：

```text
CONFIG_ERROR: configuration is incomplete (3 errors)
- provider.yaml: providers.ark.api_key -> environment variable ARK_API_KEY is missing
- runtime.yaml: models.master -> unknown text model id "ark-master"
- runtime.yaml: models.image_generation -> model "ark-vlm-primary" belongs to vlm_models, expected image_models

Fix the listed values and run: py -3.13 scripts/dev.py config-check
```

要求：

- 错误只显示环境变量名，不显示秘密值；
- 输出文件名、字段路径、错误原因和一条可执行修复命令；
- 解析错误带行号和列号；
- 不创建半初始化的 `control-data`；
- 不自动回退 offline/fake；
- `config-check` 是纯本地、零费用校验，不调用真实模型。

### 5.3 管理员保存与任务快照

受保护的管理员页面保存 `runtime.yaml` 时：

1. 前端提交完整 runtime、当前 revision 和幂等键；
2. 后端在内存中完成 schema 与三文件交叉校验；
3. 后端通过同目录临时文件、`fsync` 和原子替换写回 `runtime.yaml`；
4. 成功后生成新的 `ConfigSnapshot` revision；
5. 新任务立即使用新 revision；
6. 已创建或运行中的任务继续使用创建时快照；
7. 只有 server/supervisor 等进程级参数变化时返回 `restart_required: true`，不得自动重启服务。

多实例部署时不得让多个副本直接竞争写同一文件。P0 的管理员写入只支持单控制面实例；多实例阶段应改为受控配置发布系统，但外部发布结果仍须物化为同样三份文件契约。

## 6. 内置 Master 调用链

```text
用户消息 / 首次任务提交
  → MasterThreadService 持久化 message（沿用幂等与恢复语义）
  → MasterOrchestrator 创建内部 run
  → 读取当前任务 ConfigSnapshot
  → AssetUnderstandingService 准备资料摘要
  → 文本 Master 按需调用 read_asset / inspect_region 工具
  → StructuredOutputValidator 校验 PlanProposal
  → 持久化提案并等待用户确认
```

### 6.1 删除的外部概念

- 删除 `HttpMasterGateway` 和 `UnavailableMasterGateway`；
- 删除 `HARNESS_MASTER_GATEWAY_URL` 与 timeout 配置；
- 删除 `/v1/runs` 外部服务契约文档；
- 删除“未配置真实 MasterGateway”的用户错误；
- 删除 Gateway 地址的 UI、环境变量和排障步骤。

### 6.2 保留的可靠性语义

- 每个任务一个永久 Master 线程；
- message ID 和模型调用 idempotency key；
- `SUBMITTING/RUNNING/NEEDS_INPUT/PLAN_READY/FAILED` 内部状态；
- 崩溃恢复时不重复提交已知结果的付费请求；
- PlanProposal JSON Schema 校验；
- 提案 revision、人工确认和 TaskCard revision；
- token/图像调用用量审计，但不记录密钥或完整敏感请求。

### 6.3 模型调用边界

`MasterOrchestrator` 不直接拼 HTTP。它只依赖统一模型客户端：

```python
class TextModelClient(Protocol):
    def complete_structured(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        response_schema: dict,
        idempotency_key: str,
    ) -> ModelResult: ...
```

Provider Adapter 根据 `ConfigSnapshot` 解析 `provider`、endpoint 和非敏感参数。P0 只注册 Ark/OpenAI-compatible 实现；测试注册 fake 实现，不能由产品运行配置选择 fake。

## 7. 资料理解流水线

### 7.1 统一产物

每个上传素材在原始 `AssetManifest` 之外生成 `AssetUnderstanding`：

```yaml
schema_version: "1.0"
asset_id: asset_brand_guide
source_sha256: "..."
status: READY
media_type: application/pdf
summary: 品牌手册规定主色、禁用色、Logo 安全区和横版海报比例。
blocks:
  - block_id: p3_b7
    page: 3
    kind: paragraph
    text: 主色为……
    bbox: [0.12, 0.18, 0.86, 0.31]
    extraction_method: embedded_text
    confidence: 1.0
  - block_id: p5_v2
    page: 5
    kind: image_description
    text: Logo 四周安全区示意……
    bbox: [0.08, 0.12, 0.91, 0.88]
    extraction_method: vlm
    confidence: 0.92
warnings: []
```

该产物与 `source_sha256 + parser_version + model_config_hash` 绑定，可缓存、复验和追溯。原文件变化或解析版本变化才重新处理。

### 7.2 MIME 路由

| 输入 | P0 处理方式 | 是否调用 VLM |
| --- | --- | --- |
| TXT/Markdown | UTF-8 解码、标题/段落分块 | 否 |
| 数字原生 PDF | 提取文字与页码；页面渲染用于图表/版式 | `auto`，仅视觉页/区域 |
| 扫描 PDF | 页面渲染；OCR/VLM 结构化识别 | 是 |
| JPEG/PNG/WebP | 读取尺寸与基础元数据；VLM 生成视觉描述 | 是 |

P0 不在任务入口宣称 DOCX/PPTX 已支持。未来增加支持时必须同时提供确定性解析、页面/幻灯片定位、测试样本和错误语义，不能只把扩展名加入白名单。

### 7.3 Master 可用工具

- `list_assets()`：返回文件名、类型、页数、摘要和解析状态；
- `read_asset_blocks(asset_id, block_ids | page_range)`：按需读取文字块；
- `search_asset(asset_id, query)`：在本任务资料内检索并返回来源定位；
- `inspect_asset_region(asset_id, page, bbox, question)`：仅对需要视觉判断的区域调用 VLM；
- `get_asset_warnings(asset_id)`：返回扫描质量、截断、密码保护或解析失败信息。

Master 的计划描述引用 `asset_id/page/block_id`。用户可从计划定位回原资料，不能只得到不可验证的“模型认为”。

### 7.4 为什么不把所有文件直接给 Master

- 文本模型不能原生读取图片；
- 不同供应商支持的文件类型和大小不同；
- 长 PDF 每轮重复上传会增加延迟、上下文和费用；
- 原生文件接口不一定保留稳定的页码、表格和区域引用；
- 先解析再按需读取更容易缓存、审计、脱敏和测试；
- VLM 只处理真正需要视觉理解的部分，可以控制成本和误差。

小图片未来可以由支持图像输入的 Master 模型直接读取，但仍必须经过 AssetService 安全校验并产生来源记录；这只是优化路径，不改变统一 `AssetUnderstanding` 契约。

## 8. Image Agent 配置物化

Harness 在创建 Agent 实例时，根据任务的 `ConfigSnapshot` 生成只读运行目录：

```text
workspace/tasks/<task_id>/instances/<instance_id>/runtime-config/
  ├─ runtime.yaml
  └─ model_config.yaml
```

生成规则：

- Image Agent `runtime.yaml` 来自根 `runtime.yaml.image_agent`；
- `model_config.yaml` 由四个默认模型和六状态高级覆盖展开；
- Provider URL 与 Key 只注入子进程环境，不写进实例文件；
- 文件带 `source_config_revision`、`config_hash` 和生成时间；
- 文件只读，Image Agent 不回写根配置；
- 不再读取内嵌 Agent 仓库自带的可变配置作为运行事实源。

## 9. 管理员与设计师界面边界

### 9.1 普通设计师界面

保留：

- 新建任务与自然语言需求；
- 上传和管理任务素材；
- 与 Master 对话、补充信息；
- 审阅和确认计划；
- 查看 Agent 进度、处理业务审批；
- 查看、比较和下载交付物。

删除：

- 侧栏“设置”入口；
- Provider、Base URL、API Key、Key ID 和 revision；
- endpoint/model ID 与六状态路由；
- offline/fake 模式；
- 配置预检、付费 smoke 和“生成一张诊断图”；
- YAML、环境变量、运行端口和 Supervisor 参数；
- 任何“请配置 MasterGateway”的提示。

设计师面对的错误应是业务错误，例如“该素材无法读取，请重新上传未加密 PDF”，而不是部署参数错误。完整配置错误只进入服务端日志和管理员告警。

### 9.2 管理员界面

管理员界面不是普通产品设置页，建议独立为 `/admin/runtime`，并满足：

- 只允许 `admin` 角色访问，后端逐请求鉴权；
- 不出现在设计师导航和搜索中；
- 只读展示当前 Provider 是否已配置，不返回 URL 与 Key；
- 只编辑 `runtime.yaml` 对应字段；
- 默认仅显示四个模型选择和常用控制参数；
- “高级设置”才展开 Image Agent 六状态覆盖；
- 所有字段有可见 label，校验错误紧邻字段并汇总到页面标题；
- 保存使用 revision、防重复提交、明确成功/失败反馈；
- 不提供真实模型调用或图片生成测试按钮。

若当前系统尚无登录、角色和会话安全，则不实现或不启用 `/admin/runtime`；管理员直接编辑三份 YAML 并运行 `config-check`。不能用隐藏 URL 代替权限控制。

### 9.3 管理 API

仅在管理员权限可用后提供：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/v1/admin/runtime-config` | 读取脱敏 runtime 与 revision |
| `PUT` | `/api/v1/admin/runtime-config` | 校验并原子保存完整 runtime |
| `GET` | `/api/v1/admin/model-options` | 返回按三类分组的模型 ID、label 和 capability |

不提供 Provider 或 `.env` 的写 API，不提供 `model_list.yaml` 的 Web 写 API，不提供 paid smoke API。

## 10. 旧架构删除清单

本次是替换，不是迁移。目标分支合并后应同时删除：

### 后端

- 外部 `MasterGateway` HTTP 客户端与 unavailable 占位实现；
- `HARNESS_MASTER_GATEWAY_URL` 和对应配置校验；
- 控制面 `ConfigurationService` 作为全局配置事件源的逻辑；
- Web Key Pool、凭据 revision、启停与轮换 API；
- settings preflight 与 paid smoke API；
- 旧配置向 Image Agent 双写/导出逻辑；
- 正式运行可选 fake/offline Provider 的入口。

### 前端

- 当前 `/settings` 页；
- Ark Key 表单、脱敏凭据卡片；
- 六个裸模型 ID 输入框；
- 真实 smoke 对话框与费用确认；
- 设计师侧的配置 revision 和诊断结果。

### 文件与文档

- `config/harness.example.yaml` 的旧字段；
- 旧 `docs/configuration.md` 与 `docs/master-api.md` 中的 Gateway/Web 凭据说明；
- 旧凭据 JSON、六状态 JSON 和 smoke 文档示例；
- 任何宣称 `/settings` 是普通用户推荐配置入口的文字。

旧 `control-data` 中的配置与密钥不读取、不导入。升级前由管理员自行备份；新版本首次启动只认四个根目录文件。任务、事件、资产和交付数据不属于本次配置删除范围，必须保持可读取。

## 11. 实施顺序

### 阶段 A：新配置内核与启动门禁

1. 增加四个根目录示例和严格 Pydantic schema；
2. 实现受限 `.env` 加载与 `${ENV_NAME}` 替换；
3. 实现三文件聚合校验和 `ConfigSnapshot`；
4. 增加 `scripts/dev.py config-check`；
5. 启动器在创建服务和工作目录前 fail-fast；
6. 禁止正式运行 fallback 到 fake/offline。

### 阶段 B：内置 Master 与资料理解

1. 用 `MasterOrchestrator` 替换外部 Gateway；
2. 接入 Ark/OpenAI-compatible 文本客户端和结构化输出；
3. 实现 TXT/Markdown/PDF/图片资料理解管线；
4. 为 Master 注册按需资料工具；
5. 延续线程、幂等、恢复、提案 revision 和人工确认；
6. 实现配置快照与模型用量审计。

### 阶段 C：Image Agent 单向物化

1. 从根配置解析四个默认模型和高级覆盖；
2. 在实例创建时生成 Image Agent 只读配置；
3. 仅通过子进程环境注入 Provider 秘密；
4. 删除 Agent 私有配置的人工维护与回写入口。

### 阶段 D：前端角色收口

1. 从设计师 Shell 删除 `/settings` 与相关 API 调用；
2. 删除真实 smoke 全部 UI 和后端入口；
3. 将部署错误从设计师业务界面移出；
4. 具备认证/RBAC 后再实现 `/admin/runtime`；
5. 管理员页采用四项默认选择和渐进展开的六状态高级覆盖。

### 阶段 E：一次性清理

1. 删除旧配置代码、示例、测试和文档；
2. 不保留 feature flag、兼容读、双写或旧 Gateway 模式；
3. 更新 README/Quickstart，部署完成标准改为“配置检查通过且服务启动”；
4. 普通用户指南从“创建第一个任务”开始，不出现任何基础设施配置。

阶段 A–E 应在同一个破坏性版本中完成后再发布，不能让主分支长期处于新旧事实源并存状态。

## 12. 测试矩阵

| 层级 | 必测项 | 是否调用真实 Provider |
| --- | --- | --- |
| 配置单元测试 | YAML schema、ENV 替换、秘密拒绝、跨文件引用、类别能力校验 | 否 |
| 启动测试 | 缺 Key、缺模型、错类别、坏 URL、多个错误一次输出、无半初始化 | 否 |
| Provider 契约测试 | Chat/VLM/Images 请求映射、超时、错误归一化、幂等 header | fake HTTP server |
| 资料解析测试 | TXT/MD、数字 PDF、扫描 PDF、图片、坏文件、加密 PDF、页数限制 | fake VLM |
| Master 集成测试 | 澄清、工具调用、PlanProposal 校验、失败恢复、重复消息 | fake text/VLM |
| Image Agent 集成测试 | 四模型继承、六状态覆盖、只读物化、秘密只进环境 | fake Provider |
| 管理 API 测试 | admin 鉴权、revision 冲突、原子保存、脱敏响应 | 否 |
| 设计师 E2E | 导航无设置、任务/素材/计划/审批/交付完整 | fake Provider |
| 安全测试 | 日志/API/快照无 Key，路径与上传防护，非 admin 403 | 否 |

发布门禁不要求生成真实图片。真实供应商连通性属于部署环境和账户状态，不应通过用户界面中的付费动作证明代码正确。

## 13. 验收标准

### 部署管理员

- 只需填写 `.env`、`provider.yaml`、`model_list.yaml`、`runtime.yaml`；
- `config-check` 一次列出全部缺失或冲突字段；
- 配置正确后一个启动命令同时启动 Harness、Web 和受管 Agent；
- 不需要部署或理解 MasterGateway；
- 不需要在 Web 中保存 Key、填写六遍 endpoint 或生成诊断图片；
- 修改 runtime 后，新任务使用新 revision，运行中任务不漂移。

### 普通设计师

- 登录后直接进入任务工作台；
- 看不到设置、YAML、Key、Provider、endpoint、offline 或 smoke；
- 上传支持的资料后，Master 能引用资料中的具体页/块形成计划；
- 资料无法解析时收到可执行的业务提示，而不是配置术语；
- 能完成从需求输入、计划确认到图像交付的完整流程。

### 工程实现

- 根目录三份 YAML 是唯一非敏感事实源；
- 代码和文档中不存在 `HARNESS_MASTER_GATEWAY_URL` 的正式运行依赖；
- 不存在旧配置兼容读、双写和 Web 凭据池；
- Master、VLM、Image 使用统一配置快照与 Provider Adapter；
- 上传资料不再只传引用，必须生成可追溯 `AssetUnderstanding`；
- 所有测试在不调用真实付费模型的情况下通过。

## 14. 明确不做

- P0 不支持 Ark 以外供应商；
- 不为不同 Provider 增加协议选择器；
- 不迁移旧配置或旧凭据池；
- 不在普通用户界面暴露配置；
- 不在产品内生成真实诊断图片；
- 不把 fake/offline 作为生产降级；
- 不默认让 Master 直接吞入所有原始文件；
- 不在没有认证/RBAC 时上线管理员配置页；
- 不因本次配置重构删除既有任务、资产、事件或交付数据。

## 15. 最终产品心智模型

开发者/管理员只做一次部署配置：

```text
填 .env 和三份 YAML → config-check → start
```

普通设计师只做业务工作：

```text
描述需求 → 上传资料 → 与 Master 对齐 → 确认计划 → 审批 → 下载交付
```

两条路径不交叉。配置复杂度由平台开发和部署阶段承担，不转嫁给设计师用户。
