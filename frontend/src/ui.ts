import type { AgentInstance, ApiClient, InboxItem, UsageSummary } from "./api/client";
import {
  navigate,
  routePath,
  type Route,
  type RouteName,
  type TaskSection,
} from "./router";
import { agentDescriptor } from "./features/agents";

let appRoot: HTMLDivElement;
let apiClient: ApiClient;
let rerender: () => void;

export function configureUi(
  root: HTMLDivElement,
  api: ApiClient,
  render: () => void,
): void {
  appRoot = root;
  apiClient = api;
  rerender = render;
}
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

export function icon(name: string): string {
  const paths: Record<string, string> = {
    arrow: '<path d="m9 18 6-6-6-6"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    download: '<path d="M12 3v12m0 0 4-4m-4 4-4-4M5 21h14"/>',
    external: '<path d="M14 3h7v7M10 14 21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m21 15-5-5L5 20"/>',
    inbox: '<path d="M4 4h16l2 11h-6l-2 3h-4l-2-3H2L4 4Z"/>',
    layers: '<path d="M12 2 2 7l10 5 10-5-10-5Zm-8.5 9L12 15.25 20.5 11M3.5 15 12 19.25 20.5 15"/>',
    refresh: '<path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"/>',
  };
  return `<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[name] ?? ""}</svg>`;
}


export function instanceRow(instance: AgentInstance | undefined, fallbackId: string): string {
  if (!instance) return "";
  const descriptor = agentDescriptor(instance.agent_type);
  return `<a class="instance-row" href="${routePath({ name: "instance", instanceId: instance.instance_id })}" data-instance="${escapeHtml(instance.instance_id)}"><span class="agent-icon">${icon(descriptor.icon)}</span><span><strong>${escapeHtml(descriptor.label)}</strong><small>${escapeHtml(fallbackId)}</small></span>${statusBadge(instance.status)}<span class="row-arrow">${icon("arrow")}</span></a>`;
}

export function breadcrumb(items: Array<{ label: string; route?: Route }>): string {
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

export function detailItem(label: string, value: string): string {
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`;
}

export function inboxStatusBadge(status: InboxItem["status"]): string {
  const labels = { UNREAD: "未读", READ: "已读", HANDLED: "已处理" };
  const style = status === "UNREAD" ? "warning" : status === "HANDLED" ? "success" : "neutral";
  return `<span class="badge badge--${style}"><span aria-hidden="true"></span>${labels[status]}</span>`;
}

export function usageCompletenessBadge(
  status: UsageSummary["completeness"],
): string {
  const label = {
    COMPLETE: "完整上报",
    PARTIAL: "部分上报",
    NOT_REPORTED: "未上报",
  }[status];
  const style = status === "COMPLETE" ? "success" : status === "PARTIAL" ? "warning" : "neutral";
  return `<span class="badge badge--${style}"><span aria-hidden="true"></span>${label}</span>`;
}

export function metricCard(label: string, value: string, detail: string): string {
  return `<article class="metric-card"><p>${escapeHtml(label)}</p><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></article>`;
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

export function formatMicros(value: number): string {
  return `¥${(value / 1_000_000).toFixed(4)}`;
}

export function actionLabel(action: string): string {
  const labels: Record<string, string> = {
    answer_clarification: "回答澄清问题",
    approve_taskbook: "批准任务书",
    select_master: "选择主方案",
    approve_final: "批准最终交付",
    choose_category: "选择视觉类别",
    choose_skill: "选择创作技能",
    submit_manual_action: "提交人工动作",
    regenerate: "重新生成",
    approve_once: "批准一次预算越权",
  };
  return labels[action] ?? action;
}

export function commandLabel(command: string): string {
  const labels: Record<string, string> = {
    create_task: "创建任务",
    save_plan: "保存执行计划",
    confirm_start: "确认启动",
    cancel_task: "取消任务",
    transition_instance: "更新实例状态",
    set_approval_mode: "切换审批路由",
    commit_resolution: "提交审批决议",
  };
  return labels[command] ?? command.replaceAll("_", " ");
}

export function operationId(prefix: string): string {
  const suffix = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replaceAll("-", "")
    : `${Date.now()}_${Math.random().toString(16).slice(2)}`;
  return `${prefix}_${suffix}`;
}

export function commandEnvelope(actorType: "human" | "master", actorId: string, revision: number): Record<string, unknown> {
  return {
    idempotency_key: operationId("ui"),
    expected_revision: revision,
    actor_type: actorType,
    actor_id: actorId,
  };
}

export function setButtonBusy(button: HTMLButtonElement, label: string): void {
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = label;
}

export function showInlineError(anchor: HTMLElement, error: unknown): void {
  anchor.parentElement?.querySelector("[data-inline-error]")?.remove();
  const message = error instanceof Error ? error.message : "操作失败，请重试。";
  anchor.insertAdjacentHTML(
    "afterend",
    `<p class="inline-error" data-inline-error role="alert">${escapeHtml(message)}</p>`,
  );
}

export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

export function statusBadge(status: string): string {
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

export function emptyState(iconName: string, title: string, description: string, boundary: boolean): string {
  return `<div class="empty-card"><span class="empty-icon" aria-hidden="true">${icon(iconName)}</span><p class="eyebrow">阶段能力</p><h2>${escapeHtml(title)}</h2><p>${escapeHtml(description)}</p>${boundary ? '<p class="boundary-copy">当前页面保留稳定导航和能力边界，不展示虚假业务数据。</p>' : ""}</div>`;
}

export function renderError(error: unknown): void {
  const message = error instanceof Error ? error.message : "无法读取控制面数据。";
  pageContent().innerHTML = `<div class="alert alert--danger" role="alert"><strong>页面加载失败</strong><span>${escapeHtml(message)}</span><button class="button button--secondary" type="button" data-retry>重新加载</button></div>`;
  appRoot.querySelector<HTMLButtonElement>("[data-retry]")?.addEventListener("click", rerender);
}

export function pageContent(): HTMLElement {
  const content = appRoot.querySelector<HTMLElement>("#page-content");
  if (!content) throw new Error("Page content appRoot is missing.");
  return content;
}

export function wireNavigation(): void {
  appRoot.querySelectorAll<HTMLElement>("[data-nav]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const name = link.dataset.nav as RouteName | undefined;
      if (name) navigate({ name });
    });
  });
  appRoot.querySelectorAll<HTMLElement>("[data-task]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (link.dataset.task) navigate({ name: "task", taskId: link.dataset.task });
    });
  });
  appRoot.querySelectorAll<HTMLElement>("[data-instance]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (link.dataset.instance) {
        navigate({ name: "instance", instanceId: link.dataset.instance });
      }
    });
  });
  appRoot.querySelectorAll<HTMLElement>("[data-task-section]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const taskId = link.dataset.taskId;
      const section = link.dataset.taskSection;
      if (!taskId) return;
      if (section === "overview") navigate({ name: "task", taskId });
      else if (["resources", "approvals", "usage", "events"].includes(section ?? "")) {
        navigate({ name: "taskSection", taskId, section: section as TaskSection });
      }
    });
  });
}

export async function refreshServiceState(): Promise<void> {
  const element = appRoot.querySelector<HTMLElement>(".service-state");
  if (!element) return;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 3000);
  try {
    const readiness = await apiClient.readiness(controller.signal);
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

export function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "时间未知"
    : new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

export function escapeHtml(value: string): string {
  return value.replace(
    /[&<>'"]/g,
    (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character] ?? character,
  );
}
