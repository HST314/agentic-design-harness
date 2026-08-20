import "./styles.css";
import {
  ApiClient,
  type AgentInstance,
  type InstanceDetailResponse,
} from "./api/client";
import { currentRoute, navigate, routePath, type Route, type RouteName } from "./router";

function requireAppRoot(): HTMLDivElement {
  const element = document.querySelector<HTMLDivElement>("#app");
  if (!element) throw new Error("Application root is missing.");
  return element;
}

const root = requireAppRoot();
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
  instance: {
    eyebrow: "运行实例",
    title: "实例详情",
    description: "查看进程与工作流状态，并深链进入 Image Agent 工作台。",
  },
  inbox: {
    eyebrow: "FIFO 队列",
    title: "收件箱",
    description: "审批与运行通知将在 G3 工作包接入。",
  },
  settings: {
    eyebrow: "运行配置",
    title: "设置",
    description: "全局配置与凭据只通过受控 API 管理。",
  },
};

const statusLabels: Record<string, string> = {
  ARCHIVED: "已归档",
  AWAITING_START_CONFIRMATION: "等待启动",
  BLOCKED_UNAVAILABLE: "能力不可用",
  CANCELLED: "已取消",
  CREATED: "已创建",
  DRAFT: "草稿",
  FAILED: "失败",
  FAILED_TO_START: "启动失败",
  PARTIAL: "部分完成",
  PLANNED: "已规划",
  READY: "就绪",
  RUNNING: "运行中",
  STARTING: "启动中",
  SUCCEEDED: "已完成",
  UNAVAILABLE: "未接入",
  WAITING_APPROVAL: "等待审批",
};

function icon(name: string): string {
  const paths: Record<string, string> = {
    arrow: '<path d="m9 18 6-6-6-6"/>',
    external: '<path d="M14 3h7v7M10 14 21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m21 15-5-5L5 20"/>',
    inbox: '<path d="M4 4h16l2 11h-6l-2 3h-4l-2-3H2L4 4Z"/>',
    layers: '<path d="M12 2 2 7l10 5 10-5-10-5Zm-8.5 9L12 15.25 20.5 11M3.5 15 12 19.25 20.5 15"/>',
    refresh: '<path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"/>',
    settings: '<path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4v-.08A1.7 1.7 0 0 0 8.95 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.58 15 1.7 1.7 0 0 0 3 14H3v-4h.08A1.7 1.7 0 0 0 4.6 8.95a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.58 1.7 1.7 0 0 0 10 3h4v.08a1.7 1.7 0 0 0 1.03 1.52 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.13.4.5.77 1.52 1H21v4h-.08A1.7 1.7 0 0 0 19.4 15Z"/>',
  };
  return `<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[name] ?? ""}</svg>`;
}

function render(): void {
  renderVersion += 1;
  const version = renderVersion;
  window.clearTimeout(pollTimer);
  const route = currentRoute();
  const copy = pageCopy[route.name];
  const selectedNavigation = route.name === "task" || route.name === "instance" ? "tasks" : route.name;
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
        <div class="capability-note">
          <span class="status-dot status-dot--muted" aria-hidden="true"></span>
          <span><strong>PPT 尚未接入</strong><small>计划节点与状态可用</small></span>
        </div>
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
    else if (route.name === "instance") await renderInstance(route.instanceId, version);
    else renderPlaceholder(route.name, version);
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
    ? `<div class="page-heading"><div><p class="eyebrow">任务面板</p><h2>当前主任务</h2><p>选择任务查看阶段和实例；运行状态来自持久化控制面。</p></div><span class="count-pill">${response.items.length} 项</span></div>
       <div class="task-grid">${response.items
         .map(
           (task) => `<article class="task-card">
             <div class="card-topline">${statusBadge(task.status)}<time datetime="${escapeHtml(task.updated_at)}">${formatDate(task.updated_at)}</time></div>
             <h3>${escapeHtml(task.title)}</h3>
             <p class="identifier">${escapeHtml(task.task_id)}</p>
             <a class="card-link" href="${routePath({ name: "task", taskId: task.task_id })}" data-task="${escapeHtml(task.task_id)}">查看任务<span>${icon("arrow")}</span></a>
           </article>`,
         )
         .join("")}</div>`
    : emptyState("layers", "还没有主任务", "通过 API 创建任务并保存计划后，会在这里显示。", false);
  wireNavigation();
}

async function renderTask(taskId: string, version: number): Promise<void> {
  const response = await api.task(taskId);
  if (version !== renderVersion) return;
  const plan = response.plan;
  pageContent().innerHTML = `
    ${breadcrumb([{ label: "主任务", route: { name: "tasks" } }, { label: response.task.title }])}
    <div class="detail-hero">
      <div><div class="hero-status">${statusBadge(response.task.status)}<span class="identifier">${escapeHtml(response.task.task_id)}</span></div><h2>${escapeHtml(response.task.title)}</h2><p>${escapeHtml(response.task.goal)}</p></div>
      <dl class="hero-meta"><div><dt>启动策略</dt><dd>${response.task.start_policy === "manual" ? "人工确认" : "自动启动"}</dd></div><div><dt>任务修订</dt><dd>r${response.task_revision}</dd></div><div><dt>Master</dt><dd>${escapeHtml(response.task.master_owner)}</dd></div></dl>
    </div>
    <div class="section-heading"><div><p class="eyebrow">执行计划</p><h2>阶段与实例</h2></div></div>
    ${
      plan
        ? `<div class="stage-list">${plan.stages
            .sort((left, right) => left.position - right.position)
            .map(
              (stage) => `<section class="stage-card">
                <div class="stage-index" aria-hidden="true">${stage.position}</div>
                <div class="stage-body"><div class="stage-title"><div><p class="eyebrow">${escapeHtml(stage.type)} 阶段</p><h3>${escapeHtml(stage.stage_id)}</h3></div>${statusBadge(stage.status)}</div>
                <div class="instance-list">${stage.instance_ids
                  .map((id) => instanceRow(plan.instances.find((item) => item.instance_id === id), id))
                  .join("")}</div></div>
              </section>`,
            )
            .join("")}</div>`
        : emptyState("layers", "尚未保存执行计划", "保存计划并创建实例后，可在此查看纵向执行链。", false)
    }`;
  wireNavigation();
}

async function renderInstance(instanceId: string, version: number): Promise<void> {
  window.clearTimeout(pollTimer);
  pollTimer = undefined;
  const response = await api.instance(instanceId);
  if (version !== renderVersion) return;
  renderInstanceContent(response);
  wireNavigation();
  if (["STARTING", "RUNNING"].includes(response.instance.status)) {
    pollTimer = window.setTimeout(() => void renderInstance(instanceId, version), 1500);
  }
}

function renderInstanceContent(response: InstanceDetailResponse): void {
  const { instance, observation } = response;
  const process = instance.process;
  const safeUiUrl = instance.ui_url?.match(/^https?:\/\//) ? instance.ui_url : null;
  pageContent().innerHTML = `
    ${breadcrumb([
      { label: "主任务", route: { name: "tasks" } },
      { label: response.task_id, route: { name: "task", taskId: response.task_id } },
      { label: instance.instance_id },
    ])}
    <div class="detail-hero detail-hero--instance">
      <div><div class="hero-status">${statusBadge(instance.status)}<span class="identifier">${escapeHtml(instance.instance_id)}</span></div><h2>${instance.agent_type === "image" ? "Image Agent 实例" : "专业 Agent 实例"}</h2><p>Harness 负责进程、状态和审计；专业创作流程保留在独立工作台。</p></div>
      <div class="hero-actions">
        <button class="button button--secondary" type="button" data-refresh>${icon("refresh")}刷新状态</button>
        ${
          safeUiUrl
            ? `<a class="button button--primary" href="${escapeHtml(safeUiUrl)}" target="_blank" rel="noopener noreferrer">${icon("external")}打开工作台</a>`
            : '<span class="button button--disabled" aria-disabled="true">工作台尚未就绪</span>'
        }
      </div>
    </div>
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
    </div>
    ${
      observation?.details.compatibility_error
        ? `<div class="alert alert--danger" role="alert"><strong>兼容性检查失败</strong><span>${escapeHtml(observation.details.compatibility_error)}</span></div>`
        : ""
    }
    ${
      observation?.capabilities.length
        ? `<section class="capability-card"><div><p class="eyebrow">冻结能力</p><h3>等待下一步决议</h3><p>G2 只展示 Image Agent 返回的能力；审批与推进将在 G3 接入。</p></div><div class="chip-list">${observation.capabilities.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div></section>`
        : ""
    }`;
  root.querySelector<HTMLButtonElement>("[data-refresh]")?.addEventListener("click", (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.innerHTML = `${icon("refresh")}正在刷新`;
    const route = currentRoute();
    if (route.name === "instance") void renderInstance(route.instanceId, renderVersion);
  });
}

function renderPlaceholder(route: "inbox" | "settings", version: number): void {
  if (version !== renderVersion) return;
  const copy = pageCopy[route];
  pageContent().innerHTML = emptyState(
    route,
    `${copy.title}将在后续工作包接入`,
    copy.description,
    true,
  );
}

function instanceRow(instance: AgentInstance | undefined, fallbackId: string): string {
  if (!instance) return "";
  return `<a class="instance-row" href="${routePath({ name: "instance", instanceId: instance.instance_id })}" data-instance="${escapeHtml(instance.instance_id)}"><span class="agent-icon">${icon(instance.agent_type === "image" ? "image" : "layers")}</span><span><strong>${instance.agent_type === "image" ? "Image Agent" : escapeHtml(instance.agent_type)}</strong><small>${escapeHtml(fallbackId)}</small></span>${statusBadge(instance.status)}<span class="row-arrow">${icon("arrow")}</span></a>`;
}

function breadcrumb(items: Array<{ label: string; route?: Route }>): string {
  return `<nav class="breadcrumb" aria-label="面包屑">${items
    .map((item) => {
      const label = escapeHtml(item.label);
      if (!item.route) return `<span aria-current="page">${label}</span>`;
      const data =
        item.route.name === "task"
          ? `data-task="${escapeHtml(item.route.taskId)}"`
          : `data-nav="${item.route.name}"`;
      return `<a href="${routePath(item.route)}" ${data}>${label}</a><span aria-hidden="true">/</span>`;
    })
    .join("")}</nav>`;
}

function detailItem(label: string, value: string): string {
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`;
}

function statusBadge(status: string): string {
  const style = ["RUNNING", "STARTING"].includes(status)
    ? "active"
    : status === "SUCCEEDED"
      ? "success"
      : ["FAILED", "FAILED_TO_START", "BLOCKED_UNAVAILABLE"].includes(status)
        ? "danger"
        : status === "WAITING_APPROVAL"
          ? "warning"
          : "neutral";
  return `<span class="badge badge--${style}"><span aria-hidden="true"></span>${escapeHtml(statusLabels[status] ?? status)}</span>`;
}

function emptyState(iconName: string, title: string, description: string, boundary: boolean): string {
  return `<div class="empty-card"><span class="empty-icon" aria-hidden="true">${icon(iconName)}</span><p class="eyebrow">阶段能力</p><h2>${escapeHtml(title)}</h2><p>${escapeHtml(description)}</p>${boundary ? '<p class="boundary-copy">当前页面保留稳定导航和能力边界，不展示虚假业务数据。</p>' : ""}</div>`;
}

function renderError(error: unknown): void {
  const message = error instanceof Error ? error.message : "无法读取控制面数据。";
  pageContent().innerHTML = `<div class="alert alert--danger" role="alert"><strong>页面加载失败</strong><span>${escapeHtml(message)}</span><button class="button button--secondary" type="button" data-retry>重新加载</button></div>`;
  root.querySelector<HTMLButtonElement>("[data-retry]")?.addEventListener("click", render);
}

function pageContent(): HTMLElement {
  const content = root.querySelector<HTMLElement>("#page-content");
  if (!content) throw new Error("Page content root is missing.");
  return content;
}

function wireNavigation(): void {
  root.querySelectorAll<HTMLElement>("[data-nav]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const name = link.dataset.nav as RouteName | undefined;
      if (name) navigate({ name });
    });
  });
  root.querySelectorAll<HTMLElement>("[data-task]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (link.dataset.task) navigate({ name: "task", taskId: link.dataset.task });
    });
  });
  root.querySelectorAll<HTMLElement>("[data-instance]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (link.dataset.instance) {
        navigate({ name: "instance", instanceId: link.dataset.instance });
      }
    });
  });
}

async function refreshServiceState(): Promise<void> {
  const element = root.querySelector<HTMLElement>(".service-state");
  if (!element) return;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 3000);
  try {
    const readiness = await api.readiness(controller.signal);
    element.classList.toggle("service-state--ready", readiness.status === "ready");
    const label = element.querySelector("span:last-child");
    if (label) label.textContent = readiness.status === "ready" ? "服务就绪" : "服务未就绪";
  } catch {
    const label = element.querySelector("span:last-child");
    if (label) label.textContent = "服务不可达";
  } finally {
    window.clearTimeout(timeout);
  }
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "时间未知"
    : new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

function escapeHtml(value: string): string {
  return value.replace(
    /[&<>'"]/g,
    (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character] ?? character,
  );
}

window.addEventListener("popstate", () => {
  render();
  root.querySelector<HTMLElement>("#main-content")?.focus();
});
render();
