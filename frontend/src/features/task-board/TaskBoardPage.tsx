import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import type { WorkItemStageProjection } from "../../api/client";
import { workItemDetailQuery, workItemsQuery } from "../../api/queries";
import type { ContractWorkItemProjection } from "../../api/generated-contracts";
import { Icon } from "../../components/Icon";
import { TaskTabs } from "../master-thread/MasterThreadPage";

type BusinessStatus = ContractWorkItemProjection["business_status"];
type View = "board" | "plan";

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

function shortId(value: string): string {
  return value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-5)}` : value;
}

function agentLabel(agentType: "image" | "ppt"): string {
  return agentType === "image" ? "Image" : "PPT";
}

function detailsSearch(item: ContractWorkItemProjection): string {
  const params = new URLSearchParams({ drawer: "work-item", target: item.work_item_id });
  return `?${params.toString()}`;
}

function WorkItemCard({ item, compact = false }: {
  item: ContractWorkItemProjection;
  compact?: boolean;
}): React.JSX.Element {
  const dependencyText = item.depends_on.length
    ? `依赖 ${item.depends_on.length} 项`
    : "无前置依赖";
  const attention = item.pending_approvals.length
    ? `${item.pending_approvals.length} 项待审批`
    : item.alerts[0]?.message;
  return (
    <article
      className={`task-card task-card--${item.business_status.toLowerCase()}${compact ? " task-card--compact" : ""}`}
    >
      <Link
        className="task-card__entry"
        to={`/tasks/${encodeURIComponent(item.task_id)}/work-items/${encodeURIComponent(item.work_item_id)}`}
        aria-label={`${item.title}，${businessStatusLabel[item.business_status]}，${item.agent_type === "image" ? "进入 Image 工作台" : "查看能力边界"}`}
      >
        <div className="task-card__topline">
          <span className="task-card__agent">{agentLabel(item.agent_type)}</span>
          <span className={`task-status task-status--${item.business_status.toLowerCase()}`}>
            <span aria-hidden="true" />{rawStatusLabel[item.raw_status] ?? item.raw_status}
          </span>
        </div>
        <div>
          <h3>{item.title}</h3>
          <code title={item.work_item_id}>{shortId(item.work_item_id)}</code>
        </div>
        <dl className="task-card__facts">
          <div><dt>阶段</dt><dd>S{item.stage.position}</dd></div>
          <div><dt>依赖</dt><dd>{dependencyText}</dd></div>
          <div><dt>实例</dt><dd>{item.instance_ids.length}</dd></div>
          <div><dt>重试</dt><dd>{item.attempts.length}</dd></div>
        </dl>
        {attention ? <p className="task-card__attention"><Icon name="status" />{attention}</p> : null}
      </Link>
      <footer>
        <span>{item.delivery_count ? `${item.delivery_count} 个交付物` : "暂无交付"}</span>
        <time dateTime={item.updated_at}>{formatTime(item.updated_at)}</time>
        <Link className="task-card__details" to={{ search: detailsSearch(item) }}>详情</Link>
      </footer>
    </article>
  );
}

function EmptyColumn({ text }: { text: string }): React.JSX.Element {
  return <p className="task-board__empty">{text}</p>;
}

export function BoardView({ items }: { items: ContractWorkItemProjection[] }): React.JSX.Element {
  const [terminalFilter, setTerminalFilter] = useState<"all" | "completed" | "exception">("all");
  const columns: Array<{
    id: string;
    label: string;
    statuses: BusinessStatus[];
  }> = [
    { id: "todo", label: "待办", statuses: ["TODO"] },
    { id: "running", label: "运行中", statuses: ["RUNNING"] },
    { id: "approval", label: "待审批", statuses: ["WAITING_APPROVAL"] },
    {
      id: "ended",
      label: "已结束",
      statuses: terminalFilter === "completed"
        ? ["COMPLETED"]
        : terminalFilter === "exception"
          ? ["EXCEPTION"]
          : ["COMPLETED", "EXCEPTION"],
    },
  ];
  return (
    <div className="task-board" aria-label="逻辑子任务状态看板">
      {columns.map((column) => {
        const cards = items.filter((item) => column.statuses.includes(item.business_status));
        return (
          <section className="task-board__column" aria-labelledby={`board-column-${column.id}`} key={column.id}>
            <header>
              <div><h2 id={`board-column-${column.id}`}>{column.label}</h2><span>{cards.length}</span></div>
              {column.id === "ended" ? (
                <label className="task-board__terminal-filter">
                  <span className="sr-only">终态筛选</span>
                  <select value={terminalFilter} onChange={(event) => setTerminalFilter(event.currentTarget.value as typeof terminalFilter)}>
                    <option value="all">全部终态</option>
                    <option value="completed">已完成</option>
                    <option value="exception">异常</option>
                  </select>
                </label>
              ) : null}
            </header>
            <div className="task-board__cards">
              {cards.length ? cards.map((item) => <WorkItemCard item={item} key={item.work_item_id} />) : <EmptyColumn text="当前没有子任务" />}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function StageDependency({ stage, stages }: {
  stage: WorkItemStageProjection;
  stages: WorkItemStageProjection[];
}): React.JSX.Element {
  const stageById = new Map(stages.map((item) => [item.stage_id, item]));
  if (!stage.depends_on.length) return <span>起始阶段 · 无前置依赖</span>;
  return (
    <span>
      依赖 {stage.depends_on.map((stageId) => {
        const dependency = stageById.get(stageId);
        return dependency ? `S${dependency.position} ${agentLabel(dependency.type)}` : stageId;
      }).join("、")}
    </span>
  );
}

function PlanView({ stages, items }: {
  stages: WorkItemStageProjection[];
  items: ContractWorkItemProjection[];
}): React.JSX.Element {
  return (
    <div className="task-plan-flow" aria-label="阶段依赖计划">
      {stages.map((stage, index) => {
        const stageItems = items.filter((item) => item.stage.stage_id === stage.stage_id);
        return (
          <section className="task-plan-stage" aria-labelledby={`plan-stage-${stage.stage_id}`} key={stage.stage_id}>
            {index > 0 ? <div className="task-plan-stage__connector" aria-hidden="true"><span /></div> : null}
            <header>
              <div className="task-plan-stage__marker" aria-hidden="true">{stage.position}</div>
              <div>
                <p className="workbench-eyebrow">Stage {stage.position}</p>
                <h2 id={`plan-stage-${stage.stage_id}`}>{agentLabel(stage.type)} 设计阶段</h2>
              </div>
              <span className={`task-stage-status${stage.available ? "" : " task-stage-status--unavailable"}`}>
                {stage.available ? rawStatusLabel[stage.status] ?? stage.status : "能力未接入"}
              </span>
            </header>
            <div className="task-plan-stage__meta">
              <strong>{stage.required ? "必需阶段" : "可选阶段"}</strong>
              <StageDependency stage={stage} stages={stages} />
            </div>
            {!stage.available ? (
              <div className="task-plan-stage__boundary" role="note">
                <Icon name="status" /><span><strong>{agentLabel(stage.type)} 能力未接入</strong>该阶段保留真实占位，不会进入伪工作台或被标记成功。</span>
              </div>
            ) : null}
            <div className="task-plan-stage__items">
              {stageItems.length ? stageItems.map((item) => <WorkItemCard compact item={item} key={item.work_item_id} />) : <EmptyColumn text="该阶段暂无逻辑子任务" />}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function TaskProjectionPage({ view }: { view: View }): React.JSX.Element {
  const { taskId = "" } = useParams();
  const projection = useQuery(workItemsQuery(taskId));
  const [agent, setAgent] = useState<"all" | "image" | "ppt">("all");
  const [stageId, setStageId] = useState("all");
  const filteredItems = useMemo(() => (projection.data?.items ?? []).filter((item) => (
    (agent === "all" || item.agent_type === agent)
    && (stageId === "all" || item.stage.stage_id === stageId)
  )), [agent, projection.data?.items, stageId]);
  const title = view === "board" ? "当前任务看板" : "任务计划";
  const description = view === "board"
    ? "按业务状态扫描逻辑子任务；运行状态由系统投影，不能拖拽修改。"
    : "按 Stage 顺序查看依赖、必需性、Agent 可用性与同一逻辑子任务入口。";
  return (
    <section className={`workbench-page task-projection task-projection--${view}`} aria-labelledby="task-projection-title">
      <header className="workbench-page__header task-projection__header">
        <div>
          <p className="workbench-eyebrow">{projection.data?.task.title ?? "当前主任务"}</p>
          <h1 id="task-projection-title">{title}</h1>
          <p>{description}</p>
        </div>
        {projection.data ? (
          <div className="task-projection__freshness" role="status" aria-live="polite">
            <Icon name="retry" />{projection.data.refresh_after_ms === 3_000 ? "活动态 · 每 3 秒刷新" : "稳定态 · 每 5 秒刷新"}
          </div>
        ) : null}
      </header>
      <TaskTabs taskId={taskId} />
      {projection.isPending ? <div className="task-projection__loading" role="status">正在构建 WorkItem 投影…</div> : null}
      {projection.isError ? <div className="task-projection__error" role="alert"><strong>无法读取子任务投影</strong><span>{projection.error.message}</span><button type="button" className="workbench-secondary-button" onClick={() => void projection.refetch()}>重新读取</button></div> : null}
      {projection.data ? (
        <>
          <div className="task-projection__toolbar" aria-label="看板筛选">
            <div className="task-projection__counts" aria-label="状态汇总">
              {(Object.keys(businessStatusLabel) as BusinessStatus[]).map((status) => (
                <span key={status}><i className={`task-dot task-dot--${status.toLowerCase()}`} aria-hidden="true" />{businessStatusLabel[status]} <strong>{projection.data.summary[status]}</strong></span>
              ))}
            </div>
            <div className="task-projection__filters">
              <label><span>Agent</span><select value={agent} onChange={(event) => setAgent(event.currentTarget.value as typeof agent)}><option value="all">全部</option><option value="image">Image</option><option value="ppt">PPT</option></select></label>
              <label><span>Stage</span><select value={stageId} onChange={(event) => setStageId(event.currentTarget.value)}><option value="all">全部</option>{projection.data.stages.map((stage) => <option value={stage.stage_id} key={stage.stage_id}>S{stage.position} · {agentLabel(stage.type)}</option>)}</select></label>
            </div>
          </div>
          {projection.data.items.length === 0 ? (
            <div className="task-projection__empty"><Icon name="plan" /><h2>计划尚未生成逻辑子任务</h2><p>在 Master 中确认计划后，看板与阶段依赖会自动出现在这里。</p><Link className="workbench-primary-button" to={`/tasks/${encodeURIComponent(taskId)}/master`}>返回 Master</Link></div>
          ) : view === "board" ? (
            <BoardView items={filteredItems} />
          ) : (
            <PlanView stages={projection.data.stages.filter((stage) => stageId === "all" || stage.stage_id === stageId)} items={filteredItems} />
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
        <code>{item.work_item_id}</code>
        <span className={`task-status task-status--${item.business_status.toLowerCase()}`}><span aria-hidden="true" />{businessStatusLabel[item.business_status]}</span>
      </div>
      <dl className="workbench-definition-list">
        <div><dt>原始状态</dt><dd><code>{item.raw_status}</code> · {rawStatusLabel[item.raw_status] ?? item.raw_status}</dd></div>
        <div><dt>Stage</dt><dd>S{item.stage.position} · {item.stage.stage_id}</dd></div>
        <div><dt>阶段依赖</dt><dd>{item.stage.depends_on.length ? item.stage.depends_on.join("、") : "无"}</dd></div>
        <div><dt>子任务依赖</dt><dd>{item.depends_on.length ? item.depends_on.join("、") : "无"}</dd></div>
        <div><dt>当前实例</dt><dd>{item.current_instance?.instance_id ?? "未创建"}</dd></div>
        <div><dt>实例状态</dt><dd>{item.current_instance ? `${rawStatusLabel[item.current_instance.status] ?? item.current_instance.status} · ${item.current_instance.approval_mode === "human" ? "人工审批" : "Master 审批"}` : "无"}</dd></div>
        <div><dt>实例历史</dt><dd>{item.instance_ids.length} 个实例 · {item.attempts.length} 次自动重试</dd></div>
        <div><dt>已验证交付</dt><dd>{item.delivery_count} 个</dd></div>
        <div><dt>最近变化</dt><dd><time dateTime={item.updated_at}>{formatTime(item.updated_at)}</time></dd></div>
      </dl>
      {item.pending_approvals.length ? (
        <section className="work-item-detail__section" aria-labelledby="work-item-approvals"><h3 id="work-item-approvals">待处理审批</h3><ul>{item.pending_approvals.map((approval) => <li key={approval.approval_id}><Icon name="status" /><span><strong>{approvalLabel[approval.kind] ?? approval.kind}</strong><small>{approval.owner === "human" ? "人工处理" : "Master 处理"} · {shortId(approval.approval_id)}</small></span></li>)}</ul></section>
      ) : null}
      {item.attempts.length ? (
        <section className="work-item-detail__section" aria-labelledby="work-item-attempts"><h3 id="work-item-attempts">重试记录</h3><ol>{item.attempts.map((attempt) => <li key={attempt.attempt_id}><code>{shortId(attempt.attempt_id)}</code><span>{attempt.status}</span></li>)}</ol></section>
      ) : null}
      {item.alerts.length ? (
        <section className="work-item-detail__alerts" aria-label="子任务提醒">{item.alerts.map((alert) => <div className={`work-item-alert work-item-alert--${alert.severity}`} key={`${alert.code}-${alert.message}`}><Icon name="status" /><span><strong>{alert.code}</strong>{alert.message}</span></div>)}</section>
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
