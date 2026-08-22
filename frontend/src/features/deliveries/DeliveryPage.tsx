import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import type {
  BundleManifest,
  DeliveryBundleCandidate,
  DeliveryReview,
} from "../../api/client";
import { api, deliveryBundlesQuery } from "../../api/queries";
import { Icon } from "../../components/Icon";
import { TaskTabs } from "../master-thread/MasterThreadPage";

const statusCopy: Record<DeliveryBundleCandidate["status"], { label: string; detail: string }> = {
  PENDING_CONFIRMATION: { label: "待确认", detail: "图片与说明仍在私有候选区。" },
  PUBLISHED: { label: "已入库", detail: "图片与说明已作为同一原子批次发布。" },
  REJECTED: { label: "已退回", detail: "候选与决议记录已保留，未进入共享资源。" },
  CORRUPTED: { label: "已损坏", detail: "冻结文件未通过完整性复验。" },
};

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function MarkdownPreview({ markdown }: { markdown: string }): React.JSX.Element {
  const blocks = markdown.split(/\n{2,}/).filter((item) => item.trim());
  return (
    <div className="delivery-markdown" aria-label="渲染后的 Markdown 设计说明">
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
  review,
  pending,
  error,
  onCancel,
  onConfirm,
}: {
  candidate: DeliveryBundleCandidate;
  decision: "APPROVED" | "REJECTED";
  review: DeliveryReview;
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
        <div><p className="workbench-eyebrow">人工交付门禁</p><h2 id="delivery-dialog-title">{approved ? "确认双资产入库" : "退回分支修改"}</h2></div>
        <button type="button" className="workbench-icon-button" aria-label="关闭交付决议窗口" disabled={pending} onClick={onCancel}><Icon name="close" /></button>
      </div>
      <div className="delivery-dialog__body">
        <p>{approved ? "图片与 Markdown 将以同一 publication batch 进入共享资源；已存在的其他分支不会被覆盖。" : "此操作不会删除候选。图片、说明与退回决议仍保留用于审计和后续修改。"}</p>
        <dl className="workbench-definition-list">
          <div><dt>分支</dt><dd>{candidate.branch_id}</dd></div>
          <div><dt>冻结节点</dt><dd>{candidate.checkpoint_id}</dd></div>
          <div><dt>图片</dt><dd>{candidate.image.mime_type} · {candidate.image.width} × {candidate.image.height}</dd></div>
          <div><dt>设计说明</dt><dd>Markdown · {formatBytes(candidate.design_note.size_bytes)}</dd></div>
          <div><dt>目标</dt><dd>{approved ? "resources/shared（不可覆盖）" : "私有候选区（保留）"}</dd></div>
          <div><dt>审批修订</dt><dd>r{review.approval_revision}</dd></div>
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
  onDecide,
  onVerify,
}: {
  taskId: string;
  candidate: DeliveryBundleCandidate;
  manifest: BundleManifest | undefined;
  review: DeliveryReview | undefined;
  selected: boolean;
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
          <p className="workbench-eyebrow">分支 {candidate.branch_id}</p>
          <h3>{candidate.bundle_id}</h3>
          <p>{status.detail}</p>
        </div>
        <span className={`delivery-status delivery-status--${candidate.status.toLowerCase()}`}><span aria-hidden="true" />{status.label}</span>
      </header>
      <div className="delivery-card__previews">
        <section aria-labelledby={`image-title-${candidate.bundle_id}`}>
          <div className="delivery-preview__heading"><div><p className="workbench-eyebrow">最终图片</p><h4 id={`image-title-${candidate.bundle_id}`}>冻结预览</h4></div><span>{candidate.image.width} × {candidate.image.height}</span></div>
          <div className="delivery-image-preview">
            {!imageFailed ? <img key={imageRetry} src={api.deliveryPreviewUrl(taskId, candidate.bundle_id, "image", imageRetry)} alt={`分支 ${candidate.branch_id} 最终图片预览`} loading="lazy" width={candidate.image.width} height={candidate.image.height} onLoad={() => setImageFailed(false)} onError={() => setImageFailed(true)} /> : (
              <div role="alert"><Icon name="status" /><strong>图片预览读取失败</strong><p>候选未被修改，可重新执行完整性校验并加载。</p><button type="button" className="workbench-secondary-button" onClick={() => { setImageFailed(false); setImageRetry((value) => value + 1); }}>重新加载图片</button></div>
            )}
          </div>
          <dl className="delivery-file-facts"><div><dt>格式</dt><dd>{candidate.image.mime_type}</dd></div><div><dt>大小</dt><dd>{formatBytes(candidate.image.size_bytes)}</dd></div><div><dt>SHA</dt><dd title={candidate.image.sha256}>{candidate.image.sha256.slice(0, 12)}…</dd></div></dl>
        </section>
        <section aria-labelledby={`note-title-${candidate.bundle_id}`}>
          <div className="delivery-preview__heading"><div><p className="workbench-eyebrow">设计说明</p><h4 id={`note-title-${candidate.bundle_id}`}>Markdown 预览</h4></div><span>{formatBytes(candidate.design_note.size_bytes)}</span></div>
          {note.isPending ? <div className="delivery-note-state" role="status">正在校验并渲染 Markdown…</div> : null}
          {note.isError ? <div className="delivery-note-state delivery-note-state--error" role="alert"><strong>说明预览读取失败</strong><span>{note.error.message}</span><button type="button" className="workbench-secondary-button" onClick={() => void note.refetch()}>重新读取说明</button></div> : null}
          {note.data !== undefined ? <><MarkdownPreview markdown={note.data} /><details className="delivery-raw-note"><summary>查看原始 Markdown</summary><pre tabIndex={0}>{note.data}</pre></details></> : null}
        </section>
      </div>
      <dl className="delivery-provenance">
        <div><dt>WorkItem</dt><dd>{candidate.work_item_id}</dd></div>
        <div><dt>实例</dt><dd>{candidate.instance_id}</dd></div>
        <div><dt>TaskCard</dt><dd>r{candidate.task_card_revision}</dd></div>
        <div><dt>Checkpoint</dt><dd>{candidate.checkpoint_id}</dd></div>
      </dl>
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
      setFeedback(value.decision === "APPROVED" ? `分支 ${value.candidate.branch_id} 的双资产已原子入库。` : `分支 ${value.candidate.branch_id} 已退回，候选和记录均已保留。`);
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

  useEffect(() => {
    if (!selectedBundle || !bundles.data) return;
    window.requestAnimationFrame(() => {
      const target = document.getElementById(`bundle-${selectedBundle}`);
      target?.scrollIntoView({ block: "center" });
      target?.focus({ preventScroll: true });
    });
  }, [bundles.data, selectedBundle]);

  return (
    <section className="workbench-page deliveries-page" aria-labelledby="deliveries-title">
      <header className="workbench-page__header">
        <div><p className="workbench-eyebrow">分支级人工门禁</p><h1 id="deliveries-title">交付包</h1><p>逐分支审阅最终图片与 Markdown；只有明确确认后，两份资产才会同批次进入共享资源。</p></div>
        {bundles.data ? <span className="delivery-count">{bundles.data.candidates.filter((item) => item.status === "PENDING_CONFIRMATION").length} 个待确认</span> : null}
      </header>
      <TaskTabs taskId={taskId} />
      {feedback ? <p className="delivery-feedback" role="status" aria-live="polite"><Icon name="file-check" />{feedback}</p> : null}
      {bundles.isPending ? <div className="task-projection__loading" role="status">正在读取分支交付候选…</div> : null}
      {bundles.isError ? <div className="task-projection__error" role="alert"><strong>无法读取交付包</strong><span>{bundles.error.message}</span><button type="button" className="workbench-secondary-button" onClick={() => void bundles.refetch()}>重新读取</button></div> : null}
      {bundles.data && groups.length === 0 ? <div className="task-projection__empty"><Icon name="file-check" /><h2>暂无分支交付候选</h2><p>Image Agent 冻结分支后，图片与设计说明会在这里等待人工确认。</p></div> : null}
      <div className="delivery-groups">
        {groups.map(([workItemId, candidates]) => <section className="delivery-group" aria-labelledby={`delivery-group-${workItemId}`} key={workItemId}><header><div><p className="workbench-eyebrow">WorkItem</p><h2 id={`delivery-group-${workItemId}`}>{workItemId}</h2></div><span>{candidates.length} 个分支候选</span></header><div className="delivery-group__cards">{candidates.map((candidate) => <DeliveryCard key={candidate.bundle_id} taskId={taskId} candidate={candidate} manifest={manifests.get(candidate.bundle_id)} review={reviews.get(candidate.bundle_id)} selected={selectedBundle === candidate.bundle_id} onDecide={(selectedCandidate, review, decision, trigger) => { triggerRef.current = trigger; decide.reset(); setDialog({ candidate: selectedCandidate, review, decision }); }} onVerify={() => { setFeedback(`正在复验 ${candidate.bundle_id} 的 BundleManifest 与共享资产…`); void bundles.refetch().then((result) => setFeedback(result.isError ? null : `${candidate.bundle_id} 的入库结果已复验。`)); }} />)}</div></section>)}
      </div>
      {dialog ? <DeliveryDecisionDialog candidate={dialog.candidate} decision={dialog.decision} review={dialog.review} pending={decide.isPending} error={decide.isError ? decide.error.message : null} onCancel={() => { if (decide.isPending) return; setDialog(null); window.requestAnimationFrame(() => triggerRef.current?.focus()); }} onConfirm={() => decide.mutate(dialog)} /> : null}
    </section>
  );
}
