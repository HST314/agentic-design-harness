import { NavLink, useParams } from "react-router-dom";
import { Icon } from "../../components/Icon";

type View = "master" | "board" | "plan" | "work-item";

const pageCopy: Record<View, { eyebrow: string; title: string; description: string }> = {
  master: {
    eyebrow: "Master 永久线程",
    title: "任务分析与计划确认",
    description: "F2 将在此接入持久化消息、澄清与 PlanProposal 修订。",
  },
  board: {
    eyebrow: "逻辑工作项",
    title: "当前任务看板",
    description: "F3 将在此投影 WorkItem 状态；看板不会通过拖拽写领域状态。",
  },
  plan: {
    eyebrow: "Stage 与依赖",
    title: "任务计划",
    description: "F3 将按阶段位置展示依赖、required/optional 与 Agent 可用性。",
  },
  "work-item": {
    eyebrow: "专业工作台边界",
    title: "子任务入口",
    description: "F4 将从受控 UI link 打开 Image Agent 工作台，并提供失败回退。",
  },
};

export function FoundationPage({ view }: { view: View }): React.JSX.Element {
  const { taskId = "", workItemId } = useParams();
  const copy = pageCopy[view];
  const base = `/tasks/${encodeURIComponent(taskId)}`;

  return (
    <section className="workbench-page" aria-labelledby="foundation-title">
      <header className="workbench-page__header">
        <div>
          <p className="workbench-eyebrow">{copy.eyebrow}</p>
          <h1 id="foundation-title">{copy.title}</h1>
          <p>{copy.description}</p>
        </div>
        <span className="workbench-phase-badge"><span aria-hidden="true" />路由已建立</span>
      </header>

      <nav className="workbench-task-tabs" aria-label="任务工作区">
        <NavLink to={`${base}/master`}><Icon name="message" />Master</NavLink>
        <NavLink to={`${base}/board`}><Icon name="board" />看板</NavLink>
        <NavLink to={`${base}/plan`}><Icon name="plan" />计划</NavLink>
      </nav>

      <article className="workbench-boundary-card">
        <div className="workbench-boundary-card__index" aria-hidden="true">F0</div>
        <div>
          <p className="workbench-eyebrow">当前交付边界</p>
          <h2>{workItemId ? `工作项 ${workItemId}` : `任务 ${taskId}`}</h2>
          <p>此页面已纳入统一 AppShell、浏览器深链和 Query Provider。真实数据写入仍由后续功能阶段及领域命令负责。</p>
          <NavLink className="workbench-legacy-link" to={`${base}`}>打开已验收任务详情</NavLink>
        </div>
      </article>
    </section>
  );
}
