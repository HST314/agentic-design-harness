import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useSearchParams,
} from "react-router-dom";
import type { ReadyResponse, TaskSummary } from "../api/client";
import { api, readinessQuery, taskHistoryQuery } from "../api/queries";
import { Icon } from "../components/Icon";
import { WorkItemDetailsPanel } from "../features/task-board/TaskBoardPage";

const statusLabels: Record<string, string> = {
  AWAITING_START_CONFIRMATION: "等待启动",
  BLOCKED_UNAVAILABLE: "能力不可用",
  CANCELLED: "已取消",
  DRAFT: "草稿",
  FAILED: "失败",
  PARTIAL: "部分完成",
  PLANNED: "已规划",
  RUNNING: "运行中",
  SUCCEEDED: "已完成",
  WAITING_APPROVAL: "等待审批",
};

function taskLabel(task: TaskSummary): string {
  return statusLabels[task.status] ?? task.status;
}

function taskIdFromPath(pathname: string): string | null {
  const match = pathname.match(/^\/tasks\/([^/]+)\/(?:master|board|plan|deliveries|work-items)/);
  if (!match?.[1]) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

function sectionFromPath(pathname: string): string {
  if (pathname === "/settings") return "全局设置";
  if (pathname === "/inbox") return "收件箱";
  if (pathname.includes("/work-items/")) return "专业工作台";
  if (pathname.endsWith("/master")) return "Master 线程";
  if (pathname.endsWith("/board")) return "任务看板";
  if (pathname.endsWith("/plan")) return "任务计划";
  if (pathname.endsWith("/deliveries")) return "任务交付";
  return "创建任务";
}

function disabledAdapterLabel(readiness: ReadyResponse | undefined): string {
  const names = (readiness?.disabled_adapters ?? []).map((name) => (
    name === "image" ? "Image" : name === "ppt" ? "PPT" : name.toUpperCase()
  ));
  return names.length ? names.join("、") : "部分专业能力";
}

function historyGroups(tasks: TaskSummary[], search: string): Array<{ label: string; items: TaskSummary[] }> {
  const needle = search.trim().toLocaleLowerCase("zh-CN");
  const visible = tasks.filter((task) => {
    const matches = !needle || `${task.title} ${task.task_id}`.toLocaleLowerCase("zh-CN").includes(needle);
    return matches && (needle || !task.archived_at);
  });
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startSevenDays = startToday - 6 * 24 * 60 * 60 * 1000;
  const pinned = visible.filter((task) => task.pinned_at && !task.archived_at);
  const unpinned = visible.filter((task) => !task.pinned_at && !task.archived_at);
  const archived = visible.filter((task) => task.archived_at);
  const groups = [
    { label: "置顶", items: pinned },
    { label: "今天", items: unpinned.filter((task) => new Date(task.updated_at).getTime() >= startToday) },
    { label: "近 7 天", items: unpinned.filter((task) => {
      const value = new Date(task.updated_at).getTime();
      return value < startToday && value >= startSevenDays;
    }) },
    { label: "更早", items: unpinned.filter((task) => new Date(task.updated_at).getTime() < startSevenDays) },
    { label: "归档搜索结果", items: archived },
  ];
  return groups.filter((group) => group.items.length > 0);
}

function closeMenu(event: React.MouseEvent<HTMLButtonElement>): void {
  event.currentTarget.closest("details")?.removeAttribute("open");
}

function TaskHistoryRow({
  task,
  busy,
  onRename,
  onPin,
  onArchive,
}: {
  task: TaskSummary;
  busy: boolean;
  onRename: (task: TaskSummary) => void;
  onPin: (task: TaskSummary) => void;
  onArchive: (task: TaskSummary) => void;
}): React.JSX.Element {
  return (
    <div className="workbench-history-row">
      <NavLink
        className="workbench-history-item"
        to={`/tasks/${encodeURIComponent(task.task_id)}/master`}
        title={task.title}
      >
        <span className="workbench-history-item__title">{task.title}</span>
        <span className="workbench-history-item__meta">
          <span>{task.archived_at ? "已归档 · " : ""}{taskLabel(task)}</span>
          <time dateTime={task.updated_at}>{new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(new Date(task.updated_at))}</time>
        </span>
      </NavLink>
      <details className="workbench-history-menu">
        <summary aria-label={`打开 ${task.title} 的任务操作`}><Icon name="more" /></summary>
        <div role="menu" aria-label={`${task.title} 的任务操作`}>
          <button type="button" role="menuitem" disabled={busy} onClick={(event) => { closeMenu(event); onRename(task); }}><Icon name="rename" />重命名</button>
          {!task.archived_at ? <button type="button" role="menuitem" disabled={busy} onClick={(event) => { closeMenu(event); onPin(task); }}><Icon name="pin" />{task.pinned_at ? "取消置顶" : "置顶"}</button> : null}
          <button type="button" role="menuitem" disabled={busy} onClick={(event) => { closeMenu(event); onArchive(task); }}><Icon name="archive" />{task.archived_at ? "恢复" : "归档"}</button>
        </div>
      </details>
    </div>
  );
}

function RenameTaskDialog({
  task,
  busy,
  error,
  onCancel,
  onSubmit,
}: {
  task: TaskSummary;
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onSubmit: (title: string) => void;
}): React.JSX.Element {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState(task.title);
  useEffect(() => {
    dialogRef.current?.showModal();
    return () => dialogRef.current?.close();
  }, []);
  return (
    <dialog ref={dialogRef} className="workbench-rename-dialog" aria-labelledby="rename-task-title" onCancel={(event) => { event.preventDefault(); onCancel(); }}>
      <form onSubmit={(event) => { event.preventDefault(); if (title.trim()) onSubmit(title.trim()); }}>
        <div className="workbench-drawer__header"><div><p className="workbench-eyebrow">展示属性</p><h2 id="rename-task-title">重命名任务</h2></div><button type="button" className="workbench-icon-button" aria-label="关闭重命名窗口" onClick={onCancel}><Icon name="close" /></button></div>
        <label className="workbench-field"><span>任务标题</span><input autoFocus value={title} maxLength={256} onChange={(event) => setTitle(event.currentTarget.value)} /></label>
        {error ? <p className="workbench-inline-error" role="alert">{error}</p> : null}
        <div className="workbench-dialog-actions"><button type="button" className="workbench-secondary-button" onClick={onCancel}>取消</button><button type="submit" className="workbench-primary-button" disabled={busy || !title.trim()}>{busy ? "正在保存…" : "保存标题"}</button></div>
      </form>
    </dialog>
  );
}

function DetailsDrawer({
  drawer,
  readiness,
  taskId,
  target,
  onClose,
}: {
  drawer: string;
  readiness: { data: ReadyResponse | undefined; isError: boolean };
  taskId: string | null;
  target: string | null;
  onClose: () => void;
}): React.JSX.Element {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    return () => {
      if (dialog?.open) dialog.close();
    };
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="workbench-drawer"
      aria-label="工作台详情抽屉"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="workbench-drawer__header">
        <div><p className="workbench-eyebrow">按需详情</p><h2>{drawer === "status" ? "运行状态" : drawer === "work-item" ? "子任务详情" : "任务信息"}</h2></div>
        <button className="workbench-icon-button" type="button" aria-label="关闭详情抽屉" onClick={onClose}><Icon name="close" /></button>
      </div>
      {drawer === "status" ? (
        <div className="workbench-drawer__body">
          <div className="workbench-status-card"><Icon name="status" /><div><strong>{readiness.data?.status === "ready" ? "控制面连接正常" : readiness.data?.status === "degraded" ? "控制面已降级运行" : "正在等待控制面"}</strong><p>{readiness.data?.status === "degraded" ? `${disabledAdapterLabel(readiness.data)} Agent 当前不可用；其他控制面能力保持可用。` : "状态来自 `/readyz`，工作台每 10 秒重新校验。"}</p></div></div>
          <dl className="workbench-definition-list"><div><dt>当前上下文</dt><dd>{taskId ?? "新任务"}</dd></div><div><dt>刷新策略</dt><dd>窗口聚焦时重新校验</dd></div></dl>
        </div>
      ) : drawer === "work-item" && taskId && target ? (
        <div className="workbench-drawer__body workbench-drawer__body--work-item"><WorkItemDetailsPanel taskId={taskId} workItemId={target} /></div>
      ) : (
        <div className="workbench-drawer__body"><p>该抽屉类型将在对应功能阶段接入真实读模型。</p></div>
      )}
    </dialog>
  );
}

export function AppShell(): React.JSX.Element {
  const [collapsed, setCollapsed] = useState(false);
  const [search, setSearch] = useState("");
  const [searchParams, setSearchParams] = useSearchParams();
  const [renameTask, setRenameTask] = useState<TaskSummary | null>(null);
  const location = useLocation();
  const queryClient = useQueryClient();
  const tasks = useQuery(taskHistoryQuery);
  const readiness = useQuery(readinessQuery);
  const taskId = taskIdFromPath(location.pathname);
  const drawer = searchParams.get("drawer");
  const drawerTarget = searchParams.get("target");
  const groups = useMemo(() => historyGroups(tasks.data?.items ?? [], search), [search, tasks.data?.items]);
  const currentTask = taskId ? tasks.data?.items.find((task) => task.task_id === taskId) : undefined;
  const sectionLabel = sectionFromPath(location.pathname);
  const flushMain = taskId !== null || location.pathname === "/tasks/new";
  const presentation = useMutation({
    mutationFn: ({ task, patch }: { task: TaskSummary; patch: { title?: string; pinned?: boolean; archived?: boolean } }) => api.updateTaskPresentation(task.task_id, {
      ...patch,
      envelope: {
        idempotency_key: `presentation_${crypto.randomUUID().replaceAll("-", "")}`,
        actor_type: "human",
        actor_id: "human_operator",
        expected_revision: patch.title === undefined ? task.presentation_revision ?? 0 : task.revision,
      },
    }),
    onSuccess: () => {
      setRenameTask(null);
      void queryClient.invalidateQueries({ queryKey: taskHistoryQuery.queryKey });
    },
  });

  const setDrawer = (value: string | null): void => {
    const next = new URLSearchParams(searchParams);
    if (value) {
      next.set("drawer", value);
      if (value !== "work-item") next.delete("target");
    }
    else {
      next.delete("drawer");
      next.delete("target");
    }
    setSearchParams(next, { replace: true });
  };

  return (
    <>
      <a className="skip-link" href="#workbench-main">跳到主要内容</a>
      <div className={`workbench-shell${collapsed ? " workbench-shell--collapsed" : ""}`}>
        <aside className="workbench-sidebar" aria-label="主任务历史">
          <div className="workbench-brand-row">
            <Link className="workbench-brand" to="/tasks/new" aria-label="返回新任务页">
              <span className="workbench-brand__mark" aria-hidden="true">DW</span>
              <span className="workbench-label"><strong>Design Workbench</strong><small>多智能体控制平面</small></span>
            </Link>
            <button
              className="workbench-icon-button"
              type="button"
              aria-label={collapsed ? "展开主任务历史" : "收起主任务历史"}
              aria-expanded={!collapsed}
              onClick={() => setCollapsed((value) => !value)}
            >
              <Icon name={collapsed ? "menu" : "chevron-left"} />
            </button>
          </div>

          <NavLink className="workbench-new-task" to="/tasks/new">
            <Icon name="plus" /><span className="workbench-label">新任务</span>
          </NavLink>

          <label className="workbench-search">
            <span className="sr-only">搜索主任务</span>
            <Icon name="search" />
            <input
              type="search"
              value={search}
              placeholder="搜索任务"
              onChange={(event) => setSearch(event.currentTarget.value)}
            />
          </label>

          <nav className="workbench-history" aria-label="任务历史">
            {tasks.isPending ? <p className="workbench-sidebar-note" role="status">正在读取任务历史</p> : null}
            {tasks.isError ? <p className="workbench-sidebar-note workbench-sidebar-note--error" role="alert">任务历史暂时不可用</p> : null}
            {!tasks.isPending && !tasks.isError && groups.length === 0 ? (
              <p className="workbench-sidebar-note">没有匹配任务</p>
            ) : null}
            {groups.map((group) => (
              <section className="workbench-history-group" aria-label={group.label} key={group.label}>
                <p className="workbench-history__heading">{group.label}</p>
                {group.items.map((task) => <TaskHistoryRow key={task.task_id} task={task} busy={presentation.isPending} onRename={setRenameTask} onPin={(item) => presentation.mutate({ task: item, patch: { pinned: !item.pinned_at } })} onArchive={(item) => presentation.mutate({ task: item, patch: { archived: !item.archived_at } })} />)}
              </section>
            ))}
            {presentation.isError && !renameTask ? <p className="workbench-sidebar-note workbench-sidebar-note--error" role="alert">{presentation.error.message}</p> : null}
          </nav>

          <nav className="workbench-utilities" aria-label="工作台工具">
            <NavLink to="/inbox"><Icon name="inbox" /><span className="workbench-label">收件箱</span></NavLink>
            <NavLink to="/settings"><Icon name="settings" /><span className="workbench-label">全局设置</span></NavLink>
          </nav>
        </aside>

        <div className="workbench-center">
          <header className="workbench-topbar">
            <div className="workbench-topbar__lead">
              <p className="workbench-context">
                <span className="workbench-context__title">{currentTask ? currentTask.title : taskId ? "当前任务" : sectionLabel}</span>
                {currentTask ? (
                  <span className={`master-task-status master-task-status--${currentTask.status.toLowerCase()}`}>
                    <span aria-hidden="true" />{taskLabel(currentTask)}
                  </span>
                ) : null}
              </p>
            </div>
            <div className="workbench-topbar__actions">
              <span
                className={`workbench-service${readiness.data?.status === "ready" ? " workbench-service--ready" : ""}`}
                role="status"
                aria-live="polite"
              >
                <span aria-hidden="true" />
                {readiness.data?.status === "ready" ? "服务就绪" : readiness.data?.status === "degraded" ? `服务降级 · ${disabledAdapterLabel(readiness.data)} 不可用` : readiness.isError ? "服务不可用" : "检查服务"}
              </span>
              <button
                className="workbench-icon-button"
                type="button"
                aria-label="打开状态抽屉"
                aria-expanded={drawer === "status"}
                onClick={() => setDrawer(drawer === "status" ? null : "status")}
              >
                <Icon name="panel-right" />
              </button>
            </div>
          </header>
          <p className="workbench-width-notice" role="note">工作台当前保证 1280px 及以上桌面宽度。</p>
          <main id="workbench-main" className={`workbench-main${flushMain ? " workbench-main--flush" : ""}`} tabIndex={-1}>
            <Outlet />
          </main>
        </div>

        {drawer ? <DetailsDrawer drawer={drawer} readiness={readiness} taskId={taskId} target={drawerTarget} onClose={() => setDrawer(null)} /> : null}
        {renameTask ? <RenameTaskDialog task={renameTask} busy={presentation.isPending} error={presentation.isError ? presentation.error.message : null} onCancel={() => { presentation.reset(); setRenameTask(null); }} onSubmit={(title) => presentation.mutate({ task: renameTask, patch: { title } })} /> : null}
      </div>
    </>
  );
}
