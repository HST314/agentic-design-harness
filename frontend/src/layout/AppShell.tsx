import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useSearchParams,
} from "react-router-dom";
import type { ReadyResponse, TaskSummary } from "../api/client";
import { readinessQuery, taskHistoryQuery } from "../api/queries";
import { Icon } from "../components/Icon";

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
  const match = pathname.match(/^\/tasks\/([^/]+)\/(?:master|board|plan|work-items)/);
  if (!match?.[1]) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

function DetailsDrawer({
  drawer,
  readiness,
  taskId,
  onClose,
}: {
  drawer: string;
  readiness: { data: ReadyResponse | undefined; isError: boolean };
  taskId: string | null;
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
        <div><p className="workbench-eyebrow">按需详情</p><h2>{drawer === "status" ? "运行状态" : "任务信息"}</h2></div>
        <button className="workbench-icon-button" type="button" aria-label="关闭详情抽屉" onClick={onClose}><Icon name="close" /></button>
      </div>
      {drawer === "status" ? (
        <div className="workbench-drawer__body">
          <div className="workbench-status-card"><Icon name="status" /><div><strong>{readiness.data?.status === "ready" ? "控制面连接正常" : "正在等待控制面"}</strong><p>状态来自 `/readyz`，工作台每 10 秒重新校验。</p></div></div>
          <dl className="workbench-definition-list"><div><dt>当前上下文</dt><dd>{taskId ?? "新任务"}</dd></div><div><dt>刷新策略</dt><dd>窗口聚焦时重新校验</dd></div></dl>
        </div>
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
  const location = useLocation();
  const tasks = useQuery(taskHistoryQuery);
  const readiness = useQuery(readinessQuery);
  const taskId = taskIdFromPath(location.pathname);
  const drawer = searchParams.get("drawer");
  const filteredTasks = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase("zh-CN");
    if (!needle) return tasks.data?.items ?? [];
    return (tasks.data?.items ?? []).filter((task) =>
      `${task.title} ${task.task_id}`.toLocaleLowerCase("zh-CN").includes(needle),
    );
  }, [search, tasks.data?.items]);

  const setDrawer = (value: string | null): void => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set("drawer", value);
    else next.delete("drawer");
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
            <p className="workbench-history__heading">最近更新</p>
            {tasks.isPending ? <p className="workbench-sidebar-note" role="status">正在读取任务历史</p> : null}
            {tasks.isError ? <p className="workbench-sidebar-note workbench-sidebar-note--error" role="alert">任务历史暂时不可用</p> : null}
            {!tasks.isPending && !tasks.isError && filteredTasks.length === 0 ? (
              <p className="workbench-sidebar-note">没有匹配任务</p>
            ) : null}
            {filteredTasks.map((task) => (
              <NavLink
                className="workbench-history-item"
                key={task.task_id}
                to={`/tasks/${encodeURIComponent(task.task_id)}/master`}
                title={task.title}
              >
                <span className="workbench-history-item__title">{task.title}</span>
                <span className="workbench-history-item__meta">
                  <span>{taskLabel(task)}</span>
                  <time dateTime={task.updated_at}>{new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(new Date(task.updated_at))}</time>
                </span>
              </NavLink>
            ))}
          </nav>

          <nav className="workbench-utilities" aria-label="工作台工具">
            <Link to="/tasks"><Icon name="history" /><span className="workbench-label">已验收任务面板</span></Link>
            <Link to="/inbox"><Icon name="inbox" /><span className="workbench-label">收件箱</span></Link>
            <Link to="/settings"><Icon name="settings" /><span className="workbench-label">设置</span></Link>
          </nav>
        </aside>

        <div className="workbench-center">
          <header className="workbench-topbar">
            <div>
              <p className="workbench-eyebrow">工作台框架</p>
              <p className="workbench-context">{taskId ? `任务 ${taskId}` : "创建任务"}</p>
            </div>
            <div className="workbench-topbar__actions">
              <span
                className={`workbench-service${readiness.data?.status === "ready" ? " workbench-service--ready" : ""}`}
                role="status"
                aria-live="polite"
              >
                <span aria-hidden="true" />
                {readiness.data?.status === "ready" ? "服务就绪" : readiness.isError ? "服务不可用" : "检查服务"}
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
          <main id="workbench-main" className="workbench-main" tabIndex={-1}>
            <Outlet />
          </main>
        </div>

        {drawer ? <DetailsDrawer drawer={drawer} readiness={readiness} taskId={taskId} onClose={() => setDrawer(null)} /> : null}
      </div>
    </>
  );
}
