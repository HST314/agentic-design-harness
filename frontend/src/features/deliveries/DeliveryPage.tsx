import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import type {
  BundleManifest,
  DeliveryBundleCandidate,
  DeliveryReview,
} from "../../api/client";
import { api, deliveryBundlesQuery, workItemsQuery } from "../../api/queries";
import { Icon } from "../../components/Icon";
import { TaskTabs } from "../master-thread/MasterThreadPage";

const statusCopy: Record<DeliveryBundleCandidate["status"], { label: string; detail: string }> = {
  PENDING_CONFIRMATION: { label: "待确认", detail: "图片与说明正在等待确认。" },
  PUBLISHED: { label: "已入库", detail: "图片与说明已一起进入共享资源。" },
  REJECTED: { label: "已退回", detail: "候选与处理记录已保留，未进入共享资源。" },
  CORRUPTED: { label: "文件异常", detail: "候选文件校验未通过。" },
};

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function MarkdownPreview({ markdown }: { markdown: string }): React.JSX.Element {
  const blocks = markdown.split(/\n{2,}/).filter((item) => item.trim());
  return (
    <div className="delivery-markdown" aria-label="设计说明预览">
      {blocks.map((block, index) => {
        const value = block.trim();
        const heading = value.match(/^(#{1,3})\s+(.+)$/s);
        if (heading) {
          const content = heading[2];
          if (heading[1]?.length === 1) return <h3 key={index}>{content}</h3>;
          return <h4 key={index}>{content}</h4>;
        }
        const lines = value.split("\n");
        if (lines.every((line) => /^[-*]\s+/.test(line))) {
          return <ul key={index}>{lines.map((line) => <li key={line}>{line.replace(/^[-*]\s+/, "")}</li>)}</ul>;
        }
        if (/^```/.test(value) && /```$/.test(value)) {
          return <pre key={index}><code>{value.replace(/^```[^\n]*\n?/, "").replace(/```$/, "")}</code></pre>;
        }
        return <p key={index}>{lines.map((line, lineIndex) => <span key={`${lineIndex}-${line}`}>{line}{lineIndex < lines.length - 1 ? <br /> : null}</span>)}</p>;
      })}
    </div>
  );
}

function DeliveryDecisionDialog({
  candidate,
  decision,
  pending,
  error,
  onCancel,
  onConfirm,
}: {
  candidate: DeliveryBundleCandidate;
  decision: "APPROVED" | "REJECTED";
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}): React.JSX.Element {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialogRef.current?.showModal();
    return () => dialogRef.current?.close();
  }, []);
  const approved = decision === "APPROVED";
  return (
    <dialog
      ref={dialogRef}
      className="delivery-dialog"
      aria-labelledby="delivery-dialog-title"
      onCancel={(event) => { event.preventDefault(); if (!pending) onCancel(); }}
    >
      <div className="workbench-drawer__header">
        <div><p className="workbench-eyebrow">人工交付确认</p><h2 id="delivery-dialog-title">{approved ? "确认方案入库" : "退回方案修改"}</h2></div>
        <button type="button" className="workbench-icon-button" aria-label="关闭交付决议窗口" disabled={pending} onClick={onCancel}><Icon name="close" /></button>
      </div>
      <div className="delivery-dialog__body">
        <p>{approved ? "图片与设计说明将作为同一批次进入共享资源；已存在的其他方案不会被覆盖。" : "此操作不会删除候选。图片、说明与退回记录仍会保留，方便后续修改。"}</p>
        <dl className="workbench-definition-list">
          <div><dt>图片</dt><dd>{candidate.image.width} × {candidate.image.height} · {formatBytes(candidate.image.size_bytes)}</dd></div>
          <div><dt>设计说明</dt><dd>{formatBytes(candidate.design_note.size_bytes)}</dd></div>
          <div><dt>处理结果</dt><dd>{approved ? "进入共享资源并防止覆盖" : "保留在候选方案中"}</dd></div>
        </dl>
        {error ? <p className="workbench-inline-error" role="alert">{error}<br />候选尚未改变，请检查连接后重试。</p> : null}
      </div>
      <footer className="workbench-dialog-actions delivery-dialog__actions">
        <button type="button" className="workbench-secondary-button" disabled={pending} onClick={onCancel}>取消</button>
        <button type="button" className={approved ? "workbench-primary-button" : "delivery-reject-button"} disabled={pending} onClick={onConfirm} aria-busy={pending}>{pending ? "正在提交决议…" : approved ? "确认图片与说明并入库" : "确认退回修改"}</button>
      </footer>
    </dialog>
  );
}

function DeliveryCard({
  taskId,
  candidate,
  manifest,
  review,
  selected,
  candidateNumber,
  onDecide,
  onVerify,
}: {
  taskId: string;
  candidate: DeliveryBundleCandidate;
  manifest: BundleManifest | undefined;
  review: DeliveryReview | undefined;
  selected: boolean;
  candidateNumber: number;
  onDecide: (candidate: DeliveryBundleCandidate, review: DeliveryReview, decision: "APPROVED" | "REJECTED", trigger: HTMLButtonElement) => void;
  onVerify: () => void;
}): React.JSX.Element {
  const [imageRetry, setImageRetry] = useState(0);
  const [imageFailed, setImageFailed] = useState(false);
  const note = useQuery({
    queryKey: ["delivery-note", taskId, candidate.bundle_id],
    queryFn: ({ signal }) => api.previewDeliveryMarkdown(taskId, candidate.bundle_id, signal),
    retry: false,
  });
  const status = statusCopy[candidate.status];
  const pendingReview = review?.approval.status === "PENDING";
  return (
    <article
      id={`bundle-${candidate.bundle_id}`}
      className={`delivery-card delivery-card--${candidate.status.toLowerCase()}${selected ? " delivery-card--selected" : ""}`}
      tabIndex={-1}
    >
      <header className="delivery-card__header">
        <div>
          <p className="workbench-eyebrow">设计候选</p>
          <h3>候选方案 {candidateNumber}</h3>
          <p>{status.detail}</p>
        </div>
        <span className={`delivery-status delivery-status--${candidate.status.toLowerCase()}`}><span aria-hidden="true" />{status.label}</span>
      </header>
      <div className="delivery-card__previews">
        <section aria-labelledby={`image-title-${candidate.bundle_id}`}>
          <div className="delivery-preview__heading"><div><p className="workbench-eyebrow">最终图片</p><h4 id={`image-title-${candidate.bundle_id}`}>冻结预览</h4></div><span>{candidate.image.width} × {candidate.image.height}</span></div>
          <div className="delivery-image-preview">
            {!imageFailed ? <img key={imageRetry} src={api.deliveryPreviewUrl(taskId, candidate.bundle_id, "image", imageRetry)} alt={`候选方案 ${candidateNumber} 最终图片预览`} loading="lazy" width={candidate.image.width} height={candidate.image.height} onLoad={() => setImageFailed(false)} onError={() => setImageFailed(true)} /> : (
              <div role="alert"><Icon name="status" /><strong>图片预览读取失败</strong><p>候选未被修改，可重新执行完整性校验并加载。</p><button type="button" className="workbench-secondary-button" onClick={() => { setImageFailed(false); setImageRetry((value) => value + 1); }}>重新加载图片</button></div>
            )}
          </div>
          <dl className="delivery-file-facts"><div><dt>尺寸</dt><dd>{candidate.image.width} × {candidate.image.height}</dd></div><div><dt>大小</dt><dd>{formatBytes(candidate.image.size_bytes)}</dd></div></dl>
        </section>
        <section aria-labelledby={`note-title-${candidate.bundle_id}`}>
          <div className="delivery-preview__heading"><div><p className="workbench-eyebrow">设计说明</p><h4 id={`note-title-${candidate.bundle_id}`}>说明预览</h4></div><span>{formatBytes(candidate.design_note.size_bytes)}</span></div>
          {note.isPending ? <div className="delivery-note-state" role="status">正在校验并呈现设计说明…</div> : null}
          {note.isError ? <div className="delivery-note-state delivery-note-state--error" role="alert"><strong>说明预览读取失败</strong><span>{note.error.message}</span><button type="button" className="workbench-secondary-button" onClick={() => void note.refetch()}>重新读取说明</button></div> : null}
          {note.data !== undefined ? <MarkdownPreview markdown={note.data} /> : null}
        </section>
      </div>
      <footer className="delivery-card__actions">
        {candidate.status === "PENDING_CONFIRMATION" && pendingReview && review ? <>
          <button type="button" className="workbench-primary-button" onClick={(event) => onDecide(candidate, review, "APPROVED", event.currentTarget)}>确认图片与说明并入库</button>
          <button type="button" className="delivery-reject-button" onClick={(event) => onDecide(candidate, review, "REJECTED", event.currentTarget)}>退回修改</button>
        </> : null}
        {candidate.status === "PENDING_CONFIRMATION" && !pendingReview ? <p className="delivery-inline-warning" role="alert">交付审批暂不可用。刷新列表以恢复决议入口。</p> : null}
        {candidate.status === "PUBLISHED" ? <>
          <button type="button" className="workbench-primary-button" disabled>已入库</button>
          <button type="button" className="workbench-secondary-button" onClick={onVerify}>验证入库结果</button>
        </> : null}
        {manifest ? <>
          <Link className="workbench-secondary-button" to={`/tasks/${encodeURIComponent(taskId)}/resources?asset_id=${encodeURIComponent(manifest.image_asset.asset_id)}`}><Icon name="external-link" />打开共享图片</Link>
          <Link className="workbench-secondary-button" to={`/tasks/${encodeURIComponent(taskId)}/resources?asset_id=${encodeURIComponent(manifest.design_note_asset.asset_id)}`}><Icon name="external-link" />打开共享说明</Link>
        </> : null}
      </footer>
    </article>
  );
}

export function DeliveryPage(): React.JSX.Element {
  const { taskId = "" } = useParams();
  const [searchParams] = useSearchParams();
  const selectedBundle = searchParams.get("bundle_id");
  const queryClient = useQueryClient();
  const bundles = useQuery(deliveryBundlesQuery(taskId));
  const workItems = useQuery(workItemsQuery(taskId));
  const [dialog, setDialog] = useState<{ candidate: DeliveryBundleCandidate; review: DeliveryReview; decision: "APPROVED" | "REJECTED" } | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const decide = useMutation({
    mutationFn: async (value: NonNullable<typeof dialog>) => {
      const requestId = `delivery_${crypto.randomUUID().replaceAll("-", "")}`;
      return api.resolveApproval(value.review.approval.approval_id, {
        decision: value.decision,
        action: value.decision === "APPROVED" ? "publish_bundle" : null,
        payload: {},
        operation_id: requestId,
        envelope: {
          idempotency_key: requestId,
          actor_type: "human",
          actor_id: "human_operator",
          expected_revision: value.review.approval_revision,
        },
      });
    },
    onSuccess: async (_, value) => {
      setFeedback(value.decision === "APPROVED" ? "图片与设计说明已一并进入共享资源。" : "候选方案已退回，图片、说明和处理记录均已保留。");
      setDialog(null);
      await queryClient.invalidateQueries({ queryKey: deliveryBundlesQuery(taskId).queryKey });
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    },
  });
  const groups = useMemo(() => {
    const values = new Map<string, DeliveryBundleCandidate[]>();
    for (const candidate of bundles.data?.candidates ?? []) {
      values.set(candidate.work_item_id, [...(values.get(candidate.work_item_id) ?? []), candidate]);
    }
    return [...values.entries()];
  }, [bundles.data?.candidates]);
  const manifests = new Map((bundles.data?.manifests ?? []).map((item) => [item.bundle_id, item]));
  const reviews = new Map((bundles.data?.reviews ?? []).map((item) => [item.bundle_id, item]));
  const workItemTitles = new Map((workItems.data?.items ?? []).map((item) => [item.work_item_id, item.title]));

  useEffect(() => {
    if (!selectedBundle || !bundles.data) return;
    window.requestAnimationFrame(() => {
      const target = document.getElementById(`bundle-${selectedBundle}`);
      target?.scrollIntoView({ block: "center" });
      target?.focus({ preventScroll: true });
    });
  }, [bundles.data, selectedBundle]);

  return (
    <section className="workbench-page deliveries-page" aria-label="任务交付">
      <TaskTabs taskId={taskId} />
      {feedback ? <p className="delivery-feedback" role="status" aria-live="polite"><Icon name="file-check" />{feedback}</p> : null}
      {bundles.isPending ? <div className="task-projection__loading" role="status">正在读取设计候选…</div> : null}
      {bundles.isError ? <div className="task-projection__error" role="alert"><strong>无法读取设计候选</strong><span>请检查连接后重试。</span><button type="button" className="workbench-secondary-button" onClick={() => void bundles.refetch()}>重新读取</button></div> : null}
      {bundles.data && groups.length === 0 ? <div className="task-projection__empty"><Icon name="file-check" /><h2>暂无待确认的设计候选</h2><p>图片助手完成候选方案后，图片与设计说明会在这里等待人工确认。</p></div> : null}
      <div className="delivery-groups">
        {groups.map(([workItemId, candidates], groupIndex) => <section className="delivery-group" aria-labelledby={`delivery-group-${workItemId}`} key={workItemId}><header><div><p className="workbench-eyebrow">设计任务</p><h2 id={`delivery-group-${workItemId}`}>{workItemTitles.get(workItemId) ?? `设计任务 ${groupIndex + 1}`}</h2></div><span>{candidates.length} 个候选方案</span></header><div className="delivery-group__cards">{candidates.map((candidate, index) => <DeliveryCard key={candidate.bundle_id} taskId={taskId} candidate={candidate} candidateNumber={index + 1} manifest={manifests.get(candidate.bundle_id)} review={reviews.get(candidate.bundle_id)} selected={selectedBundle === candidate.bundle_id} onDecide={(selectedCandidate, review, decision, trigger) => { triggerRef.current = trigger; decide.reset(); setDialog({ candidate: selectedCandidate, review, decision }); }} onVerify={() => { setFeedback("正在复验候选方案与共享资源…"); void bundles.refetch().then((result) => setFeedback(result.isError ? null : "候选方案的入库结果已复验。")); }} />)}</div></section>)}
      </div>
      {dialog ? <DeliveryDecisionDialog candidate={dialog.candidate} decision={dialog.decision} pending={decide.isPending} error={decide.isError ? decide.error.message : null} onCancel={() => { if (decide.isPending) return; setDialog(null); window.requestAnimationFrame(() => triggerRef.current?.focus()); }} onConfirm={() => decide.mutate(dialog)} /> : null}
    </section>
  );
}
