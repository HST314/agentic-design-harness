import "./styles.css";
import { ApiClient } from "./api/client";
import { currentRoute, navigate, routePath, type RouteName } from "./router";

function requireAppRoot(): HTMLDivElement {
  const element = document.querySelector<HTMLDivElement>("#app");
  if (!element) throw new Error("Application root is missing.");
  return element;
}

const root = requireAppRoot();

const api = new ApiClient();
const navigation: Array<{ route: RouteName; label: string; icon: string }> = [
  { route: "tasks", label: "任务", icon: "layers" },
  { route: "inbox", label: "收件箱", icon: "inbox" },
  { route: "settings", label: "设置", icon: "settings" },
];

const pageCopy: Record<RouteName, { eyebrow: string; title: string; description: string }> = {
  tasks: {
    eyebrow: "控制平面",
    title: "主任务",
    description: "统一查看阶段、实例与需要处理的运行状态。",
  },
  inbox: {
    eyebrow: "FIFO 队列",
    title: "收件箱",
    description: "审批与运行通知将在后续工作包接入。",
  },
  settings: {
    eyebrow: "运行配置",
    title: "设置",
    description: "全局配置与凭据只通过受控 API 管理。",
  },
};

function icon(name: string): string {
  const paths: Record<string, string> = {
    layers: '<path d="M12 2 2 7l10 5 10-5-10-5Zm-8.5 9L12 15.25 20.5 11M3.5 15 12 19.25 20.5 15"/>',
    inbox: '<path d="M4 4h16l2 11h-6l-2 3h-4l-2-3H2L4 4Z"/>',
    settings: '<path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4v-.08A1.7 1.7 0 0 0 8.95 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.58 15 1.7 1.7 0 0 0 3 14H3v-4h.08A1.7 1.7 0 0 0 4.6 8.95a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.58 1.7 1.7 0 0 0 10 3h4v.08a1.7 1.7 0 0 0 1.03 1.52 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.13.4.5.77 1.52 1H21v4h-.08A1.7 1.7 0 0 0 19.4 15Z"/>',
  };
  return `<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[name] ?? ""}</svg>`;
}

function render(): void {
  const route = currentRoute();
  const copy = pageCopy[route];
  root.innerHTML = `
    <div class="shell">
      <aside class="sidebar" aria-label="主导航">
        <a class="brand" href="${routePath("tasks")}" data-route="tasks" aria-label="返回任务面板">
          <span class="brand-mark" aria-hidden="true">DH</span>
          <span><strong>Design Harness</strong><small>Control plane</small></span>
        </a>
        <nav aria-label="主导航">
          ${navigation
            .map(
              (item) => `<a href="${routePath(item.route)}" data-route="${item.route}" ${
                item.route === route ? 'aria-current="page"' : ""
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
        <section class="content" aria-labelledby="empty-title">
          <div class="empty-card">
            <span class="empty-icon" aria-hidden="true">${icon(route === "tasks" ? "layers" : route)}</span>
            <p class="eyebrow">P1 控制面底座</p>
            <h2 id="empty-title">${copy.title}基础页面已就绪</h2>
            <p>${copy.description}</p>
            <p class="boundary-copy">当前壳只承载 Harness 控制功能；专业设计流程通过实例的“打开工作台”深链进入。</p>
          </div>
        </section>
      </main>
    </div>`;
  root.querySelectorAll<HTMLElement>("[data-route]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const target = link.dataset.route as RouteName | undefined;
      if (target) navigate(target);
    });
  });
  void refreshServiceState();
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

window.addEventListener("popstate", () => {
  render();
  root.querySelector<HTMLElement>("#main-content")?.focus();
});
render();
