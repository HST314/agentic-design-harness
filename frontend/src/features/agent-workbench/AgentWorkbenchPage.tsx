import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  agentWorkbenchLinkQuery,
  workItemDetailQuery,
} from "../../api/queries";
import type { AgentWorkbenchLinkResponse } from "../../api/client";
import { Icon } from "../../components/Icon";
import { TaskTabs } from "../master-thread/MasterThreadPage";

type FrameState = "loading" | "ready" | "failed";

const businessStatusLabel = {
  TODO: "待办",
  RUNNING: "运行中",
  WAITING_APPROVAL: "待审批",
  COMPLETED: "已完成",
  EXCEPTION: "异常",
} as const;

function WorkbenchFailure({
  link,
  message,
  onRetry,
  taskId,
}: {
  link?: AgentWorkbenchLinkResponse;
  message: string;
  onRetry: () => void;
  taskId: string;
}): React.JSX.Element {
  const heading = link?.link_status === "FRAME_BLOCKED"
    ? "Image Agent 无法安全内嵌"
    : link?.link_status === "NO_UI_URL"
      ? "Image Agent 尚未提供工作台"
      : "Image Agent 工作台加载失败";
  return (
    <div className="agent-workbench-error" role="alert">
      <Icon name="status" />
      <div>
        <p className="workbench-eyebrow">专业工作台</p>
        <h2>{heading}</h2>
        <p>{message}</p>
        <div className="agent-workbench-error__actions">
          <button className="workbench-secondary-button" type="button" onClick={onRetry}>
            <Icon name="retry" />重新检查
          </button>
          {link?.ui_url ? (
            <a
              className="workbench-secondary-button"
              href={link.ui_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              <Icon name="external-link" />在新标签页中尝试
            </a>
          ) : null}
          <Link className="workbench-secondary-button" to={`/tasks/${encodeURIComponent(taskId)}/board`}>
            返回看板
          </Link>
        </div>
      </div>
    </div>
  );
}

export function AgentWorkbenchPage(): React.JSX.Element {
  const { taskId = "", workItemId = "" } = useParams();
  const detail = useQuery(workItemDetailQuery(taskId, workItemId));
  const item = detail.data?.item;
  const instanceId = item?.current_instance?.instance_id ?? "";
  const link = useQuery({
    ...agentWorkbenchLinkQuery(taskId, workItemId, instanceId),
    enabled: Boolean(item?.agent_type === "image" && instanceId),
  });
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const loadTimeoutRef = useRef<number | undefined>(undefined);
  const [frameKey, setFrameKey] = useState(0);
  const [frameState, setFrameState] = useState<FrameState>("loading");

  useEffect(() => {
    if (!link.data?.embeddable || !link.data.ui_url) return undefined;
    setFrameState("loading");
    loadTimeoutRef.current = window.setTimeout(() => setFrameState("failed"), 12_000);
    return () => window.clearTimeout(loadTimeoutRef.current);
  }, [frameKey, link.data?.embeddable, link.data?.ui_url]);

  const retry = (): void => {
    setFrameState("loading");
    setFrameKey((value) => value + 1);
    void link.refetch();
  };

  if (detail.isPending) {
    return <section className="workbench-page agent-workbench-state" role="status">正在读取 Image WorkItem…</section>;
  }
  if (detail.isError || !item) {
    return (
      <section className="workbench-page agent-workbench-state" role="alert">
        <strong>无法读取逻辑子任务</strong>
        <p>{detail.error?.message}</p>
        <button className="workbench-secondary-button" type="button" onClick={() => void detail.refetch()}>重新读取</button>
      </section>
    );
  }

  if (item.agent_type === "ppt" || !item.stage.available) {
    return (
      <section className="workbench-page agent-workbench" aria-labelledby="agent-workbench-title">
        <header className="agent-workbench__context">
          <div><p className="workbench-eyebrow">{detail.data.task.title} / 阶段 {item.stage.position}</p><h1 id="agent-workbench-title">{item.title}</h1><p>当前任务需要的 PPT 创作能力尚未开放，任务不会被误标为完成。</p></div>
          <Link className="workbench-secondary-button" to={`/tasks/${encodeURIComponent(taskId)}/board`}>返回看板</Link>
        </header>
        <TaskTabs taskId={taskId} />
        <div className="agent-workbench-boundary" role="note"><Icon name="status" /><div><h2>PPT 工作台暂不可用</h2><p>请返回看板调整计划，或选择当前已开放的创作能力。</p></div></div>
      </section>
    );
  }

  const readyLink = link.data?.embeddable && link.data.ui_url ? link.data : undefined;
  const statusText = businessStatusLabel[item.business_status];
  const failureMessage = frameState === "failed"
    ? "工作台在 12 秒内未完成加载。可重新检查、返回看板，或使用已验证地址在新标签页中尝试。"
    : link.data?.link_status === "FRAME_BLOCKED"
      ? "当前工作台无法在此页面安全打开。请在新标签页中尝试，或返回看板。"
      : link.data?.link_status === "NO_UI_URL"
        ? "工作台仍在准备中，请稍后重新检查。"
        : link.data?.link_status === "ADAPTER_UNAVAILABLE"
          ? "专业工作台暂时不可用，请稍后重新检查；当前任务进度已保留。"
          : link.error?.message ?? "当前专业工作台暂时不可用，请稍后重新检查。";

  return (
    <section className="workbench-page agent-workbench" aria-labelledby="agent-workbench-title">
      <header className="agent-workbench__context">
        <div>
          <p className="workbench-eyebrow">{detail.data.task.title} / 阶段 {item.stage.position}</p>
          <h1 id="agent-workbench-title">{item.title}</h1>
          <p>Image Agent 专业会话与审批保留在下方原生工作台中，Harness 只维护任务上下文和安全入口。</p>
        </div>
        <div className="agent-workbench__actions" ref={toolbarRef} tabIndex={-1}>
          <span className={`task-status task-status--${item.business_status.toLowerCase()}`}><span aria-hidden="true" />{statusText}</span>
          <Link className="workbench-secondary-button" to={`/tasks/${encodeURIComponent(taskId)}/board`}><Icon name="board" />返回看板</Link>
          {link.data?.ui_url ? (
            <a className="workbench-secondary-button" href={link.data.ui_url} target="_blank" rel="noopener noreferrer"><Icon name="external-link" />新标签页</a>
          ) : null}
        </div>
      </header>
      <TaskTabs taskId={taskId} />

      <div className="agent-workbench__security" role="status" aria-live="polite">
        <Icon name="status" />
        <span>{readyLink ? "专业工作台连接已验证" : link.isPending ? "正在准备专业工作台…" : "专业工作台暂时不可用"}</span>
      </div>

      {link.isPending ? <div className="agent-workbench__loading" role="status">正在获取受控 Image Agent 工作台地址…</div> : null}
      {link.isError || (link.data && !readyLink) ? (
        <WorkbenchFailure link={link.data} message={failureMessage} onRetry={retry} taskId={taskId} />
      ) : null}
      {readyLink && frameState === "failed" ? (
        <WorkbenchFailure link={readyLink} message={failureMessage} onRetry={retry} taskId={taskId} />
      ) : null}
      {readyLink && frameState !== "failed" ? (
        <div className="agent-workbench-frame">
          <div className="agent-workbench-frame__entry">
            <button className="workbench-secondary-button" type="button" onClick={() => iframeRef.current?.focus()}>跳到 Image Agent 工作台</button>
            <span>专业工作台将在安全隔离区域内打开。</span>
          </div>
          {frameState === "loading" ? <div className="agent-workbench-frame__overlay" role="status">Image Agent 工作台正在加载…</div> : null}
          <iframe
            key={`${readyLink.instance_id}-${frameKey}`}
            ref={iframeRef}
            className="agent-workbench-frame__iframe"
            src={readyLink.ui_url ?? undefined}
            title={`Image Agent 工作台：${item.title}`}
            sandbox="allow-downloads allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-same-origin allow-scripts"
            allow="clipboard-read; clipboard-write"
            referrerPolicy="no-referrer"
            onLoad={() => { window.clearTimeout(loadTimeoutRef.current); setFrameState("ready"); }}
            onError={() => { window.clearTimeout(loadTimeoutRef.current); setFrameState("failed"); }}
          />
          <div className="agent-workbench-frame__exit">
            <button className="workbench-secondary-button" type="button" onClick={() => toolbarRef.current?.focus()}>返回工作台操作栏</button>
            <Link to={`/tasks/${encodeURIComponent(taskId)}/board`}>结束专业工作，返回看板</Link>
          </div>
        </div>
      ) : null}
    </section>
  );
}
