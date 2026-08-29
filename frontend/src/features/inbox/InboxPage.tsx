import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError, type ApprovalDetailResponse, type InboxItem } from "../../api/client";
import { api, approvalDetailQuery, inboxQuery, workItemsQuery } from "../../api/queries";
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
  INSTANCE_CRASHED: "子任务中断",
  INSTANCE_DELIVERY_REJECTED: "交付被拒",
  INSTANCE_FAILED: "子任务失败",
  INSTANCE_SUCCEEDED: "子任务完成",
  STAGE_READY: "阶段就绪",
  TASK_FAILED: "任务失败",
  TASK_SUCCEEDED: "任务完成",
};

const payloadFreeActions = new Set([
  "approve_category_constraint",
  "approve_final",
  "approve_once",
  "approve_skill_invocations",
  "approve_taskbook",
  "apply_clarification_safe_defaults",
  "apply_taskbook_scope_boundaries",
  "build_taskbook",
  "choose_master",
  "continue_clarification_after_budget_change",
  "open_final_approval",
  "prepare_style_direction",
  "publish_bundle",
  "regenerate_taskbook",
  "render_candidates",
  "resume_quality_inspection",
  "retry_category_constraint",
  "retry_skill_invocations",
  "start_category_match",
  "start_clarification",
  "start_quality_inspection",
]);

export function canResolveApprovalInInbox(action: string): boolean {
  return payloadFreeActions.has(action);
}

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

export function designerFacingNotification(value: string): string {
  return value
    .replace(/\bImage Agent\b/gi, "图片助手")
    .replace(/\bPPT Agent\b/gi, "演示文稿助手")
    .replace(/\bAgent\b/gi, "专业助手")
    .replace(/正在\s+[A-Za-z][A-Za-z0-9_-]+\s+等待处理[。.\s]*/gi, "正在等待处理。")
    .replace(/实例\s+instance_[A-Za-z0-9_-]+/gi, "该子任务")
    .replace(/重试\s+attempt_[A-Za-z0-9_-]+/gi, "本次重试")
    .replace(/分支\s+[A-Za-z][A-Za-z0-9_-]{7,}/gi, "设计分支")
    .replace(/\b(?:work|stage|instance|card|proposal|attempt|approval|task|bundle|checkpoint)_[A-Za-z0-9_-]+\b/gi, "")
    .replace(/\b[A-Za-z]+(?:_[A-Za-z0-9]+)+\b/g, "")
    .replace(/\s+([，。；：])/g, "$1")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/([\u3400-\u9fff])\s+([\u3400-\u9fff])/g, "$1$2")
    .trim();
}

function ApprovalForm({
  details,
  onResolved,
}: {
  details: ApprovalDetailResponse;
  onResolved: () => void;
}): React.JSX.Element {
  const actions = details.payload.available_actions.filter(canResolveApprovalInInbox);
  const [action, setAction] = useState(actions[0] ?? "");
  const [feedback, setFeedback] = useState<string | null>(null);
  const resolve = useMutation({
    mutationFn: (decision: "APPROVED" | "REJECTED") => {
      return api.resolveApproval(details.approval.approval_id, {
        decision,
        action: decision === "APPROVED" ? action : null,
        payload: {},
        operation_id: operationId("approval"),
        envelope: commandEnvelope("human_operator", details.approval_revision),
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
      {actions.length ? (
        <div className="inbox-approval-form__grid">
          <label className="workbench-field">
            <span>推进动作</span>
            <select value={action} onChange={(event) => setAction(event.currentTarget.value)}>
              {actions.map((item) => <option key={item} value={item}>{actionLabel(item)}</option>)}
            </select>
          </label>
        </div>
      ) : <p>此项需要在专业工作台中查看设计内容并完成选择。</p>}
      <div className="workbench-dialog-actions">
        <button className="workbench-secondary-button" type="submit" data-decision="REJECTED" disabled={resolve.isPending}>拒绝</button>
        <button className="workbench-primary-button" type="submit" data-decision="APPROVED" disabled={resolve.isPending || !actions.length}>{resolve.isPending ? "正在提交…" : "批准并推进"}</button>
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
  const instanceId = item.instance_id ?? "";
  const approval = useQuery({
    ...approvalDetailQuery(approvalId),
    enabled: Boolean(approvalId && (selected || detailsOpen)),
  });
  const details = approval.data ?? null;
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
      <h3>{designerFacingNotification(item.title)}</h3>
      <p className="inbox-card__message">{designerFacingNotification(item.message)}</p>
      <div className="inbox-card__actions">
        {instanceId ? (
          <Link className="workbench-secondary-button" to={`/instances/${encodeURIComponent(instanceId)}`}>打开专业工作台</Link>
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
              {pending ? (
                <ApprovalForm
                  details={details}
                  onResolved={() => {
                    void queryClient.invalidateQueries({ queryKey: approvalDetailQuery(approvalId).queryKey });
                    void queryClient.invalidateQueries({
                      queryKey: workItemsQuery(details.approval.task_id).queryKey,
                    });
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
      <h1 id="inbox-title" className="sr-only">收件箱</h1>
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
              <h2>待处理通知</h2>
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
