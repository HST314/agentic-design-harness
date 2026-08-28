import { NavLink, useParams } from "react-router-dom";
import { Icon } from "../../components/Icon";

type View = "master" | "board" | "plan" | "work-item";

const pageCopy: Record<View, { eyebrow: string; title: string; description: string }> = {
  master: {
    eyebrow: "Master 永久线程",
    title: "任务分析与计划确认",
    description: "此处将接入持久化消息、澄清与执行计划更新。",
  },
  board: {
    eyebrow: "逻辑工作项",
    title: "当前任务看板",
    description: "此处将呈现子任务状态；看板不会通过拖拽直接改写执行状态。",
  },
  plan: {
    eyebrow: "阶段与依赖",
    title: "任务计划",
    description: "此处将按阶段位置展示依赖、是否必需与专业助手可用性。",
  },
  "work-item": {
    eyebrow: "专业工作台边界",
    title: "子任务入口",
    description: "此处将通过安全链接打开图片助手工作台，并提供失败回退。",
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
