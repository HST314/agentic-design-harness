import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, workItemDetailQuery, workItemsQuery } from "../../api/queries";
import type { ContractWorkItemProjection } from "../../api/generated-contracts";
import { Icon } from "../../components/Icon";
import { TaskTabs } from "../master-thread/MasterThreadPage";

type BusinessStatus = ContractWorkItemProjection["business_status"];
type EditableBusinessStatus = Exclude<BusinessStatus, "EXCEPTION">;
type View = "board" | "plan";

const editableStatuses: EditableBusinessStatus[] = [
  "TODO",
  "RUNNING",
  "WAITING_APPROVAL",
  "COMPLETED",
];

const businessStatusLabel: Record<BusinessStatus, string> = {
  TODO: "待办",
  RUNNING: "运行中",
  WAITING_APPROVAL: "待审批",
  COMPLETED: "已完成",
  EXCEPTION: "异常",
};

const rawStatusLabel: Record<string, string> = {
  ARCHIVED: "已归档",
  AWAITING_START_CONFIRMATION: "等待启动",
  BLOCKED_UNAVAILABLE: "能力不可用",
  CANCELLED: "已取消",
  CRASHED: "运行中断",
  CREATED: "等待规划",
  FAILED: "失败",
  FAILED_TO_START: "启动失败",
  PENDING: "等待前置",
  READY: "待启动",
  RUNNING: "运行中",
  SKIPPED: "已跳过",
  STARTING: "启动中",
  SUCCEEDED: "已完成",
  SUPERSEDED: "已替换",
  UNAVAILABLE: "不可用",
  WAITING_APPROVAL: "等待审批",
};

const approvalLabel: Record<string, string> = {
  ASSET_DELETION: "资源删除",
  BUDGET_OVERRIDE: "重试预算",
  WORKFLOW: "工作流推进",
};

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function agentLabel(agentType: "general" | "image" | "ppt"): string {
  return agentType === "general" ? "通用" : agentType === "image" ? "图片" : "PPT";
}

function detailsSearch(item: ContractWorkItemProjection): string {
  const params = new URLSearchParams({ drawer: "work-item", target: item.work_item_id });
  return `?${params.toString()}`;
}

export function workbenchPath(item: ContractWorkItemProjection): string {
  return `/tasks/${encodeURIComponent(item.task_id)}/work-items/${encodeURIComponent(item.work_item_id)}`;
}

export function canEnterWorkbench(item: ContractWorkItemProjection): boolean {
  if (item.agent_type !== "ppt") return true;
  return ["STARTING", "RUNNING", "WAITING_APPROVAL", "SUCCEEDED"].includes(
    item.current_instance?.status ?? "",
  );
}

function WorkItemCard({
  item,
  compact = false,
  statusPending = false,
  statusDisabled = false,
  statusError,
  onStatusChange,
}: {
  item: ContractWorkItemProjection;
  compact?: boolean;
  statusPending?: boolean;
  statusDisabled?: boolean;
  statusError?: string;
  onStatusChange?: (item: ContractWorkItemProjection, status: EditableBusinessStatus) => void;
}): React.JSX.Element {
  const dependencyText = item.depends_on.length
    ? `依赖 ${item.depends_on.length} 项`
    : "无前置依赖";
  const attention = item.pending_approvals.length
    ? `${item.pending_approvals.length} 项待审批`
    : item.alerts[0]?.message;
  const canEnter = canEnterWorkbench(item);
  const statusValue: EditableBusinessStatus = item.business_status === "EXCEPTION"
    ? "TODO"
    : item.business_status;
  const cardBody = (
    <>
      <div>
        <h3>{item.title}</h3>
      </div>
      <dl className="task-card__facts">
        <div><dt>阶段</dt><dd>第 {item.stage.position} 阶段</dd></div>
        <div><dt>依赖</dt><dd>{dependencyText}</dd></div>
        <div><dt>执行</dt><dd>{item.instance_ids.length}</dd></div>
        <div><dt>重试</dt><dd>{item.attempts.length}</dd></div>
      </dl>
      {attention ? <p className="task-card__attention"><Icon name="status" />{attention}</p> : null}
      {!canEnter ? <p className="task-card__entry-hint">请从 Master 启动，启动成功后可进入 PPT 工作台</p> : null}
    </>
  );
  return (
    <article
      className={`task-card task-card--${item.business_status.toLowerCase()}${compact ? " task-card--compact" : ""}`}
    >
      <div className="task-card__topline">
        <span className="task-card__agent">{agentLabel(item.agent_type)}</span>
        <label className="task-card__status-control">
          <span className="sr-only">设置 {item.title} 状态</span>
          <select
            aria-label={`设置 ${item.title} 状态`}
            disabled={statusDisabled || !item.current_instance || !onStatusChange}
            value={statusValue}
            onChange={(event) => onStatusChange?.(
              item,
              event.currentTarget.value as EditableBusinessStatus,
            )}
          >
            {editableStatuses.map((status) => (
              <option value={status} key={status}>{businessStatusLabel[status]}</option>
            ))}
          </select>
        </label>
      </div>
      {canEnter ? (
        <Link
          className="task-card__entry"
          to={workbenchPath(item)}
          aria-label={`${item.title}，${businessStatusLabel[item.business_status]}，进入 ${agentLabel(item.agent_type)}工作台`}
        >
          {cardBody}
        </Link>
      ) : (
        <div className="task-card__entry task-card__entry--disabled" aria-label={`${item.title}，PPT 工作台尚未启动`}>
          {cardBody}
        </div>
      )}
      <footer>
        <span>{item.delivery_count ? `${item.delivery_count} 个交付物` : "暂无交付"}</span>
        <time dateTime={item.updated_at}>{formatTime(item.updated_at)}</time>
        <Link className="task-card__details" to={{ search: detailsSearch(item) }}>详情</Link>
      </footer>
      {statusPending ? <p className="task-card__status-feedback" role="status">正在更新状态…</p> : null}
      {statusError ? <p className="task-card__status-feedback task-card__status-feedback--error" role="alert">{statusError}</p> : null}
    </article>
  );
}

function EmptyColumn({ text }: { text: string }): React.JSX.Element {
  return <p className="task-board__empty">{text}</p>;
}

export function BoardView({ items, ...cardActions }: {
  items: ContractWorkItemProjection[];
  statusPendingId?: string;
  statusErrorId?: string;
  statusError?: string;
  onStatusChange?: (item: ContractWorkItemProjection, status: EditableBusinessStatus) => void;
}): React.JSX.Element {
  const columns: Array<{
    id: string;
    label: string;
    statuses: BusinessStatus[];
  }> = [
    { id: "todo", label: "待办", statuses: ["TODO", "EXCEPTION"] },
    { id: "running", label: "运行中", statuses: ["RUNNING"] },
    { id: "approval", label: "待审批", statuses: ["WAITING_APPROVAL"] },
    { id: "completed", label: "已完成", statuses: ["COMPLETED"] },
  ];
  return (
    <div className="task-board" aria-label="逻辑子任务状态看板">
      {columns.map((column) => {
        const cards = items.filter((item) => column.statuses.includes(item.business_status));
        return (
          <section className="task-board__column" aria-labelledby={`board-column-${column.id}`} key={column.id}>
            <header>
              <div><h2 id={`board-column-${column.id}`}>{column.label}</h2><span>{cards.length}</span></div>
            </header>
            <div className="task-board__cards">
              {cards.length ? cards.map((item) => (
                <WorkItemCard
                  item={item}
                  key={item.work_item_id}
                  statusPending={cardActions.statusPendingId === item.work_item_id}
                  statusDisabled={Boolean(cardActions.statusPendingId)}
                  statusError={cardActions.statusErrorId === item.work_item_id ? cardActions.statusError : undefined}
                  onStatusChange={cardActions.onStatusChange}
                />
              )) : <EmptyColumn text="当前没有子任务" />}
            </div>
          </section>
        );
      })}
    </div>
  );
}

const planColumns: Array<{ id: "image" | "ppt" | "general"; label: string }> = [
  { id: "image", label: "Image" },
  { id: "ppt", label: "PPT" },
  { id: "general", label: "方案" },
];

export function PlanView({ items, allItems, ...cardActions }: {
  items: ContractWorkItemProjection[];
  allItems?: ContractWorkItemProjection[];
  statusPendingId?: string;
  statusErrorId?: string;
  statusError?: string;
  onStatusChange?: (item: ContractWorkItemProjection, status: EditableBusinessStatus) => void;
}): React.JSX.Element {
  // Cards keep the projection's plan order: it is immutable across status
  // updates, unlike updated_at which shifts whenever a card changes state.
  const columns = planColumns.map((column) => ({
    ...column,
    cards: items.filter((item) => item.agent_type === column.id),
  }));
  // The current stage reflects the whole plan, not the filtered subset —
  // otherwise hiding a column would fake a later stage.
  const stageSource = allItems ?? items;
  const activeColumns = planColumns
    .map((column) => ({
      id: column.id,
      cards: stageSource.filter((item) => item.agent_type === column.id),
    }))
    .filter((column) => column.cards.length > 0);
  const currentColumn = activeColumns.find((column) => (
    column.cards.some((item) => item.business_status !== "COMPLETED")
  )) ?? activeColumns[activeColumns.length - 1];
  return (
    <div className="task-board task-board--plan" aria-label="任务计划看板">
      {columns.map((column) => {
        const isCurrent = currentColumn?.id === column.id;
        return (
          <section
            className={`task-board__column${isCurrent ? " task-board__column--current" : ""}`}
            aria-labelledby={`plan-column-${column.id}`}
            key={column.id}
          >
            <header>
              <div><h2 id={`plan-column-${column.id}`}>{column.label}</h2><span>{column.cards.length}</span></div>
              {isCurrent ? <span className="task-board__current-badge">当前阶段</span> : null}
            </header>
            <div className="task-board__cards">
              {column.cards.length ? column.cards.map((item) => (
                <WorkItemCard
                  item={item}
                  key={item.work_item_id}
                  statusPending={cardActions.statusPendingId === item.work_item_id}
                  statusDisabled={Boolean(cardActions.statusPendingId)}
                  statusError={cardActions.statusErrorId === item.work_item_id ? cardActions.statusError : undefined}
                  onStatusChange={cardActions.onStatusChange}
                />
              )) : <EmptyColumn text="当前没有子任务" />}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function TaskProjectionPage({ view }: { view: View }): React.JSX.Element {
  const { taskId = "" } = useParams();
  const queryClient = useQueryClient();
  const projection = useQuery(workItemsQuery(taskId));
  const [agent, setAgent] = useState<"all" | "general" | "image" | "ppt">("all");
  const [stageId, setStageId] = useState("all");
  const filteredItems = useMemo(() => (projection.data?.items ?? []).filter((item) => (
    (agent === "all" || item.agent_type === agent)
    && (stageId === "all" || item.stage.stage_id === stageId)
  )), [agent, projection.data?.items, stageId]);
  const title = view === "board" ? "当前任务看板" : "任务计划";
  const updateStatus = useMutation({
    mutationFn: ({ item, status }: {
      item: ContractWorkItemProjection;
      status: EditableBusinessStatus;
    }) => {
      const taskRevision = projection.data?.task_revision;
      if (taskRevision === undefined) throw new Error("当前任务修订尚未就绪，请重新读取看板。");
      const suffix = typeof crypto.randomUUID === "function"
        ? crypto.randomUUID().replaceAll("-", "")
        : `${Date.now()}${Math.random().toString(16).slice(2)}`;
      return api.updateWorkItemStatus(taskId, item.work_item_id, status, {
        idempotency_key: `set_work_item_status_${suffix}`.slice(0, 128),
        actor_type: "human",
        actor_id: "human_operator",
        expected_revision: taskRevision,
      });
    },
    onSuccess: (response) => {
      queryClient.setQueryData(workItemsQuery(taskId).queryKey, response);
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: workItemsQuery(taskId).queryKey });
    },
  });
  const cardActions = {
    statusPendingId: updateStatus.isPending ? updateStatus.variables?.item.work_item_id : undefined,
    statusErrorId: updateStatus.isError ? updateStatus.variables?.item.work_item_id : undefined,
    statusError: updateStatus.isError ? updateStatus.error.message : undefined,
    onStatusChange: (item: ContractWorkItemProjection, status: EditableBusinessStatus) => {
      updateStatus.reset();
      updateStatus.mutate({ item, status });
    },
  };
  return (
    <section className={`workbench-page task-projection task-projection--${view}`} aria-label={title}>
      <TaskTabs taskId={taskId} />
      {projection.isPending ? <div className="task-projection__loading" role="status">正在读取子任务…</div> : null}
      {projection.isError ? <div className="task-projection__error" role="alert"><strong>无法读取子任务投影</strong><span>{projection.error.message}</span><button type="button" className="workbench-secondary-button" onClick={() => void projection.refetch()}>重新读取</button></div> : null}
      {projection.data ? (
        <>
          <div className="task-projection__toolbar" aria-label="看板筛选">
            <div className="task-projection__counts" aria-label="状态汇总">
              {editableStatuses.map((status) => (
                <span key={status}><i className={`task-dot task-dot--${status.toLowerCase()}`} aria-hidden="true" />{businessStatusLabel[status]} <strong>{status === "TODO" ? projection.data.summary.TODO + projection.data.summary.EXCEPTION : projection.data.summary[status]}</strong></span>
              ))}
            </div>
            <div className="task-projection__filters">
              <label><span>创作类型</span><select value={agent} onChange={(event) => setAgent(event.currentTarget.value as typeof agent)}><option value="all">全部</option><option value="general">通用</option><option value="image">图片</option><option value="ppt">PPT</option></select></label>
              <label><span>执行阶段</span><select value={stageId} onChange={(event) => setStageId(event.currentTarget.value)}><option value="all">全部</option>{projection.data.stages.map((stage) => <option value={stage.stage_id} key={stage.stage_id}>第 {stage.position} 阶段 · {agentLabel(stage.type)}</option>)}</select></label>
            </div>
          </div>
          {projection.data.items.length === 0 ? (
            <div className="task-projection__empty"><Icon name="plan" /><h2>计划尚未生成逻辑子任务</h2><p>在 Master 中确认计划后，看板与阶段依赖会自动出现在这里。</p><Link className="workbench-primary-button" to={`/tasks/${encodeURIComponent(taskId)}/master`}>返回 Master</Link></div>
          ) : view === "board" ? (
            <BoardView items={filteredItems} {...cardActions} />
          ) : (
            <PlanView items={filteredItems} allItems={projection.data.items} {...cardActions} />
          )}
        </>
      ) : null}
    </section>
  );
}

function WorkItemDetailsBody({ taskId, workItemId, standalone = false }: {
  taskId: string;
  workItemId: string;
  standalone?: boolean;
}): React.JSX.Element {
  const detail = useQuery(workItemDetailQuery(taskId, workItemId));
  if (detail.isPending) return <p className="work-item-detail__state" role="status">正在读取逻辑子任务详情…</p>;
  if (detail.isError || !detail.data) return <div className="work-item-detail__state" role="alert"><strong>无法读取详情</strong><p>{detail.error?.message}</p><button className="workbench-secondary-button" type="button" onClick={() => void detail.refetch()}>重新读取</button></div>;
  const item = detail.data.item;
  return (
    <div className="work-item-detail">
      <div className="work-item-detail__identity">
        <div><span className="task-card__agent">{agentLabel(item.agent_type)}</span><span>{item.required ? "必需" : "可选"}</span></div>
        <h2>{item.title}</h2>
        <span className={`task-status task-status--${item.business_status.toLowerCase()}`}><span aria-hidden="true" />{businessStatusLabel[item.business_status]}</span>
      </div>
      <dl className="workbench-definition-list">
        <div><dt>当前进度</dt><dd>{rawStatusLabel[item.raw_status] ?? "处理中"}</dd></div>
        <div><dt>执行阶段</dt><dd>第 {item.stage.position} 阶段 · {agentLabel(item.stage.type)}</dd></div>
        <div><dt>阶段依赖</dt><dd>{item.stage.depends_on.length ? `${item.stage.depends_on.length} 个前置阶段` : "无"}</dd></div>
        <div><dt>子任务依赖</dt><dd>{item.depends_on.length ? `${item.depends_on.length} 项前置任务` : "无"}</dd></div>
        <div><dt>专业工作台</dt><dd>{item.current_instance ? "已创建" : "未创建"}</dd></div>
        <div><dt>工作台状态</dt><dd>{item.current_instance ? `${rawStatusLabel[item.current_instance.status] ?? "处理中"} · ${item.current_instance.approval_mode === "human" ? "人工审批" : "统筹助手审批"}` : "无"}</dd></div>
        <div><dt>执行记录</dt><dd>{item.instance_ids.length} 次执行 · {item.attempts.length} 次自动重试</dd></div>
        <div><dt>已验证交付</dt><dd>{item.delivery_count} 个</dd></div>
        <div><dt>最近变化</dt><dd><time dateTime={item.updated_at}>{formatTime(item.updated_at)}</time></dd></div>
      </dl>
      {item.pending_approvals.length ? (
        <section className="work-item-detail__section" aria-labelledby="work-item-approvals"><h3 id="work-item-approvals">待处理审批</h3><ul>{item.pending_approvals.map((approval) => <li key={approval.approval_id}><Icon name="status" /><span><strong>{approvalLabel[approval.kind] ?? "任务审批"}</strong><small>{approval.owner === "human" ? "等待人工处理" : "等待统筹助手处理"}</small></span></li>)}</ul></section>
      ) : null}
      {item.attempts.length ? (
        <section className="work-item-detail__section" aria-labelledby="work-item-attempts"><h3 id="work-item-attempts">重试记录</h3><ol>{item.attempts.map((attempt, index) => <li key={attempt.attempt_id}><span>第 {index + 1} 次重试</span><span>{rawStatusLabel[attempt.status] ?? "处理中"}</span></li>)}</ol></section>
      ) : null}
      {item.alerts.length ? (
        <section className="work-item-detail__alerts" aria-label="子任务提醒">{item.alerts.map((alert) => <div className={`work-item-alert work-item-alert--${alert.severity}`} key={`${alert.code}-${alert.message}`}><Icon name="status" /><span>{alert.message}</span></div>)}</section>
      ) : null}
      {standalone ? <Link className="workbench-secondary-button" to={`/tasks/${encodeURIComponent(taskId)}/board`}>返回看板</Link> : <Link className="work-item-detail__deep-link" to={`/tasks/${encodeURIComponent(taskId)}/work-items/${encodeURIComponent(workItemId)}`}>打开完整详情</Link>}
    </div>
  );
}

export function WorkItemDetailsPanel({ taskId, workItemId }: {
  taskId: string;
  workItemId: string;
}): React.JSX.Element {
  return <WorkItemDetailsBody taskId={taskId} workItemId={workItemId} />;
}

export function TaskBoardPage(): React.JSX.Element {
  return <TaskProjectionPage view="board" />;
}

export function TaskPlanPage(): React.JSX.Element {
  return <TaskProjectionPage view="plan" />;
}
