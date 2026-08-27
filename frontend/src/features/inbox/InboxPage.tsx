import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError, type ApprovalDetailResponse, type InboxItem } from "../../api/client";
import { api, approvalDetailQuery, inboxQuery } from "../../api/queries";
import { Icon } from "../../components/Icon";
import { actionLabel } from "../../ui";

const inboxStatusLabel: Record<InboxItem["status"], string> = {
  UNREAD: "未读",
  READ: "已读",
  HANDLED: "已处理",
};

const inboxKindLabel: Record<InboxItem["kind"], string> = {
  APPROVAL_REQUIRED: "需要审批",
  BUDGET_APPROVAL_REQUIRED: "预算审批",
  CONFIG_RESTART_REQUIRED: "需要重启",
  DELIVERY_REVIEW_REQUIRED: "交付复核",
  INSTANCE_CRASHED: "实例中断",
  INSTANCE_DELIVERY_REJECTED: "交付被拒",
  INSTANCE_FAILED: "实例失败",
  INSTANCE_SUCCEEDED: "实例完成",
  STAGE_READY: "阶段就绪",
  TASK_FAILED: "任务失败",
  TASK_SUCCEEDED: "任务完成",
};

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function operationId(prefix: string): string {
  const suffix = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replaceAll("-", "")
    : `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `${prefix}_${suffix}`;
}

function commandEnvelope(actorId: string, revision: number): Record<string, unknown> {
  return {
    idempotency_key: operationId("ui"),
    expected_revision: revision,
    actor_type: "human",
    actor_id: actorId,
  };
}

function inboxRevision(item: InboxItem): number {
  return (item as InboxItem & { store_revision?: number }).store_revision ?? item.revision;
}

function ApprovalForm({
  details,
  onResolved,
}: {
  details: ApprovalDetailResponse;
  onResolved: () => void;
}): React.JSX.Element {
  const [action, setAction] = useState(details.payload.available_actions[0] ?? "");
  const [payloadText, setPayloadText] = useState("{}");
  const [actorId, setActorId] = useState("human_operator");
  const [feedback, setFeedback] = useState<string | null>(null);
  const resolve = useMutation({
    mutationFn: (decision: "APPROVED" | "REJECTED") => {
      let payload: Record<string, unknown>;
      try {
        const parsed: unknown = JSON.parse(payloadText || "{}");
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error("动作参数必须是 JSON 对象。");
        }
        payload = parsed as Record<string, unknown>;
      } catch (error) {
        throw error instanceof Error && error.message === "动作参数必须是 JSON 对象。"
          ? error
          : new Error("动作参数不是有效的 JSON，请检查后重试。");
      }
      return api.resolveApproval(details.approval.approval_id, {
        decision,
        action: decision === "APPROVED" ? action : null,
        payload,
        operation_id: operationId("approval"),
        envelope: commandEnvelope(actorId, details.approval_revision),
      });
    },
    onSuccess: () => {
      setFeedback("决议已提交，正在同步状态。");
      onResolved();
    },
    onError: (error) => {
      setFeedback(error.message || "提交失败，请重试。");
    },
  });
  const actions = details.payload.available_actions;
  return (
    <form
      className="inbox-approval-form"
      onSubmit={(event) => {
        event.preventDefault();
        const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
        const decision = submitter?.dataset.decision === "REJECTED" ? "REJECTED" : "APPROVED";
        setFeedback(null);
        resolve.mutate(decision);
      }}
    >
      <div className="inbox-approval-form__grid">
        <label className="workbench-field">
          <span>推进动作</span>
          <select value={action} disabled={!actions.length} onChange={(event) => setAction(event.currentTarget.value)}>
            {actions.map((item) => <option key={item} value={item}>{actionLabel(item)} · {item}</option>)}
          </select>
        </label>
        <label className="workbench-field">
          <span>操作人 ID</span>
          <input
            value={actorId}
            pattern="[A-Za-z][A-Za-z0-9_\-]{0,127}"
            required
            onChange={(event) => setActorId(event.currentTarget.value)}
          />
        </label>
        <label className="workbench-field inbox-approval-form__payload">
          <span>动作参数（JSON）</span>
          <textarea rows={3} spellCheck={false} value={payloadText} onChange={(event) => setPayloadText(event.currentTarget.value)} />
          <small>只填写当前动作需要的字段；无参数时保留 {"{}"}。</small>
        </label>
      </div>
      <div className="workbench-dialog-actions">
        <button className="workbench-secondary-button" type="submit" data-decision="REJECTED" disabled={resolve.isPending}>拒绝</button>
        <button className="workbench-primary-button" type="submit" data-decision="APPROVED" disabled={resolve.isPending}>{resolve.isPending ? "正在提交…" : "批准并推进"}</button>
      </div>
      {feedback ? <p className={resolve.isError ? "workbench-inline-error" : "master-composer__notice"} role={resolve.isError ? "alert" : "status"}>{feedback}</p> : null}
    </form>
  );
}

function InboxCard({
  item,
  selected,
  onChanged,
}: {
  item: InboxItem;
  selected: boolean;
  onChanged: () => void;
}): React.JSX.Element {
  const cardRef = useRef<HTMLElement>(null);
  const queryClient = useQueryClient();
  const [detailsOpen, setDetailsOpen] = useState(selected);
  const approvalId = item.approval_id ?? "";
  const approval = useQuery({
    ...approvalDetailQuery(approvalId),
    enabled: Boolean(approvalId && (selected || detailsOpen)),
  });
  const details = approval.data ?? null;
  const markRead = useMutation({
    mutationFn: () => api.updateInboxStatus(item.inbox_id, {
      status: "READ",
      envelope: commandEnvelope("human_operator", inboxRevision(item)),
    }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: inboxQuery.queryKey });
    },
  });
  useEffect(() => {
    if (selected) cardRef.current?.scrollIntoView({ block: "center" });
  }, [selected]);
  const pending = details?.approval.status === "PENDING";
  const missingApproval = approval.error instanceof ApiError
    && [404, 410].includes(approval.error.status);
  return (
    <article ref={cardRef} className={`inbox-card${selected ? " inbox-card--selected" : ""}`}>
      <header className="inbox-card__head">
        <div>
          <span className={`inbox-status inbox-status--${item.status.toLowerCase()}`}><span aria-hidden="true" />{inboxStatusLabel[item.status]}</span>
          <span className="inbox-card__kind">{inboxKindLabel[item.kind] ?? item.kind}</span>
        </div>
        <time dateTime={item.created_at}>{formatTime(item.created_at)}</time>
      </header>
      <h3>{item.title}</h3>
      <p className="inbox-card__message">{item.message}</p>
      <dl className="inbox-card__meta">
        <div><dt>任务</dt><dd>{item.task_id}</dd></div>
        <div><dt>实例</dt><dd>{item.instance_id ?? "—"}</dd></div>
        <div><dt>队列序号</dt><dd>{item.sequence}</dd></div>
      </dl>
      <div className="inbox-card__actions">
        {item.status === "UNREAD" ? (
          <button className="workbench-secondary-button" type="button" disabled={markRead.isPending} onClick={() => markRead.mutate()}>
            <Icon name="file-check" />{markRead.isPending ? "正在保存…" : markRead.isError ? "重试标为已读" : "标为已读"}
          </button>
        ) : null}
        {item.instance_id ? (
          <Link className="workbench-secondary-button" to={`/instances/${encodeURIComponent(item.instance_id)}`}>查看实例</Link>
        ) : null}
      </div>
      {item.approval_id ? (
        <details
          className="inbox-card__context"
          open={selected || detailsOpen}
          onToggle={(event) => setDetailsOpen(event.currentTarget.open)}
        >
          <summary>{pending ? "查看并处理审批" : "查看审批详情"}</summary>
          {approval.isPending ? <p role="status">正在读取审批详情…</p> : null}
          {approval.isError ? (
            <div className="workbench-inline-error" role="alert">
              <p>{missingApproval ? "该审批已不存在或已失效。" : approval.error.message}</p>
              {missingApproval ? null : (
                <button className="workbench-secondary-button" type="button" onClick={() => void approval.refetch()}>
                  重试读取审批
                </button>
              )}
            </div>
          ) : null}
          {details ? (
            <>
              <pre>{JSON.stringify(details.payload.context, null, 2)}</pre>
              {pending ? (
                <ApprovalForm
                  details={details}
                  onResolved={() => {
                    void queryClient.invalidateQueries({ queryKey: approvalDetailQuery(approvalId).queryKey });
                    onChanged();
                  }}
                />
              ) : <p className="inbox-card__handled">该审批已完成处理。</p>}
            </>
          ) : null}
        </details>
      ) : null}
    </article>
  );
}

export function InboxPage(): React.JSX.Element {
  const inbox = useQuery(inboxQuery);
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const selectedApproval = searchParams.get("approval_id");
  const items = inbox.data?.items ?? [];
  const pendingCount = items.filter((item) => item.status !== "HANDLED").length;
  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: inboxQuery.queryKey });
  };
  return (
    <section className="workbench-page inbox-page" aria-labelledby="inbox-title">
      <header className="workbench-page__header">
        <div>
          <p className="workbench-eyebrow">FIFO 队列</p>
          <h1 id="inbox-title">收件箱</h1>
          <p>按事件顺序处理人工审批与运行通知，已读和已处理分别记录。</p>
        </div>
      </header>
      {inbox.isPending ? <div className="task-projection__loading" role="status">正在读取收件箱…</div> : null}
      {inbox.isError ? (
        <div className="task-projection__error" role="alert">
          <strong>无法读取收件箱</strong>
          <span>{inbox.error.message}</span>
          <button type="button" className="workbench-secondary-button" onClick={() => void inbox.refetch()}>重新读取</button>
        </div>
      ) : null}
      {inbox.data ? (
        items.length ? (
          <>
            <div className="inbox-page__heading">
              <h2>按到达顺序处理</h2>
              <span className="delivery-count">{pendingCount} 待处理</span>
            </div>
            <div className="inbox-page__list">
              {items.map((item) => (
                <InboxCard
                  key={item.inbox_id}
                  item={item}
                  selected={selectedApproval !== null && selectedApproval === item.approval_id}
                  onChanged={refresh}
                />
              ))}
            </div>
          </>
        ) : (
          <div className="task-projection__empty">
            <Icon name="inbox" />
            <h2>收件箱已清空</h2>
            <p>新的审批与运行通知会按事件顺序出现在这里。</p>
          </div>
        )
      ) : null}
    </section>
  );
}
