# PPT Agent 前端接入 API

本文只描述前端接入所需的控制面契约。PPT 的澄清、叙事、大纲、样例、全稿确认和 ZIP 导出均在 PPT Agent 工作台内完成；前端不得拼接 Agent 地址或绕过 Harness 的链接校验。

所有控制面端点以 `/api/v1` 为前缀。写请求使用任务当前 `revision`，并在 `envelope` 中携带唯一幂等键、人工身份和预期修订：

```json
{
  "envelope": {
    "idempotency_key": "image-finished-i_image_1-7",
    "actor_type": "human",
    "actor_id": "operator",
    "expected_revision": 7
  }
}
```

## 任务卡与实例字段

PPT TaskCard 的 `parameters` 增加以下字段：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `slide_count` | integer，1–500 | 目标页数，映射为 PPT Agent 的 `target_slide_count` |
| `input_source` | `shared` 或 `empty` | 默认 `shared`；前者读取任务共享区，后者使用实例私有空目录 |

`input_source` 只在 TaskCard 审阅阶段展示，不应在实例启动后静默修改。前端可从任务详情、计划或 WorkItem 投影读取 TaskCard；`shared` 表示 PPT 工作台同步已人工确认发布的图片与同 stem Markdown 说明，`empty` 表示纯文字生成。

Image 实例投影包含 `manual_finished: boolean`，默认 `false`。它只控制 PPT 的启动门禁，不改变 Image 实例的运行状态、交付状态或共享区内容。

## Image 人工结束开关

标记结束：

```http
POST /api/v1/instances/{image_instance_id}/manual-finished
Content-Type: application/json
```

改回进行中：

```http
POST /api/v1/instances/{image_instance_id}/manual-in-progress
Content-Type: application/json
```

两者请求体均为上文的 `envelope`，且只接受 `actor_type: human`。成功返回更新后的任务、计划与 `task_revision`。前端必须用返回的 `task_revision` 更新本地修订，并以返回计划中的 `manual_finished` 为准；重复点击应复用原幂等键，新的用户动作使用新幂等键。

常见错误：

| HTTP | code | 前端处理 |
| --- | --- | --- |
| 404 | `INSTANCE_NOT_FOUND` | 刷新任务；实例已不存在或不属于当前任务 |
| 409 | `REVISION_CONFLICT` | 重新读取任务详情后让用户重试 |
| 409 | `INVALID_STATE_TRANSITION` | 仅 Image、当前实例或权限条件不满足 |
| 422 | `VALIDATION_ERROR` | 不允许非人工身份操作，或请求格式错误 |

## 启动 PPT 与门禁反馈

任务首次启动仍调用任务级确认启动 API。阶段式 image→ppt 流程中，首次确认只启动当前 READY 的 Image 阶段，PPT 不会提前入队。Image 阶段结束后，前端对 PPT 实例调用：

```http
POST /api/v1/instances/{ppt_instance_id}/start
Content-Type: application/json

{
  "operation_id": "start-ppt-i_ppt_1-9",
  "envelope": {
    "idempotency_key": "start-ppt-i_ppt_1-9",
    "actor_type": "human",
    "actor_id": "operator",
    "expected_revision": 9
  }
}
```

只要任务内任一 Image 实例未人工标记结束，服务端返回 HTTP 409：

```json
{
  "schema_version": "1.0",
  "error": {
    "code": "INVALID_STATE_TRANSITION",
    "message": "All Image instances must be manually marked finished before PPT can start.",
    "retryable": false,
    "details": {
      "instance_id": "i_ppt_1",
      "unfinished_instance_ids": ["i_image_1"]
    }
  }
}
```

前端应按 `unfinished_instance_ids` 定位并提示未结束的 Image 卡片。任务没有 Image 实例时不应用此门禁；`input_source` 可为 `shared` 或 `empty`。

## PPT 工作台入口

先通过任务 WorkItem 列表得到 PPT 实例所属的 `work_item_id`，再请求：

```http
GET /api/v1/instances/{ppt_instance_id}/ui-link?task_id={task_id}&work_item_id={work_item_id}
```

前端依据 `link_status` 渲染：

| link_status | 行为 |
| --- | --- |
| `READY` | 仅在 `embeddable: true` 时，把返回的 `ui_url` 放入 sandbox iframe |
| `STARTING` | 展示启动中并轮询同一端点 |
| `NO_UI_URL` | 实例尚未发布工作台地址，保留重试入口 |
| `START_FAILED` | 展示 `diagnostic`，由现有实例恢复入口处理 |
| `FRAME_BLOCKED` | 不创建 iframe，展示 `diagnostic` |
| `ADAPTER_UNAVAILABLE` | 展示能力不可用，禁止启动入口 |

不要缓存、改写或自行推导 `ui_url`。每次打开工作台都重新请求 ui-link；Harness 会校验任务归属、当前 WorkItem、Adapter allowlist、loopback 端口、HTML 内容与 frame policy。`UI_LINK_REJECTED`（HTTP 409）必须作为安全错误展示，不应降级为直接打开 URL。

PPT Agent 工作台拥有自己的人工门和导出按钮。父页面只负责提供 iframe 容器、实例状态与恢复入口，不复制这些业务按钮，也不把 PPT Agent 内部 API 当作公共控制面契约。

## 前端状态建议

PPT 卡片的启动按钮仅在以下条件同时满足时启用：任务已确认启动、PPT 实例为 `READY`、所有 Image 实例的 `manual_finished` 均为 `true`（或任务无 Image）。这是交互优化，不替代服务端门禁。

Image 卡片提供可逆开关并明确标注“仅控制 PPT 启动”。共享区新增已确认图片后，无需重启 PPT 实例；用户在 PPT 工作台执行同步即可看到 `{bundle_id}.{ext}` 与同 stem `{bundle_id}.md`。

## 联调与回归

从仓库根目录执行：

```bash
make p4-acceptance
make check
```

`p4-acceptance` 覆盖仅 Image 的真实受管交付、image→ppt 的可逆启动门禁与真实 PPT 全门/ZIP 导出、仅 PPT 的 `shared`/`empty` 输入准备；`make check` 执行 Harness 全量测试、静态检查、契约、文档和前端回归。
