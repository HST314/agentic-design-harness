import {
  ApiClient,
  type AgentInstance,
  type Approval,
  type ApprovalDetailResponse,
  type AssetManifest,
  type AuditEvent,
  type InboxItem,
  type InstanceDetailResponse,
  type RetryBudget,
  type TaskFile,
  type TaskDetailResponse,
  type UsageSummary,
} from "./api/client";
import {
  currentRoute,
  navigate,
  routePath,
  type Route,
  type RouteName,
  type TaskSection,
} from "./router";
import {
  agentDescriptor,
  sidebarCapabilityNote,
  stageCapabilityNotice,
  taskCapabilityNotice,
} from "./features/agents";
import {
  actionLabel,
  breadcrumb,
  commandEnvelope,
  commandLabel,
  configureUi,
  detailItem,
  emptyState,
  escapeHtml,
  formatBytes,
  formatDate,
  formatMicros,
  formatNumber,
  icon,
  inboxStatusBadge,
  instanceRow,
  metricCard,
  operationId,
  pageContent,
  renderError,
  refreshServiceState,
  setButtonBusy,
  showInlineError,
  statusBadge,
  usageCompletenessBadge,
  wireNavigation,
} from "./ui";

let root: HTMLDivElement;
const api = new ApiClient();
let renderVersion = 0;
let pollTimer: number | undefined;

const navigation: Array<{ route: RouteName; label: string; icon: string }> = [
  { route: "tasks", label: "任务", icon: "layers" },
  { route: "inbox", label: "收件箱", icon: "inbox" },
  { route: "settings", label: "设置", icon: "settings" },
];

const pageCopy: Record<
  Route["name"],
  { eyebrow: string; title: string; description: string }
> = {
  tasks: {
    eyebrow: "控制平面",
    title: "主任务",
    description: "统一查看阶段、实例与需要处理的运行状态。",
  },
  task: {
    eyebrow: "任务编排",
    title: "任务详情",
    description: "查看阶段进度和当前任务下的专业 Agent 实例。",
  },
  taskSection: {
    eyebrow: "任务视图",
    title: "任务详情",
    description: "按资源、审批、Token 与审计事件查看可追溯证据。",
  },
  instance: {
    eyebrow: "运行实例",
    title: "实例详情",
    description: "查看进程与工作流状态，并深链进入 Image Agent 工作台。",
  },
  inbox: {
    eyebrow: "FIFO 队列",
    title: "收件箱",
    description: "按事件顺序处理人工审批与运行通知，已读和已处理分别记录。",
  },
  settings: {
    eyebrow: "运行配置",
    title: "设置",
    description: "全局配置与凭据只通过受控 API 管理。",
  },
};


function render(): void {
  renderVersion += 1;
  const version = renderVersion;
  window.clearTimeout(pollTimer);
  const route = currentRoute();
  const copy = pageCopy[route.name];
  const selectedNavigation = ["task", "taskSection", "instance"].includes(route.name)
    ? "tasks"
    : route.name;
  root.innerHTML = `
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <div class="shell">
      <aside class="sidebar" aria-label="主导航">
        <a class="brand" href="${routePath("tasks")}" data-nav="tasks" aria-label="返回任务面板">
          <span class="brand-mark" aria-hidden="true">DH</span>
          <span><strong>Design Harness</strong><small>Control plane</small></span>
        </a>
        <nav aria-label="主导航">
          ${navigation
            .map(
              (item) => `<a href="${routePath(item.route)}" data-nav="${item.route}" ${
                item.route === selectedNavigation ? 'aria-current="page"' : ""
              }>${icon(item.icon)}<span>${item.label}</span></a>`,
            )
            .join("")}
        </nav>
        ${sidebarCapabilityNote()}
      </aside>
      <main id="main-content" tabindex="-1">
        <header class="topbar">
          <div><span class="eyebrow">${copy.eyebrow}</span><h1>${copy.title}</h1></div>
          <div class="service-state" role="status" aria-live="polite">
            <span class="status-dot" aria-hidden="true"></span><span>正在检查服务</span>
          </div>
        </header>
        <section class="content content--top" id="page-content" aria-live="polite">
          <div class="loading-card" role="status"><span class="loading-bar"></span><span>正在加载控制面数据</span></div>
        </section>
      </main>
    </div>`;
  wireNavigation();
  void refreshServiceState();
  void renderRoute(route, version);
}

async function renderRoute(route: Route, version: number): Promise<void> {
  try {
    if (route.name === "tasks") await renderTasks(version);
    else if (route.name === "task") await renderTask(route.taskId, version);
    else if (route.name === "taskSection") {
      await renderTaskSection(route.taskId, route.section, version);
    } else if (route.name === "instance") await renderInstance(route.instanceId, version);
    else if (route.name === "inbox") await renderInbox(version);
    else await renderSettings(version);
  } catch (error) {
    if (version !== renderVersion) return;
    renderError(error);
  }
}

async function renderTasks(version: number): Promise<void> {
  const response = await api.tasks();
  if (version !== renderVersion) return;
  const content = pageContent();
  content.innerHTML = response.items.length
    ? `<div class="page-heading"><div><p class="eyebrow">任务面板</p><h2>当前主任务</h2><p>目标、阶段、实例、Token 与最新通知均来自持久化控制面。</p></div><span class="count-pill">${response.items.length} 项</span></div>
       <div class="task-grid">${response.items
         .map(
           (task) => `<article class="task-card">
             <div class="card-topline">${statusBadge(task.status)}<time datetime="${escapeHtml(task.updated_at)}">${formatDate(task.updated_at)}</time></div>
             <h3>${escapeHtml(task.title)}</h3>
             <p>${escapeHtml(task.goal ?? "尚未提供任务目标。")}</p>
             <dl class="task-card__metrics">${detailItem("启动", task.start_policy === "auto" ? "自动" : "人工确认")}${detailItem("阶段 / 实例", `${task.stage_count ?? 0} / ${task.instance_count ?? 0}`)}${detailItem("Token", task.usage_completeness === "NOT_REPORTED" ? "未上报" : formatNumber(task.total_tokens ?? 0))}</dl>
             ${taskCapabilityNotice(task)}
             ${task.latest_notification ? `<p class="task-card__notice"><strong>${escapeHtml(task.latest_notification.title)}</strong><span>${escapeHtml(task.latest_notification.message)}</span></p>` : ""}
             <a class="card-link" href="${routePath({ name: "task", taskId: task.task_id })}" data-task="${escapeHtml(task.task_id)}">查看任务<span>${icon("arrow")}</span></a>
           </article>`,
         )
         .join("")}</div>`
    : emptyState("layers", "还没有主任务", "通过 API 创建任务并保存计划后，会在这里显示。", false);
  wireNavigation();
}

async function renderTask(taskId: string, version: number): Promise<void> {
  const [response, decisions] = await Promise.all([
    api.task(taskId),
    api.taskEvents(taskId, "master"),
  ]);
  if (version !== renderVersion) return;
  const plan = response.plan;
  const body = `
    <section class="task-actions" aria-label="任务操作">
      <div><p class="eyebrow">任务控制</p><h2>计划与运行边界</h2><p>所有命令都携带当前修订与幂等键；冲突时不会覆盖新状态。</p></div>
      <div class="hero-actions">
        ${response.task.status === "AWAITING_START_CONFIRMATION" ? '<button class="button button--primary" type="button" data-task-action="confirm">确认并启动</button>' : ""}
        ${!["SUCCEEDED", "PARTIAL", "CANCELLED"].includes(response.task.status) ? '<button class="button button--danger" type="button" data-task-action="cancel">取消任务</button>' : ""}
      </div><p class="form-feedback" role="status" aria-live="polite"></p>
    </section>
    <div class="section-heading"><div><p class="eyebrow">执行计划</p><h2>阶段与实例</h2></div></div>
    ${
      plan
        ? `<div class="stage-list">${[...plan.stages]
            .sort((left, right) => left.position - right.position)
            .map(
              (stage) => `<section class="stage-card">
                <div class="stage-index" aria-hidden="true">${stage.position}</div>
                <div class="stage-body"><div class="stage-title"><div><p class="eyebrow">${escapeHtml(stage.type)} 阶段</p><h3>${escapeHtml(stage.stage_id)}</h3><small>${stage.depends_on?.length ? `依赖 ${stage.depends_on.map(escapeHtml).join("、")}` : "无前置依赖"}</small></div>${statusBadge(stage.status)}</div>
                ${stageCapabilityNotice(stage.type)}
                <div class="instance-list">${stage.instance_ids
                  .map((id) => instanceRow(plan.instances.find((item) => item.instance_id === id), id))
                  .join("")}</div></div>
              </section>`,
            )
            .join("")}</div>`
        : emptyState("layers", "尚未保存执行计划", "保存计划并创建实例后，可在此查看纵向执行链。", false)
    }
    ${renderDecisionTimeline(decisions.items)}
    ${renderNotificationSummary(response.recent_notifications ?? [])}`;
  pageContent().innerHTML = renderTaskShell(response, "overview", body);
  wireTaskActions(response);
  wireNavigation();
}

async function renderTaskSection(
  taskId: string,
  section: TaskSection,
  version: number,
): Promise<void> {
  const response = await api.task(taskId);
  let body = "";
  if (section === "resources") {
    const resources = await api.taskFiles(taskId);
    body = renderResources(taskId, resources.items, resources.assets);
  } else if (section === "approvals") {
    const approvals = await api.taskApprovals(taskId);
    body = renderTaskApprovals(approvals.items);
  } else if (section === "usage") {
    const [usage, retryBudget] = await Promise.all([
      api.taskUsage(taskId),
      api.retryBudget(taskId),
    ]);
    body = renderUsagePanel(usage, retryBudget.budget);
  } else {
    const events = await api.taskEvents(taskId);
    body = renderAuditEvents(events.items);
  }
  if (version !== renderVersion) return;
  pageContent().innerHTML = renderTaskShell(response, section, body);
  wireNavigation();
  wireResourcePreviews(taskId);
  if (section === "resources") {
    const selected = root.querySelector<HTMLElement>("[data-selected-asset]");
    selected?.scrollIntoView({ block: "center" });
    selected?.focus({ preventScroll: true });
  }
}

function renderTaskShell(
  response: TaskDetailResponse,
  active: "overview" | TaskSection,
  body: string,
): string {
  return `${breadcrumb([{ label: "主任务", route: { name: "tasks" } }, { label: response.task.title }])}
    <div class="detail-hero">
      <div><div class="hero-status">${statusBadge(response.task.status)}<span class="identifier">${escapeHtml(response.task.task_id)}</span></div><h2>${escapeHtml(response.task.title)}</h2><p>${escapeHtml(response.task.goal)}</p></div>
      <dl class="hero-meta"><div><dt>启动策略</dt><dd>${response.task.start_policy === "manual" ? "人工确认" : "自动启动"}</dd></div><div><dt>任务修订</dt><dd>r${response.task_revision}</dd></div><div><dt>Master</dt><dd>${escapeHtml(response.task.master_owner)}</dd></div></dl>
    </div>${taskTabs(response.task.task_id, active)}${body}`;
}

function taskTabs(taskId: string, active: "overview" | TaskSection): string {
  const tabs: Array<{ key: "overview" | TaskSection; label: string }> = [
    { key: "overview", label: "概览" },
    { key: "resources", label: "资源" },
    { key: "approvals", label: "审批" },
    { key: "usage", label: "Token" },
    { key: "events", label: "事件" },
  ];
  return `<nav class="task-tabs" aria-label="任务详情页签">${tabs.map((tab) => {
    const route: Route = tab.key === "overview"
      ? { name: "task", taskId }
      : { name: "taskSection", taskId, section: tab.key };
    return `<a href="${routePath(route)}" data-task-section="${tab.key}" data-task-id="${escapeHtml(taskId)}" ${tab.key === active ? 'aria-current="page"' : ""}>${tab.label}</a>`;
  }).join("")}</nav>`;
}

function renderDecisionTimeline(events: AuditEvent[]): string {
  return `<section class="timeline-section" aria-labelledby="decisions-title"><div class="section-heading"><div><p class="eyebrow">Master 决策</p><h2 id="decisions-title">最近编排记录</h2><p>这里只展示已提交的 Master 命令，不把读取轮询记作决策。</p></div></div>${events.length ? `<ol class="timeline-list">${events.slice(0, 5).map((event) => `<li><span class="timeline-dot" aria-hidden="true"></span><div><strong>${escapeHtml(commandLabel(event.command))}</strong><p>${escapeHtml(event.actor.actor_id)} · ${escapeHtml(event.object_type)} r${event.revision}</p><time datetime="${escapeHtml(event.occurred_at)}">${formatDate(event.occurred_at)}</time></div></li>`).join("")}</ol>` : '<p class="resource-empty">尚无 Master 决策记录。</p>'}</section>`;
}

function renderNotificationSummary(items: InboxItem[]): string {
  return `<section class="timeline-section" aria-labelledby="notifications-title"><div class="section-heading"><div><p class="eyebrow">站内通知</p><h2 id="notifications-title">最新运行提醒</h2></div></div>${items.length ? `<div class="notification-summary">${items.map((item) => `<article>${inboxStatusBadge(item.status)}<div><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.message)}</p></div><time datetime="${escapeHtml(item.created_at)}">${formatDate(item.created_at)}</time></article>`).join("")}</div>` : '<p class="resource-empty">当前任务还没有通知。</p>'}</section>`;
}

function renderTaskApprovals(items: Approval[]): string {
  return `<section class="approval-history" aria-labelledby="approval-history-title"><div class="section-heading"><div><p class="eyebrow">冻结处理人</p><h2 id="approval-history-title">审批记录</h2><p>按创建时间展示待处理与历史决议；已有审批不会因路由切换而迁移。</p></div><span class="count-pill">${items.length} 项</span></div>${items.length ? `<div class="approval-table" role="region" aria-label="任务审批记录" tabindex="0"><table><thead><tr><th scope="col">步骤</th><th scope="col">处理人</th><th scope="col">状态</th><th scope="col">实例</th><th scope="col">时间</th></tr></thead><tbody>${items.map((item) => `<tr><th scope="row">${escapeHtml(actionLabel(item.step_id))}</th><td>${item.owner === "human" ? "人工" : "Master"}</td><td>${statusBadge(item.status)}</td><td><a href="${routePath({ name: "instance", instanceId: item.instance_id })}" data-instance="${escapeHtml(item.instance_id)}">${escapeHtml(item.instance_id)}</a></td><td>${formatDate(item.created_at)}</td></tr>`).join("")}</tbody></table></div>` : '<p class="resource-empty">当前任务没有审批记录。</p>'}</section>`;
}

function renderAuditEvents(items: AuditEvent[]): string {
  return `<section class="event-section" aria-labelledby="events-title"><div class="section-heading"><div><p class="eyebrow">只读审计</p><h2 id="events-title">任务事件</h2><p>事件投影不暴露命令载荷、幂等键、文件路径或凭据内容。</p></div><span class="count-pill">${items.length} 条</span></div>${items.length ? `<div class="event-table" role="region" aria-label="任务审计事件" tabindex="0"><table><thead><tr><th scope="col">时间</th><th scope="col">Actor</th><th scope="col">命令</th><th scope="col">对象</th><th scope="col">修订</th><th scope="col">结果</th></tr></thead><tbody>${items.map((item) => `<tr><td>${formatDate(item.occurred_at)}</td><td>${escapeHtml(item.actor.actor_type)} / ${escapeHtml(item.actor.actor_id)}</td><th scope="row">${escapeHtml(commandLabel(item.command))}</th><td>${escapeHtml(item.object_type)} · ${escapeHtml(item.object_id)}</td><td>r${item.revision}</td><td><span class="badge badge--success"><span aria-hidden="true"></span>已提交</span></td></tr>`).join("")}</tbody></table></div>` : '<p class="resource-empty">当前任务没有可展示的提交事件。</p>'}</section>`;
}

function wireTaskActions(response: TaskDetailResponse): void {
  root.querySelectorAll<HTMLButtonElement>("[data-task-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      const action = button.dataset.taskAction;
      if (action === "cancel" && !window.confirm("确认取消该任务及其仍可取消的实例？工作区和审计记录会保留。")) return;
      const feedback = root.querySelector<HTMLElement>(".task-actions .form-feedback");
      setButtonBusy(button, action === "confirm" ? "正在启动" : "正在取消");
      try {
        const body = {
          operation_id: operationId(`task_${action}`),
          envelope: commandEnvelope("human", "human_operator", response.task_revision),
        };
        if (action === "confirm") await api.confirmTaskStart(response.task.task_id, body);
        else await api.cancelTask(response.task.task_id, body);
        if (feedback) feedback.textContent = "命令已提交，正在刷新任务状态。";
        await renderTask(response.task.task_id, renderVersion);
      } catch (error) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        if (feedback) {
          feedback.classList.add("form-feedback--error");
          feedback.textContent = error instanceof Error ? error.message : "任务命令提交失败。";
        }
      }
    });
  });
}

async function renderInstance(instanceId: string, version: number): Promise<void> {
  window.clearTimeout(pollTimer);
  pollTimer = undefined;
  const [response, usage] = await Promise.all([
    api.instance(instanceId),
    api.instanceUsage(instanceId),
  ]);
  if (version !== renderVersion) return;
  renderInstanceContent(response, usage);
  wireNavigation();
  if (["STARTING", "RUNNING"].includes(response.instance.status)) {
    pollTimer = window.setTimeout(
      () => {
        if (!root.querySelector("[data-instance-config]:focus-within")) {
          void renderRoute({ name: "instance", instanceId }, version);
        }
      },
      5000,
    );
  }
}

function renderInstanceContent(
  response: InstanceDetailResponse,
  usage: UsageSummary,
): void {
  const { instance, observation } = response;
  const process = instance.process;
  const instanceConfig = response.config ?? {
    config_revision: instance.config_revision,
    restart_required: false,
    config: {},
  };
  const safeUiUrl = instance.ui_url?.match(/^https?:\/\//) ? instance.ui_url : null;
  pageContent().innerHTML = `
    ${breadcrumb([
      { label: "主任务", route: { name: "tasks" } },
      { label: response.task_id, route: { name: "task", taskId: response.task_id } },
      { label: instance.instance_id },
    ])}
    <div class="detail-hero detail-hero--instance">
      <div><div class="hero-status">${statusBadge(instance.status)}<span class="identifier">${escapeHtml(instance.instance_id)}</span></div><h2>${escapeHtml(agentDescriptor(instance.agent_type).instanceTitle)}</h2><p>Harness 负责进程、状态和审计；专业创作流程保留在独立工作台。</p></div>
      <div class="hero-actions">
        <button class="button button--secondary" type="button" data-refresh>${icon("refresh")}刷新状态</button>
        ${
          safeUiUrl
            ? `<a class="button button--primary" href="${escapeHtml(safeUiUrl)}" target="_blank" rel="noopener noreferrer">${icon("external")}打开工作台</a>`
            : '<span class="button button--disabled" aria-disabled="true">工作台尚未就绪</span>'
        }
        ${instance.status === "READY" ? '<button class="button button--primary" type="button" data-instance-action="start">启动实例</button>' : ""}
        ${instance.delivery_rejection?.retryable ? '<button class="button button--primary" type="button" data-delivery-retry>重新校验交付</button>' : ""}
        ${["RUNNING", "WAITING_APPROVAL", "FAILED_TO_START", "FAILED", "CRASHED"].includes(instance.status) && !instance.delivery_rejection ? '<button class="button button--secondary" type="button" data-instance-action="restart">重启实例</button>' : ""}
        ${["UNAVAILABLE", "READY", "STARTING", "RUNNING", "WAITING_APPROVAL"].includes(instance.status) ? '<button class="button button--danger" type="button" data-instance-action="cancel">取消实例</button>' : ""}
        ${["UNAVAILABLE", "FAILED_TO_START", "SUCCEEDED", "FAILED", "CRASHED", "CANCELLED", "SUPERSEDED"].includes(instance.status) ? '<button class="button button--secondary" type="button" data-instance-action="archive">归档实例</button>' : ""}
      </div>
    </div>
    ${
      instance.delivery_rejection
        ? `<div class="alert alert--danger" role="alert"><strong>交付未通过发布校验</strong><span>${escapeHtml(instance.delivery_rejection.message)}（${escapeHtml(instance.delivery_rejection.code)}）</span><span>不合格文件仍隔离在实例输出区；重新校验不会重跑已完成的模型步骤。</span></div>`
        : ""
    }
    <div class="detail-grid">
      <section class="info-card"><p class="eyebrow">运行进程</p><h3>隔离运行环境</h3><dl class="detail-list">
        ${detailItem("进程状态", process?.state ?? "未启动")}
        ${detailItem("PID", process?.pid?.toString() ?? "—")}
        ${detailItem("端口", process?.port?.toString() ?? "—")}
        ${detailItem("配置修订", `r${instance.config_revision}`)}
      </dl></section>
      <section class="info-card"><p class="eyebrow">Agent 观测</p><h3>${observation?.step_id ? escapeHtml(observation.step_id) : "等待运行数据"}</h3><dl class="detail-list">
        ${detailItem("Job", observation?.details.job_status ?? "未上报")}
        ${detailItem("Timeline 游标", observation?.details.timeline_cursor?.toString() ?? "—")}
        ${detailItem("审批模式", instance.approval_mode === "human" ? "人工" : "Master")}
        ${detailItem("能力数量", observation?.capabilities.length.toString() ?? "0")}
      </dl></section>
      <section class="info-card"><p class="eyebrow">脱敏凭据</p><h3>${escapeHtml(response.credential?.credential_pair_id ?? instance.credential_pair_ref ?? "未分配")}</h3><dl class="detail-list">
        ${detailItem("Provider", response.credential?.provider ?? "未公开")}
        ${detailItem("Key ID", response.credential?.key_id ?? "未公开")}
        ${detailItem("Key 尾号", response.credential?.key_tail ?? "••••")}
        ${detailItem("凭据修订", `r${response.credential?.revision ?? instance.credential_pair_revision ?? 1}`)}
      </dl></section>
    </div>
    <section class="settings-card instance-config-card" aria-labelledby="instance-config-title">
      <div><p class="eyebrow">实例局部配置</p><h3 id="instance-config-title">有效参数 r${instanceConfig.config_revision}</h3><p>${instanceConfig.restart_required ? "该变更需要受控重启后生效。" : "保存后由 Adapter 尝试热应用；无法热应用时会明确标记需重启。"}</p></div>
      <form data-instance-config data-config-revision="${instanceConfig.config_revision}">
        <div class="field"><label for="instance-config-json">配置 JSON</label><textarea id="instance-config-json" name="config" rows="10" spellcheck="false">${escapeHtml(JSON.stringify(instanceConfig.config, null, 2))}</textarea></div>
        <div class="form-actions"><button class="button button--primary" type="submit">保存实例配置</button></div><p class="form-feedback" role="status" aria-live="polite"></p>
      </form>
    </section>
    ${renderUsagePanel(usage)}
    ${
      observation?.details.compatibility_error
        ? `<div class="alert alert--danger" role="alert"><strong>兼容性检查失败</strong><span>${escapeHtml(observation.details.compatibility_error)}</span></div>`
        : ""
    }
    ${
      observation?.capabilities.length
        ? `<section class="capability-card"><div><p class="eyebrow">冻结能力</p><h3>等待下一步决议</h3><p>审批项会冻结当前处理人，裁决后由 Harness 幂等推进专业工作流。</p></div><div class="chip-list">${observation.capabilities.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div></section>`
        : ""
    }
    <section class="approval-routing" aria-labelledby="routing-title">
      <div><p class="eyebrow">审批路由</p><h3 id="routing-title">${instance.approval_mode === "human" ? "由人工处理" : "由 Master 处理"}</h3><p>新的审批项采用此设置；已经创建的审批仍保留原处理人。</p></div>
      <button class="button button--secondary" type="button" data-approval-mode="${instance.approval_mode === "human" ? "master" : "human"}">切换为${instance.approval_mode === "human" ? " Master" : "人工"}</button>
    </section>
    ${
      response.pending_approval
        ? `<section class="pending-card"><div><p class="eyebrow">待处理审批</p><h3>${escapeHtml(actionLabel(response.pending_approval.step_id))}</h3><p class="identifier">${escapeHtml(response.pending_approval.approval_id)}</p></div><a class="button button--primary" href="/inbox?approval_id=${encodeURIComponent(response.pending_approval.approval_id)}" data-inbox-link>前往收件箱${icon("arrow")}</a></section>`
        : ""
    }`;
  wireInstanceOperations(response);
  root.querySelector<HTMLButtonElement>("[data-delivery-retry]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    if (button.disabled) return;
    setButtonBusy(button, "正在重新校验");
    try {
      await api.retryDelivery(instance.instance_id, {
        operation_id: operationId("delivery_retry"),
        envelope: commandEnvelope("human", "human_operator", response.task_revision),
      });
      await renderInstance(instance.instance_id, renderVersion);
    } catch (error) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      showInlineError(button, error);
    }
  });
  root.querySelector<HTMLFormElement>("[data-instance-config]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    await submitJsonForm(form, async (value) => {
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("实例配置必须是 JSON 对象。");
      await api.updateInstanceConfig(instance.instance_id, {
        patch: value,
        operation_id: operationId("instance_config"),
        envelope: commandEnvelope("human", "human_operator", Number(form.dataset.configRevision)),
      });
      await renderInstance(instance.instance_id, renderVersion);
    }, "config");
  });
  root.querySelector<HTMLButtonElement>("[data-refresh]")?.addEventListener("click", (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.innerHTML = `${icon("refresh")}正在刷新`;
    const route = currentRoute();
    if (route.name === "instance") void renderRoute(route, renderVersion);
  });
  root.querySelector<HTMLButtonElement>("[data-approval-mode]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    const approvalMode = button.dataset.approvalMode;
    if (!approvalMode || button.disabled) return;
    const idleLabel = button.textContent ?? "切换审批模式";
    setButtonBusy(button, "正在保存");
    try {
      await api.updateApprovalMode(instance.instance_id, {
        approval_mode: approvalMode,
        envelope: commandEnvelope("human", "human_operator", response.task_revision),
      });
      await renderInstance(instance.instance_id, renderVersion);
    } catch (error) {
      showInlineError(button, error);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = idleLabel;
    }
  });
  root.querySelector<HTMLElement>("[data-inbox-link]")?.addEventListener("click", (event) => {
    event.preventDefault();
    const approvalId = response.pending_approval?.approval_id;
    window.history.pushState({}, "", `/inbox?approval_id=${encodeURIComponent(approvalId ?? "")}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}

function wireInstanceOperations(response: InstanceDetailResponse): void {
  root.querySelectorAll<HTMLButtonElement>("[data-instance-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.instanceAction as
        | "start"
        | "restart"
        | "cancel"
        | "archive"
        | undefined;
      if (!action || button.disabled) return;
      if (["cancel", "archive"].includes(action)) {
        const verb = action === "cancel" ? "取消" : "归档";
        if (!window.confirm(`确认${verb}该实例？工作区和审计记录会保留。`)) return;
      }
      setButtonBusy(button, "正在提交");
      try {
        await api.instanceOperation(response.instance.instance_id, action, {
          operation_id: operationId(`instance_${action}`),
          envelope: commandEnvelope("human", "human_operator", response.task_revision),
        });
        await renderInstance(response.instance.instance_id, renderVersion);
      } catch (error) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        showInlineError(button, error);
      }
    });
  });
}

async function renderInbox(version: number): Promise<void> {
  const response = await api.inbox();
  const approvalEntries = await Promise.all(
    response.items.map(async (item): Promise<[string, ApprovalDetailResponse | null]> => {
      if (!item.approval_id) return [item.inbox_id, null];
      try {
        return [item.inbox_id, await api.approval(item.approval_id)];
      } catch {
        return [item.inbox_id, null];
      }
    }),
  );
  if (version !== renderVersion) return;
  const approvals = new Map(approvalEntries);
  const selectedApproval = new URLSearchParams(window.location.search).get("approval_id");
  const pendingCount = response.items.filter((item) => item.status !== "HANDLED").length;
  pageContent().innerHTML = response.items.length
    ? `<div class="page-heading"><div><p class="eyebrow">人工队列</p><h2>按到达顺序处理</h2><p>最早事件排在最前；待处理、已读和已处理彼此独立。</p></div><span class="count-pill">${pendingCount} 待处理</span></div>
       <div class="inbox-list">${response.items
         .map((item) => renderInboxItem(item, approvals.get(item.inbox_id) ?? null, selectedApproval))
         .join("")}</div>`
    : emptyState("inbox", "收件箱已清空", "新的审批与运行通知会按事件顺序出现在这里。", false);
  wireInboxActions();
  const selected = root.querySelector<HTMLElement>("[data-selected-approval]");
  selected?.scrollIntoView({ block: "center" });
}

function renderInboxItem(
  item: InboxItem & { store_revision?: number },
  details: ApprovalDetailResponse | null,
  selectedApproval: string | null,
): string {
  const selected = selectedApproval === item.approval_id;
  const actions = details?.payload.available_actions ?? [];
  const pending = details?.approval.status === "PENDING";
  return `<article class="inbox-card${selected ? " inbox-card--selected" : ""}" data-inbox-id="${escapeHtml(item.inbox_id)}" ${selected ? "data-selected-approval" : ""}>
    <div class="inbox-card__head"><div>${inboxStatusBadge(item.status)}<span class="event-kind">${escapeHtml(item.kind)}</span></div><time datetime="${escapeHtml(item.created_at)}">${formatDate(item.created_at)}</time></div>
    <h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.message)}</p>
    <dl class="inbox-meta">${detailItem("任务", item.task_id)}${detailItem("实例", item.instance_id ?? "—")}${detailItem("队列序号", item.sequence.toString())}</dl>
    ${
      details
        ? `<details class="approval-context"><summary>查看审批上下文</summary><pre>${escapeHtml(JSON.stringify(details.payload.context, null, 2))}</pre></details>`
        : ""
    }
    <div class="inbox-actions">
      ${item.status === "UNREAD" ? `<button class="button button--secondary" type="button" data-mark-read data-revision="${item.store_revision ?? item.revision}">${icon("check")}标为已读</button>` : ""}
      ${item.instance_id ? `<a class="button button--secondary" href="${routePath({ name: "instance", instanceId: item.instance_id })}" data-instance="${escapeHtml(item.instance_id)}">查看实例</a>` : ""}
    </div>
    ${
      details && pending
        ? `<form class="approval-form" data-approval-id="${escapeHtml(details.approval.approval_id)}" data-approval-revision="${details.approval_revision}">
          <div class="field"><label for="action-${escapeHtml(item.inbox_id)}">推进动作</label><select id="action-${escapeHtml(item.inbox_id)}" name="action" ${actions.length ? "" : "disabled"}>${actions.map((action) => `<option value="${escapeHtml(action)}">${escapeHtml(actionLabel(action))} · ${escapeHtml(action)}</option>`).join("")}</select></div>
          <div class="field"><label for="payload-${escapeHtml(item.inbox_id)}">动作参数（JSON）</label><textarea id="payload-${escapeHtml(item.inbox_id)}" name="payload" rows="4" spellcheck="false">{}</textarea><small>只填写当前动作需要的字段；无参数时保留 {}。</small></div>
          <div class="field"><label for="actor-${escapeHtml(item.inbox_id)}">操作人 ID</label><input id="actor-${escapeHtml(item.inbox_id)}" name="actor_id" value="human_operator" pattern="[A-Za-z][A-Za-z0-9_\\-]{0,127}" required></div>
          <div class="form-actions"><button class="button button--primary" type="submit" data-decision="APPROVED">批准并推进</button><button class="button button--danger" type="submit" data-decision="REJECTED">拒绝</button></div>
          <p class="form-feedback" role="status" aria-live="polite"></p>
        </form>`
        : item.approval_id
          ? `<p class="handled-copy">${details ? "该审批已完成处理。" : "审批详情暂时不可用，请刷新重试。"}</p>`
          : ""
    }
  </article>`;
}

function wireInboxActions(): void {
  wireNavigation();
  root.querySelectorAll<HTMLButtonElement>("[data-mark-read]").forEach((button) => {
    button.addEventListener("click", async () => {
      const card = button.closest<HTMLElement>("[data-inbox-id]");
      if (!card?.dataset.inboxId || button.disabled) return;
      setButtonBusy(button, "正在保存");
      try {
        await api.updateInboxStatus(card.dataset.inboxId, {
          status: "READ",
          envelope: commandEnvelope("human", "human_operator", Number(button.dataset.revision)),
        });
        await renderInbox(renderVersion);
      } catch (error) {
        showInlineError(button, error);
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.innerHTML = `${icon("check")}重试标为已读`;
      }
    });
  });
  root.querySelectorAll<HTMLFormElement>(".approval-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitter = (event as SubmitEvent).submitter as HTMLButtonElement | null;
      const approvalId = form.dataset.approvalId;
      if (!submitter || !approvalId) return;
      const feedback = form.querySelector<HTMLElement>(".form-feedback");
      const controls = Array.from(form.querySelectorAll<HTMLButtonElement>("button"));
      controls.forEach((control) => { control.disabled = true; });
      submitter.setAttribute("aria-busy", "true");
      if (feedback) feedback.textContent = "正在提交决议…";
      try {
        const data = new FormData(form);
        const actorId = String(data.get("actor_id") ?? "");
        const decision = submitter.dataset.decision ?? "APPROVED";
        const payload = JSON.parse(String(data.get("payload") ?? "{}")) as Record<string, unknown>;
        await api.resolveApproval(approvalId, {
          decision,
          action: decision === "APPROVED" ? String(data.get("action") ?? "") : null,
          payload,
          operation_id: operationId("approval"),
          envelope: commandEnvelope("human", actorId, Number(form.dataset.approvalRevision)),
        });
        if (feedback) feedback.textContent = "决议已提交，正在同步状态。";
        await renderInbox(renderVersion);
      } catch (error) {
        if (feedback) {
          feedback.classList.add("form-feedback--error");
          feedback.textContent = error instanceof Error ? error.message : "提交失败，请重试。";
        }
        controls.forEach((control) => { control.disabled = false; });
        submitter.removeAttribute("aria-busy");
      }
    });
  });
}

function renderResources(taskId: string, files: TaskFile[], assets: AssetManifest[]): string {
  const manifests = new Map(assets.map((entry) => [entry.manifest.relative_path, entry]));
  const selectedAssetId = new URLSearchParams(window.location.search).get("asset_id");
  const deliverables = files.filter((file) => !file.relative_path.includes("/manifests/"));
  const groups = [
    { key: "inputs", label: "任务输入", files: deliverables.filter((file) => file.relative_path.startsWith("inputs/")) },
    { key: "shared", label: "公共交付", files: deliverables.filter((file) => file.relative_path.startsWith("resources/shared/")) },
    { key: "instances", label: "实例输出", files: deliverables.filter((file) => file.relative_path.startsWith("instances/")) },
  ];
  return `<section class="resources-section" aria-labelledby="resources-title"><div class="section-heading"><div><p class="eyebrow">受控资源</p><h2 id="resources-title">任务文件</h2><p>只展示已提交且实时完整性校验通过的文件；未发布候选不会混入公共交付。</p></div><span class="count-pill">${deliverables.length} 个文件</span></div>${groups.map((group) => `<section class="resource-group" aria-labelledby="resource-group-${group.key}"><div class="resource-group__heading"><h3 id="resource-group-${group.key}">${group.label}</h3><span>${group.files.length}</span></div>${group.files.length ? `<div class="resource-grid">${group.files.map((file) => { const asset = manifests.get(file.relative_path); return resourceCard(taskId, file, asset, asset?.manifest.asset_id === selectedAssetId); }).join("")}</div>` : '<p class="resource-empty">该分组暂无已提交文件。</p>'}</section>`).join("")}</section>`;
}

function resourceCard(
  taskId: string,
  file: TaskFile,
  asset: AssetManifest | undefined,
  selected = false,
): string {
  const imagePreview = file.previewable && file.mime_type.startsWith("image/");
  const textPreview = file.previewable && !imagePreview;
  return `<article class="resource-card${selected ? " resource-card--selected" : ""}" tabindex="-1" ${selected ? "data-selected-asset" : ""}>${imagePreview ? `<img src="${escapeHtml(api.previewUrl(taskId, file.relative_path))}" alt="${escapeHtml(file.filename)} 预览" loading="lazy" width="480" height="320">` : `<div class="resource-file-icon" aria-hidden="true">${icon("image")}</div>`}<div class="resource-card__body"><p class="eyebrow">${escapeHtml(asset?.manifest.role ?? file.mime_type)}</p><h3 title="${escapeHtml(file.filename)}">${escapeHtml(file.filename)}</h3><p>${escapeHtml(asset?.manifest.description || formatBytes(file.size_bytes))}</p><dl class="resource-meta">${detailItem("来源实例", asset?.manifest.producer_instance_id ?? "用户输入")}${detailItem("完整性", asset?.integrity_status ?? "VERIFIED")}${detailItem("SHA-256", file.sha256.slice(0, 12) + "…")}</dl><div class="resource-actions">${textPreview ? `<button class="button button--secondary" type="button" data-preview-path="${escapeHtml(file.relative_path)}">安全预览</button>` : ""}<a class="button button--secondary" href="${escapeHtml(api.downloadUrl(taskId, file.relative_path))}" download>${icon("download")}下载文件</a></div>${textPreview ? '<pre class="resource-text-preview" tabindex="0" hidden></pre>' : ""}</div></article>`;
}

function wireResourcePreviews(taskId: string): void {
  root.querySelectorAll<HTMLButtonElement>("[data-preview-path]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.disabled || !button.dataset.previewPath) return;
      const output = button.closest(".resource-card")?.querySelector<HTMLPreElement>(".resource-text-preview");
      if (!output) return;
      setButtonBusy(button, "正在读取");
      try {
        output.textContent = await api.previewText(taskId, button.dataset.previewPath);
        output.hidden = false;
        output.focus();
        button.textContent = "已安全预览";
      } catch (error) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        showInlineError(button, error);
      }
    });
  });
}

async function renderSettings(version: number): Promise<void> {
  const [globalConfig, keyPool] = await Promise.all([
    api.globalConfig(),
    api.keyPool(),
  ]);
  if (version !== renderVersion) return;
  const revision = globalConfig.config.revision;
  const { revision: _revision, ...editableConfig } = globalConfig.config;
  pageContent().innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">受控配置</p><h2>运行配置与凭据池</h2><p>全局保存覆盖所有未归档实例；凭据始终按 Key + Base URL 完整配对。</p></div><span class="count-pill">全局 r${revision}</span></div>
    <div class="settings-grid">
      <section class="settings-card" aria-labelledby="global-config-title">
        <div><p class="eyebrow">全局配置</p><h3 id="global-config-title">Image 运行参数</h3><p>仅接受受控 Schema 字段；运行中实例会热应用，无法热应用时明确标记需要重启。</p></div>
        <form data-global-config data-revision="${revision}">
          <div class="field"><label for="global-config-json">配置 JSON</label><textarea id="global-config-json" name="config" rows="18" spellcheck="false">${escapeHtml(JSON.stringify(editableConfig, null, 2))}</textarea><small>保存采用 revision 检查，避免覆盖其他操作人的新版本。</small></div>
          <div class="form-actions"><button class="button button--primary" type="submit">保存全局配置</button></div>
          <p class="form-feedback" role="status" aria-live="polite"></p>
        </form>
      </section>
      <section class="settings-card" aria-labelledby="key-pool-title">
        <div><p class="eyebrow">凭据池</p><h3 id="key-pool-title">完整凭据对</h3><p>明文 Key 只在本次提交中发送，响应、事件和页面均不回显。</p></div>
        <div class="credential-list">${
          keyPool.items.length
            ? keyPool.items.map((item) => `<article><div>${statusBadge(item.enabled ? "READY" : "CANCELLED")}<span class="identifier">${escapeHtml(item.credential_pair_id)}</span></div><dl>${detailItem("Provider", item.provider)}${detailItem("Key ID", item.key_id)}${detailItem("Key 尾号", item.key_tail)}${detailItem("服务地址", item.base_url_hint)}${detailItem("修订", `r${item.revision}`)}</dl></article>`).join("")
            : '<p class="resource-empty">尚未配置凭据对。</p>'
        }</div>
        <form data-key-pool>
          <div class="field"><label for="key-pool-json">替换凭据池（JSON 数组）</label><textarea id="key-pool-json" name="pairs" rows="10" spellcheck="false" placeholder='[{"credential_pair_id":"cred_01", …}]'></textarea><small>同一 revision 不可修改；编辑时递增 revision。保存后输入框会立即清空。</small></div>
          <div class="form-actions"><button class="button button--primary" type="submit">安全保存凭据池</button></div>
          <p class="form-feedback" role="status" aria-live="polite"></p>
        </form>
      </section>
    </div>`;
  wireSettingsActions();
}

function renderUsagePanel(usage: UsageSummary, budget?: RetryBudget): string {
  const maximum = budget?.retry_policy.max_auto_retry_tokens_task ?? 0;
  const consumed = budget
    ? budget.retry_budget_ledger.retry_tokens_settled +
      budget.retry_budget_ledger.retry_tokens_reserved
    : 0;
  const progress = maximum > 0 ? Math.min(100, Math.round((consumed / maximum) * 100)) : 0;
  const boundedProgressValue = maximum > 0 ? Math.min(consumed, maximum) : 0;
  const peakTokens = Math.max(1, ...usage.time_buckets.map((item) => item.tokens.total_tokens));
  const modelTotal = Math.max(1, usage.models.reduce((sum, item) => sum + item.tokens.total_tokens, 0));
  const instanceTotal = Math.max(
    1,
    usage.instances.reduce((sum, item) => sum + item.tokens.total_tokens, 0),
  );
  return `<section class="usage-section" aria-labelledby="usage-title-${escapeHtml(usage.instance_id ?? usage.task_id)}">
    <div class="section-heading"><div><p class="eyebrow">Token 与费用</p><h2 id="usage-title-${escapeHtml(usage.instance_id ?? usage.task_id)}">用量观测</h2><p>${usage.completeness === "NOT_REPORTED" ? "当前 Agent 尚未上报用量；这里不会把缺失数据显示成 0 用量。" : "原始事件可重建，汇总按实例、模型与时间保持可追溯。"}</p></div>${usageCompletenessBadge(usage.completeness)}</div>
    <div class="metric-grid">
      ${metricCard("总 Token", formatNumber(usage.tokens.total_tokens), `${usage.event_count} 次调用`)}
      ${metricCard("输入 / 输出", `${formatNumber(usage.tokens.input_tokens)} / ${formatNumber(usage.tokens.output_tokens)}`, `缓存 ${formatNumber(usage.tokens.cached_input_tokens)}`)}
      ${metricCard("已知费用", usage.cost.completeness === "UNKNOWN" ? "费用未知" : formatMicros(usage.cost.known_micros), `${usage.cost.unpriced_event_count} 条未定价`)}
      ${metricCard("模型", formatNumber(usage.models.length), usage.models[0]?.model ?? "尚无记录")}
    </div>
    ${usage.time_buckets.length || usage.models.length || usage.instances.length ? `<div class="usage-visual-grid">${usage.time_buckets.length ? `<figure class="usage-chart"><figcaption><strong>按小时 Token</strong><span>柱高表示相对用量，数值标签可被辅助技术读取。</span></figcaption><div class="usage-bars" role="img" aria-label="${escapeHtml(usage.time_buckets.map((item) => `${formatDate(item.hour)} ${formatNumber(item.tokens.total_tokens)} Token`).join("；"))}">${usage.time_buckets.slice(-12).map((item) => `<div><span style="height:${Math.max(8, Math.round((item.tokens.total_tokens / peakTokens) * 100))}%"></span><small>${new Date(item.hour).getHours().toString().padStart(2, "0")}:00</small><b>${formatNumber(item.tokens.total_tokens)}</b></div>`).join("")}</div></figure>` : ""}${usage.models.length ? `<figure class="model-share"><figcaption><strong>模型占比</strong><span>同时使用文字和长度表达，避免只依赖颜色。</span></figcaption><div>${usage.models.map((item) => { const share = Math.round((item.tokens.total_tokens / modelTotal) * 100); return `<article><p><span>${escapeHtml(item.model)}</span><strong>${share}%</strong></p><div role="progressbar" aria-label="${escapeHtml(item.model)} Token 占比" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${share}"><span style="width:${share}%"></span></div></article>`; }).join("")}</div></figure>` : ""}${usage.instances.length ? `<figure class="instance-share"><figcaption><strong>实例占比</strong><span>每个实例均显示精确百分比和可读进度。</span></figcaption><div>${usage.instances.map((item) => { const share = Math.round((item.tokens.total_tokens / instanceTotal) * 100); return `<article><p><span>${escapeHtml(item.instance_id)}</span><strong>${share}%</strong></p><div role="progressbar" aria-label="${escapeHtml(item.instance_id)} Token 占比" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${share}"><span style="width:${share}%"></span></div></article>`; }).join("")}</div></figure>` : ""}</div>` : ""}
    ${budget ? `<div class="budget-card"><div><p class="eyebrow">自动重试预算</p><h3>${budget.retry_budget_ledger.frozen ? "预算已冻结" : `已用与预留 ${formatNumber(consumed)} / ${formatNumber(maximum)}`}</h3><p>${budget.retry_budget_ledger.frozen ? escapeHtml(budget.retry_budget_ledger.frozen_reason ?? "实际用量超过预留") : `每组最多 ${budget.retry_policy.max_auto_retries_per_retry_group} 次；费用上限${budget.retry_policy.max_auto_retry_cost_micros === null ? "未配置" : formatMicros(budget.retry_policy.max_auto_retry_cost_micros)}。`}</p></div>${maximum > 0 ? `<div class="budget-progress" role="progressbar" aria-label="自动重试 Token 预算" aria-valuemin="0" aria-valuemax="${maximum}" aria-valuenow="${boundedProgressValue}"><span style="width:${progress}%"></span></div>` : '<p class="budget-zero">自动重试 Token 额度为 0；重试将进入人工预算确认。</p>'}</div>` : ""}
    ${usage.instances.length ? `<div class="usage-table" role="region" aria-label="实例 Token 汇总" tabindex="0"><table><thead><tr><th scope="col">实例</th><th scope="col">完整性</th><th scope="col">输入</th><th scope="col">输出</th><th scope="col">总计</th><th scope="col">费用</th></tr></thead><tbody>${usage.instances.map((item) => `<tr><th scope="row">${escapeHtml(item.instance_id)}</th><td>${usageCompletenessBadge(item.completeness)}</td><td>${formatNumber(item.tokens.input_tokens)}</td><td>${formatNumber(item.tokens.output_tokens)}</td><td>${formatNumber(item.tokens.total_tokens)}</td><td>${item.cost.completeness === "UNKNOWN" ? "未知" : formatMicros(item.cost.known_micros)}</td></tr>`).join("")}</tbody></table></div>` : ""}
    ${usage.events.length ? `<details class="usage-events"><summary>查看最近调用（${usage.events.length}）</summary><div>${usage.events.slice(-20).reverse().map((event) => `<article><span>${escapeHtml(event.model)}</span><strong>${escapeHtml(formatUsageQuantity(event))}</strong><small>${escapeHtml(event.provider_request_id ?? event.request_id)} · ${formatDate(event.occurred_at)}</small></article>`).join("")}</div></details>` : ""}
  </section>`;
}

function formatUsageQuantity(event: UsageSummary["events"][number]): string {
  const imageUnits = event.billing_units?.filter((item) => item.unit === "image") ?? [];
  if (imageUnits.length) {
    const quantity = imageUnits.reduce((sum, item) => sum + item.quantity, 0);
    const details = imageUnits[0]?.attributes ?? {};
    const qualifiers = [details.resolution, details.model_tier]
      .filter((item): item is string | number => typeof item === "string" || typeof item === "number")
      .map(String);
    return `${formatNumber(quantity)} 张图片${qualifiers.length ? ` · ${qualifiers.join(" · ")}` : ""}`;
  }
  return `${formatNumber(event.total_tokens)} Token`;
}

function wireSettingsActions(): void {
  wireNavigation();
  root.querySelector<HTMLFormElement>("[data-global-config]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    await submitJsonForm(form, async (value) => {
      await api.updateGlobalConfig({
        config: value,
        operation_id: operationId("global_config"),
        envelope: commandEnvelope("human", "human_operator", Number(form.dataset.revision)),
      });
    }, "config");
  });
  root.querySelector<HTMLFormElement>("[data-key-pool]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    await submitJsonForm(form, async (value) => {
      if (!Array.isArray(value) || value.length === 0) throw new Error("请填写至少一个完整凭据对。");
      await api.updateKeyPool({
        pairs: value,
        envelope: commandEnvelope("human", "human_operator", 0),
      });
      const textarea = form.elements.namedItem("pairs") as HTMLTextAreaElement;
      textarea.value = "";
    }, "pairs");
  });
}

async function submitJsonForm(
  form: HTMLFormElement,
  submit: (value: unknown) => Promise<void>,
  field: string,
): Promise<void> {
  const button = form.querySelector<HTMLButtonElement>("button[type='submit']");
  const feedback = form.querySelector<HTMLElement>(".form-feedback");
  if (!button || button.disabled) return;
  setButtonBusy(button, "正在安全保存");
  if (feedback) feedback.textContent = "正在校验并提交…";
  try {
    const value = JSON.parse(String(new FormData(form).get(field) ?? "")) as unknown;
    await submit(value);
    if (feedback) feedback.textContent = "保存成功，正在刷新受控视图。";
    if (currentRoute().name === "settings") await renderSettings(renderVersion);
  } catch (error) {
    if (feedback) {
      feedback.classList.add("form-feedback--error");
      feedback.textContent = error instanceof Error ? error.message : "保存失败，请检查输入。";
    }
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = "重试保存";
  }
}


export function mountLegacyApp(element: HTMLDivElement): () => void {
  root = element;
  configureUi(root, api, render);
  const handlePopState = (): void => {
    render();
    root.querySelector<HTMLElement>("#main-content")?.focus();
  };
  window.addEventListener("popstate", handlePopState);
  render();
  return () => {
    window.clearTimeout(pollTimer);
    pollTimer = undefined;
    window.removeEventListener("popstate", handlePopState);
    element.replaceChildren();
  };
}
