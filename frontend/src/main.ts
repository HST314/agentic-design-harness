import "./styles.css";
import {
  ApiClient,
  type AgentInstance,
  type ApprovalDetailResponse,
  type AssetManifest,
  type InboxItem,
  type InstanceDetailResponse,
  type RetryBudget,
  type TaskFile,
  type UsageSummary,
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
    description: "按事件顺序处理人工审批与运行通知，已读和已处理分别记录。",
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
    check: '<path d="m5 12 4 4L19 6"/>',
    download: '<path d="M12 3v12m0 0 4-4m-4 4-4-4M5 21h14"/>',
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
  const [response, resources, usage, retryBudget] = await Promise.all([
    api.task(taskId),
    api.taskFiles(taskId),
    api.taskUsage(taskId),
    api.retryBudget(taskId),
  ]);
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
    }
    ${renderUsagePanel(usage, retryBudget.budget)}
    ${renderResources(taskId, resources.items, resources.assets)}`;
  wireNavigation();
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
      () => void renderRoute({ name: "instance", instanceId }, version),
      1500,
    );
  }
}

function renderInstanceContent(
  response: InstanceDetailResponse,
  usage: UsageSummary,
): void {
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
          <div class="field"><label for="actor-${escapeHtml(item.inbox_id)}">操作人 ID</label><input id="actor-${escapeHtml(item.inbox_id)}" name="actor_id" value="human_operator" pattern="[A-Za-z][A-Za-z0-9_-]{0,127}" required></div>
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
  const deliverables = files.filter((file) => !file.relative_path.includes("/manifests/"));
  return `<section class="resources-section" aria-labelledby="resources-title"><div class="section-heading"><div><p class="eyebrow">受控资源</p><h2 id="resources-title">任务文件</h2><p>这里只展示已提交并通过实时完整性校验的输入和交付物。</p></div><span class="count-pill">${deliverables.length} 个文件</span></div>
    ${deliverables.length ? `<div class="resource-grid">${deliverables.map((file) => {
      const asset = manifests.get(file.relative_path);
      const imagePreview = file.previewable && file.mime_type.startsWith("image/");
      return `<article class="resource-card">${imagePreview ? `<img src="${escapeHtml(api.previewUrl(taskId, file.relative_path))}" alt="${escapeHtml(file.filename)} 预览" loading="lazy">` : `<div class="resource-file-icon" aria-hidden="true">${icon("image")}</div>`}<div class="resource-card__body"><p class="eyebrow">${escapeHtml(asset?.manifest.role ?? file.mime_type)}</p><h3 title="${escapeHtml(file.filename)}">${escapeHtml(file.filename)}</h3><p>${escapeHtml(asset?.manifest.description || formatBytes(file.size_bytes))}</p><dl class="resource-meta">${detailItem("来源实例", asset?.manifest.producer_instance_id ?? "用户输入")}${detailItem("完整性", asset?.integrity_status ?? "VERIFIED")}</dl><a class="button button--secondary" href="${escapeHtml(api.downloadUrl(taskId, file.relative_path))}" download>${icon("download")}下载文件</a></div></article>`;
    }).join("")}</div>` : `<div class="resource-empty">当前没有已提交的任务资源。</div>`}
  </section>`;
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
  return `<section class="usage-section" aria-labelledby="usage-title-${escapeHtml(usage.instance_id ?? usage.task_id)}">
    <div class="section-heading"><div><p class="eyebrow">Token 与费用</p><h2 id="usage-title-${escapeHtml(usage.instance_id ?? usage.task_id)}">用量观测</h2><p>${usage.completeness === "NOT_REPORTED" ? "当前 Agent 尚未上报用量；这里不会把缺失数据显示成 0 用量。" : "原始事件可重建，汇总按实例、模型与时间保持可追溯。"}</p></div>${usageCompletenessBadge(usage.completeness)}</div>
    <div class="metric-grid">
      ${metricCard("总 Token", formatNumber(usage.tokens.total_tokens), `${usage.event_count} 次调用`)}
      ${metricCard("输入 / 输出", `${formatNumber(usage.tokens.input_tokens)} / ${formatNumber(usage.tokens.output_tokens)}`, `缓存 ${formatNumber(usage.tokens.cached_input_tokens)}`)}
      ${metricCard("已知费用", usage.cost.completeness === "UNKNOWN" ? "费用未知" : formatMicros(usage.cost.known_micros), `${usage.cost.unpriced_event_count} 条未定价`)}
      ${metricCard("模型", formatNumber(usage.models.length), usage.models[0]?.model ?? "尚无记录")}
    </div>
    ${budget ? `<div class="budget-card"><div><p class="eyebrow">自动重试预算</p><h3>${budget.retry_budget_ledger.frozen ? "预算已冻结" : `已用与预留 ${formatNumber(consumed)} / ${formatNumber(maximum)}`}</h3><p>${budget.retry_budget_ledger.frozen ? escapeHtml(budget.retry_budget_ledger.frozen_reason ?? "实际用量超过预留") : `每组最多 ${budget.retry_policy.max_auto_retries_per_retry_group} 次；费用上限${budget.retry_policy.max_auto_retry_cost_micros === null ? "未配置" : formatMicros(budget.retry_policy.max_auto_retry_cost_micros)}。`}</p></div>${maximum > 0 ? `<div class="budget-progress" role="progressbar" aria-label="自动重试 Token 预算" aria-valuemin="0" aria-valuemax="${maximum}" aria-valuenow="${boundedProgressValue}"><span style="width:${progress}%"></span></div>` : '<p class="budget-zero">自动重试 Token 额度为 0；重试将进入人工预算确认。</p>'}</div>` : ""}
    ${usage.instances.length ? `<div class="usage-table" role="region" aria-label="实例 Token 汇总" tabindex="0"><table><thead><tr><th scope="col">实例</th><th scope="col">完整性</th><th scope="col">输入</th><th scope="col">输出</th><th scope="col">总计</th><th scope="col">费用</th></tr></thead><tbody>${usage.instances.map((item) => `<tr><th scope="row">${escapeHtml(item.instance_id)}</th><td>${usageCompletenessBadge(item.completeness)}</td><td>${formatNumber(item.tokens.input_tokens)}</td><td>${formatNumber(item.tokens.output_tokens)}</td><td>${formatNumber(item.tokens.total_tokens)}</td><td>${item.cost.completeness === "UNKNOWN" ? "未知" : formatMicros(item.cost.known_micros)}</td></tr>`).join("")}</tbody></table></div>` : ""}
    ${usage.events.length ? `<details class="usage-events"><summary>查看最近调用（${usage.events.length}）</summary><div>${usage.events.slice(-20).reverse().map((event) => `<article><span>${escapeHtml(event.model)}</span><strong>${formatNumber(event.total_tokens)} Token</strong><small>${escapeHtml(event.request_id)} · ${formatDate(event.occurred_at)}</small></article>`).join("")}</div></details>` : ""}
  </section>`;
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

function inboxStatusBadge(status: InboxItem["status"]): string {
  const labels = { UNREAD: "未读", READ: "已读", HANDLED: "已处理" };
  const style = status === "UNREAD" ? "warning" : status === "HANDLED" ? "success" : "neutral";
  return `<span class="badge badge--${style}"><span aria-hidden="true"></span>${labels[status]}</span>`;
}

function usageCompletenessBadge(
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

function metricCard(label: string, value: string, detail: string): string {
  return `<article class="metric-card"><p>${escapeHtml(label)}</p><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></article>`;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function formatMicros(value: number): string {
  return `¥${(value / 1_000_000).toFixed(4)}`;
}

function actionLabel(action: string): string {
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

function operationId(prefix: string): string {
  const suffix = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replaceAll("-", "")
    : `${Date.now()}_${Math.random().toString(16).slice(2)}`;
  return `${prefix}_${suffix}`;
}

function commandEnvelope(actorType: "human" | "master", actorId: string, revision: number): Record<string, unknown> {
  return {
    idempotency_key: operationId("ui"),
    expected_revision: revision,
    actor_type: actorType,
    actor_id: actorId,
  };
}

function setButtonBusy(button: HTMLButtonElement, label: string): void {
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = label;
}

function showInlineError(anchor: HTMLElement, error: unknown): void {
  anchor.parentElement?.querySelector("[data-inline-error]")?.remove();
  const message = error instanceof Error ? error.message : "操作失败，请重试。";
  anchor.insertAdjacentHTML(
    "afterend",
    `<p class="inline-error" data-inline-error role="alert">${escapeHtml(message)}</p>`,
  );
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
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
