# PPT Agent 前端接入 API

本文只描述前端接入所需的控制面契约。PPT 的澄清、叙事、大纲、样例、全稿确认和 ZIP 导出均在 PPT Agent 工作台内完成；前端不得拼接 Agent 地址或绕过 Harness 的链接校验。

所有控制面端点以 `/api/v1` 为前缀。写请求使用任务当前 `revision`，并在 `envelope` 中携带唯一幂等键、人工身份和预期修订：

```json
{
  "envelope": {
    "idempotency_key": "update-work-item-status-7",
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

WorkItem 列表响应包含 `task_revision`，任务卡使用统一的四态业务状态：`TODO`、`RUNNING`、`WAITING_APPROVAL`、`COMPLETED`。运行异常仍保留在 `raw_status` 与 `alerts` 中，前端将需要处理的异常卡放入待办栏。

## 更新任务卡状态

看板与计划页通过同一个端点更新当前状态：

```http
PATCH /api/v1/tasks/{task_id}/work-items/{work_item_id}/status
Content-Type: application/json

{
  "business_status": "COMPLETED",
  "envelope": {
    "idempotency_key": "complete-work-item-7",
    "actor_type": "human",
    "actor_id": "operator",
    "expected_revision": 7
  }
}
```

成功响应是刷新后的完整 WorkItem 列表，任务卡应立即移动到对应栏。用户选择的状态保持到下一次真实实例状态变化；运行时事件到达后，系统状态继续接管。Image 卡选择 `COMPLETED` 时同时满足 PPT 启动门禁，选择其他状态则重新关闭该人工完成条件。

常见错误：

| HTTP | code | 前端处理 |
| --- | --- | --- |
| 404 | `INSTANCE_NOT_FOUND` | 刷新任务；WorkItem 当前实例已不存在 |
| 409 | `REVISION_CONFLICT` | 重新读取任务详情后让用户重试 |
| 422 | `VALIDATION_ERROR` | 状态不在四态集合内、身份无权操作，或请求格式错误 |

## 启动 PPT 与门禁反馈

任务首次启动仍调用任务级确认启动 API。阶段式 image→ppt 流程中，首次确认只启动当前 READY 的 Image 阶段。所有 Image 任务已完成后，Master 面板对 PPT 实例调用：

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

只要任务内任一 Image WorkItem 尚未完成，服务端返回 HTTP 409：

```json
{
  "schema_version": "1.0",
  "error": {
    "code": "INVALID_STATE_TRANSITION",
    "message": "All Image WorkItems must be completed before PPT can start.",
    "retryable": false,
    "details": {
      "instance_id": "i_ppt_1",
      "unfinished_instance_ids": ["i_image_1"]
    }
  }
}
```

前端应按 `unfinished_instance_ids` 定位并提示尚未完成的 Image 卡片。任务没有 Image 实例时不应用此门禁；`input_source` 可为 `shared` 或 `empty`。

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
| `NO_UI_URL` | 返回 Master 面板启动 PPT；子 Agent 页面不提供启动操作 |
| `START_FAILED` | 展示 `diagnostic` 并返回 Master 处理恢复 |
| `FRAME_BLOCKED` | 不创建 iframe，展示 `diagnostic` |
| `ADAPTER_UNAVAILABLE` | 展示能力不可用，禁止启动入口 |

不要缓存、改写或自行推导 `ui_url`。每次打开工作台都重新请求 ui-link；Harness 会校验任务归属、当前 WorkItem、Adapter allowlist、loopback 端口、HTML 内容与 frame policy。`UI_LINK_REJECTED`（HTTP 409）必须作为安全错误展示，不应降级为直接打开 URL。

PPT Agent 工作台拥有自己的人工门和导出按钮。父页面只负责提供 iframe 容器与实例状态，不复制这些业务按钮，也不把 PPT Agent 内部 API 当作公共控制面契约。

## 前端状态建议

PPT 启动操作只出现在 Master 计划卡。未启动的 PPT 看板卡不可进入工作台；实例进入 `STARTING` 或 `RUNNING` 后，卡片恢复工作台入口。Image 完成条件直接通过看板/计划状态下拉设置。

共享区新增已确认图片后，无需重启 PPT 实例；用户在 PPT 工作台执行同步即可看到 `{bundle_id}.{ext}` 与同 stem `{bundle_id}.md`。

## 联调与回归

从仓库根目录执行：

```bash
make p4-acceptance
make check
```

`p4-acceptance` 覆盖仅 Image 的真实受管交付、image→ppt 的可逆启动门禁与真实 PPT 全门/ZIP 导出、仅 PPT 的 `shared`/`empty` 输入准备；`make check` 执行 Harness 全量测试、静态检查、契约、文档和前端回归。
