# Image Agent 内嵌工作台

F4 将 Image WorkItem 的当前实例工作台嵌入统一工作台框架。Harness 只负责主任务导航、WorkItem 上下文、业务状态和安全入口；Image Agent 的专业会话、审批、下载与创作流程仍由其原生页面维护。

## 受控链接

前端只调用：

```text
GET /api/v1/instances/{instance_id}/ui-link
    ?task_id={task_id}
    &work_item_id={work_item_id}
```

两个查询参数都是必需的。服务端依次确认：

1. WorkItem 属于 `task_id`；
2. `instance_id` 是该 WorkItem 的当前实例；
3. 实例快照仍属于同一主任务；
4. URL 的协议、主机、端口和路径符合 Image Adapter 对当前进程的 allowlist；
5. URL 不与 Harness 同源；
6. 目标返回 HTML，且重定向、`X-Frame-Options` 和 CSP `frame-ancestors` 不阻止当前来源嵌入。

URL 缺失或 Agent 不可用时响应保留真实状态；URL 不符合 Adapter allowlist 时返回 `UI_LINK_REJECTED`。显式 frame 策略阻止、探测不可达或非 HTML 响应不会生成空白 iframe，而是进入可重试的错误态；仅在 URL 已通过 Adapter allowlist 后提供 `noopener noreferrer` 新标签回退。

## 浏览器边界

- iframe 固定使用跨源 sandbox，只开放脚本、表单、弹窗、下载、模态框和子页面自身同源能力；不开放顶层导航。
- 父页面 CSP 的 `frame-src` 仅允许本机 Image Agent HTTP 端口，并禁止 Harness 自身被其他页面嵌入。
- 父页面不读取跨域 iframe DOM，也不监听或发送未版本化 `postMessage`。
- iframe 前后均提供键盘导航出口，加载超过 12 秒进入错误恢复界面。
- PPT WorkItem 不请求 UI link，始终显示“能力未接入”的真实边界。

## 验证

API 集成测试覆盖缺失上下文、跨任务上下文、无 URL、合法本机端口、恶意公网 URL 和 `X-Frame-Options: DENY`。Playwright 覆盖成功内嵌、sandbox 权限、键盘进出、受控新标签、frame 阻断错误、PPT 边界和 1280/1440/1920px 无页面级横向溢出。
