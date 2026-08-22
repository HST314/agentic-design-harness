import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { NavLink, useParams } from "react-router-dom";
import type {
  CommandEnvelope,
  MasterAssetReference,
  MasterSessionResponse,
} from "../../api/client";
import { ApiError } from "../../api/client";
import {
  api,
  masterSessionQuery,
  taskHistoryQuery,
  taskIntakeQuery,
} from "../../api/queries";
import type {
  ContractMasterMessage,
  ContractPlanProposal,
} from "../../api/generated-contracts";
import { Icon } from "../../components/Icon";
import { TaskIntakePage } from "../task-intake/TaskIntakePage";

const ACTOR_ID = "human_operator";

const taskStatusLabel: Record<string, string> = {
  DRAFT: "规划中",
  PLANNED: "计划已保存",
  AWAITING_START_CONFIRMATION: "等待启动确认",
  RUNNING: "运行中",
  WAITING_APPROVAL: "等待审批",
  BLOCKED_UNAVAILABLE: "能力不可用",
  FAILED: "失败",
  SUCCEEDED: "已完成",
  PARTIAL: "部分完成",
  CANCELLED: "已取消",
};

const messageLabel: Record<ContractMasterMessage["kind"], string> = {
  text: "用户消息",
  clarification: "Master 澄清",
  plan_proposal: "Master 计划",
  plan_confirmation: "系统记录",
  error: "系统错误",
};

function operationId(prefix: string): string {
  const suffix = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replaceAll("-", "")
    : `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `${prefix}_${suffix}`.slice(0, 128);
}

function envelope(key: string, expectedRevision: number): CommandEnvelope {
  return {
    idempotency_key: key,
    actor_type: "human",
    actor_id: ACTOR_ID,
    expected_revision: expectedRevision,
  };
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function TaskTabs({ taskId }: { taskId: string }): React.JSX.Element {
  const base = `/tasks/${encodeURIComponent(taskId)}`;
  return (
    <nav className="workbench-task-tabs" aria-label="任务工作区">
      <NavLink to={`${base}/master`}><Icon name="message" />Master</NavLink>
      <NavLink to={`${base}/board`}><Icon name="board" />看板</NavLink>
      <NavLink to={`${base}/plan`}><Icon name="plan" />计划</NavLink>
    </nav>
  );
}

function MessageCard({ message }: { message: ContractMasterMessage }): React.JSX.Element {
  return (
    <article
      className={`master-message master-message--${message.role} master-message--${message.kind}`}
      aria-label={`${messageLabel[message.kind]}，${formatTime(message.created_at)}`}
    >
      <header>
        <span>{messageLabel[message.kind]}</span>
        <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>
      </header>
      <p>{message.content}</p>
      {message.asset_refs.length ? (
        <ul className="master-message__assets" aria-label="引用资源">
          {message.asset_refs.map((asset) => <li key={asset.asset_id}><Icon name="file-check" />{asset.asset_id}</li>)}
        </ul>
      ) : null}
    </article>
  );
}

function ProposalCard({
  proposal,
  startPolicy,
  confirming,
  onConfirm,
  onAdjust,
}: {
  proposal: ContractPlanProposal;
  startPolicy: "manual" | "auto";
  confirming: boolean;
  onConfirm: () => void;
  onAdjust: () => void;
}): React.JSX.Element {
  const stageById = useMemo(
    () => new Map(proposal.stages.map((stage) => [stage.stage_id, stage])),
    [proposal.stages],
  );
  return (
    <section className="master-proposal" aria-labelledby={`proposal-${proposal.revision}`}>
      <header className="master-proposal__header">
        <div>
          <p className="workbench-eyebrow">PlanProposal · r{proposal.revision}</p>
          <h2 id={`proposal-${proposal.revision}`}>执行计划预览</h2>
          <p>{proposal.stages.length} 个阶段 · {proposal.work_items.length} 个逻辑子任务</p>
        </div>
        <span className={`master-proposal__status master-proposal__status--${proposal.status.toLowerCase()}`}>
          {proposal.status === "PENDING_CONFIRMATION" ? "待确认" : proposal.status === "CONFIRMED" ? "已确认" : "已被替换"}
        </span>
      </header>
      <ol className="master-stage-list">
        {proposal.stages.map((stage) => (
          <li key={stage.stage_id}>
            <div className="master-stage-list__marker" aria-hidden="true">{stage.position}</div>
            <div>
              <strong>{stage.type === "image" ? "Image 设计阶段" : "PPT 阶段"}</strong>
              <span>{stage.required ? "必需" : "可选"} · {stage.depends_on.length ? `依赖 ${stage.depends_on.join("、")}` : "无前置依赖"}</span>
            </div>
          </li>
        ))}
      </ol>
      <div className="master-work-items" aria-label="计划子任务">
        {proposal.work_items.map((item) => {
          const stage = stageById.get(item.stage_id);
          return (
            <article key={item.work_item_id}>
              <div><span className="master-agent-chip">{item.agent_type === "image" ? "Image" : "PPT"}</span><span>{item.required ? "必需" : "可选"}</span></div>
              <h3>{item.title}</h3>
              <p>{stage ? `阶段 ${stage.position}` : item.stage_id} · {item.depends_on.length ? `依赖 ${item.depends_on.length} 项` : "可独立开始"}</p>
              <code>{item.work_item_id}</code>
            </article>
          );
        })}
      </div>
      {proposal.status === "PENDING_CONFIRMATION" ? (
        <footer className="master-proposal__actions">
          <div>
            <strong>{startPolicy === "manual" ? "人工确认模式" : "自动运行模式"}</strong>
            <span>{startPolicy === "manual" ? "确认前不会创建或启动运行实例。" : "系统会执行同样的预算、凭据与 Adapter 门禁。"}</span>
          </div>
          {startPolicy === "manual" ? (
            <div>
              <button type="button" className="workbench-secondary-button" onClick={onAdjust}>要求调整</button>
              <button type="button" className="workbench-primary-button" disabled={confirming} onClick={onConfirm}>{confirming ? "正在确认…" : "确认并运行"}</button>
            </div>
          ) : <span className="master-auto-badge"><Icon name="status" />自动校验中</span>}
        </footer>
      ) : null}
    </section>
  );
}

function ConfirmDialog({
  proposal,
  open,
  pending,
  onCancel,
  onConfirm,
}: {
  proposal: ContractPlanProposal | null;
  open: boolean;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}): React.JSX.Element | null {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  if (!proposal) return null;
  return (
    <dialog ref={ref} className="master-confirm-dialog" aria-labelledby="master-confirm-title" onCancel={(event) => { event.preventDefault(); onCancel(); }}>
      <div className="workbench-drawer__header"><div><p className="workbench-eyebrow">最终确认</p><h2 id="master-confirm-title">启动计划 r{proposal.revision}</h2></div></div>
      <div className="master-confirm-dialog__body">
        <p>确认后将保存当前计划、分配凭据并启动满足门禁的实例。此操作始终使用你刚审阅的修订版本。</p>
        <dl><div><dt>阶段</dt><dd>{proposal.stages.length}</dd></div><div><dt>逻辑子任务</dt><dd>{proposal.work_items.length}</dd></div></dl>
        <div className="workbench-dialog-actions">
          <button type="button" className="workbench-secondary-button" disabled={pending} onClick={onCancel}>返回审阅</button>
          <button type="button" className="workbench-primary-button" disabled={pending} onClick={onConfirm}>{pending ? "正在启动…" : "确认并启动"}</button>
        </div>
      </div>
    </dialog>
  );
}

function MasterWorkspace({ taskId }: { taskId: string }): React.JSX.Element {
  const queryClient = useQueryClient();
  const session = useQuery(masterSessionQuery(taskId));
  const [content, setContent] = useState("");
  const [selectedAssets, setSelectedAssets] = useState<Set<string>>(() => new Set());
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  const append = useMutation({
    mutationFn: ({ text, refs }: { text: string; refs: MasterAssetReference[] }) => api.appendMasterMessage(taskId, {
      content: text,
      asset_refs: refs,
      envelope: envelope(operationId("master_message"), session.data?.thread_revision ?? 0),
    }),
    onSuccess: (response) => {
      queryClient.setQueryData(masterSessionQuery(taskId).queryKey, response);
      setContent("");
      setSelectedAssets(new Set());
      setNotice("消息已保存，Master 正在处理。");
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === "REVISION_CONFLICT") {
        setNotice("线程已更新，已重新读取；请确认内容后再次发送。");
        void queryClient.invalidateQueries({ queryKey: masterSessionQuery(taskId).queryKey });
      }
    },
  });
  const confirm = useMutation({
    mutationFn: () => {
      const proposal = session.data?.latest_proposal;
      if (!proposal || !session.data) throw new Error("当前没有可确认计划。");
      return api.confirmPlanProposal(taskId, proposal.revision, {
        task_expected_revision: session.data.task_revision,
        envelope: envelope(operationId("confirm_plan"), proposal.revision),
      });
    },
    onSuccess: (response) => {
      queryClient.setQueryData(masterSessionQuery(taskId).queryKey, response.session);
      void queryClient.invalidateQueries({ queryKey: taskHistoryQuery.queryKey });
      setConfirmOpen(false);
      setNotice("计划已确认，实例启动结果已记录。");
    },
    onError: (error) => {
      setConfirmOpen(false);
      if (error instanceof ApiError && error.code === "REVISION_CONFLICT") {
        setNotice("计划或任务已更新，本次未启动；请重新审阅最新版本。");
        void queryClient.invalidateQueries({ queryKey: masterSessionQuery(taskId).queryKey });
      }
    },
  });

  if (session.isPending) return <section className="workbench-page"><div className="workbench-intake-card" role="status">正在恢复 Master 永久线程…</div></section>;
  if (session.isError || !session.data) {
    return <section className="workbench-page"><div className="workbench-intake-card"><p className="workbench-inline-error" role="alert">{session.error?.message ?? "无法读取 Master 线程。"}</p><button type="button" className="workbench-secondary-button" onClick={() => void session.refetch()}>重新读取</button></div></section>;
  }
  const data: MasterSessionResponse = session.data;
  const active = data.thread.active_run;
  const busy = active?.status === "SUBMITTING" || active?.status === "RUNNING";
  const proposal = data.latest_proposal;
  const refs = data.assets
    .filter((asset) => selectedAssets.has(asset.asset_id))
    .map(({ asset_id, manifest_relpath }) => ({ asset_id, manifest_relpath }));

  return (
    <section className="workbench-page master-workspace" aria-labelledby="master-title">
      <header className="workbench-page__header master-workspace__header">
        <div><p className="workbench-eyebrow">Master 永久线程</p><h1 id="master-title">{data.task.title}</h1><p>澄清、计划版本与启动结果均持久化在当前主任务下。</p></div>
        <span className={`master-task-status master-task-status--${data.task.status.toLowerCase()}`}><span aria-hidden="true" />{taskStatusLabel[data.task.status] ?? data.task.status}</span>
      </header>
      <TaskTabs taskId={taskId} />
      {!data.gateway_available || data.thread.last_error?.code === "MASTER_UNAVAILABLE" ? (
        <div className="master-alert master-alert--error" role="alert"><Icon name="status" /><div><strong>Master 服务未配置</strong><p>{data.thread.last_error?.message ?? "需要配置真实 MasterGateway 后才能分析消息；系统不会生成占位回复或伪计划。"}</p></div></div>
      ) : busy ? (
        <div className="master-alert" role="status"><Icon name="status" /><div><strong>已进入 Master 分析阶段</strong><p>消息与运行标识已保存；页面会每 3 秒读取一次版本化结果。</p></div></div>
      ) : null}
      <div className="master-thread" role="log" aria-live="polite" aria-label="Master 消息记录">
        {data.messages.length ? data.messages.map((message) => <MessageCard key={message.message_id} message={message} />) : <p className="master-thread__empty">尚无消息。发送目标或补充要求以开始规划。</p>}
      </div>
      {proposal ? (
        <ProposalCard
          proposal={proposal}
          startPolicy={data.task.start_policy}
          confirming={confirm.isPending}
          onConfirm={() => setConfirmOpen(true)}
          onAdjust={() => {
            setContent(`请调整计划 r${proposal.revision}：`);
            requestAnimationFrame(() => composerRef.current?.focus());
          }}
        />
      ) : null}
      <form
        className="master-composer"
        onSubmit={(event) => {
          event.preventDefault();
          const text = content.trim();
          if (!text) return;
          setNotice(null);
          append.mutate({ text, refs });
        }}
      >
        <label htmlFor="master-message"><span>发送给 Master</span><textarea ref={composerRef} id="master-message" rows={4} maxLength={20_000} value={content} placeholder="补充目标、回答澄清，或说明需要调整的计划内容…" onChange={(event) => setContent(event.currentTarget.value)} /></label>
        {data.assets.length ? (
          <fieldset><legend>引用已有资源（创建提交后不可追加上传）</legend><div>{data.assets.map((asset) => <label key={asset.asset_id}><input type="checkbox" checked={selectedAssets.has(asset.asset_id)} onChange={(event) => { const checked = event.currentTarget.checked; setSelectedAssets((current) => { const next = new Set(current); if (checked) next.add(asset.asset_id); else next.delete(asset.asset_id); return next; }); }} /><Icon name="file-check" /><span><strong>{asset.filename}</strong>{asset.description ? <small>{asset.description}</small> : null}</span></label>)}</div></fieldset>
        ) : null}
        <footer><span>{content.length.toLocaleString("zh-CN")} / 20,000</span><button type="submit" className="workbench-primary-button" aria-label="发送 Master 消息" disabled={!content.trim() || busy || append.isPending}>{append.isPending ? "正在保存…" : busy ? "等待 Master 完成" : "发送消息"}</button></footer>
        {notice ? <p className="master-composer__notice" role="status">{notice}</p> : null}
        {append.isError ? <p className="workbench-inline-error" role="alert">{append.error.message}</p> : null}
        {confirm.isError ? <p className="workbench-inline-error" role="alert">{confirm.error.message}</p> : null}
      </form>
      <ConfirmDialog proposal={proposal} open={confirmOpen} pending={confirm.isPending} onCancel={() => setConfirmOpen(false)} onConfirm={() => confirm.mutate()} />
    </section>
  );
}

export function MasterRoutePage(): React.JSX.Element {
  const { taskId = "" } = useParams();
  const intake = useQuery(taskIntakeQuery(taskId));
  if (intake.data?.intake.status === "DRAFT") return <TaskIntakePage />;
  if (intake.isPending) return <section className="workbench-page"><div className="workbench-intake-card" role="status">正在读取任务阶段…</div></section>;
  if (intake.isError && !(intake.error instanceof ApiError && intake.error.status === 404)) {
    return <section className="workbench-page"><div className="workbench-intake-card"><p className="workbench-inline-error" role="alert">{intake.error.message}</p></div></section>;
  }
  return <MasterWorkspace taskId={taskId} />;
}
