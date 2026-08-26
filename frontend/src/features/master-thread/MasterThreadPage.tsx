import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { NavLink, useParams } from "react-router-dom";
import type {
  CommandEnvelope,
  EditableTaskCard,
  MasterAssetReference,
  MasterSessionAsset,
  MasterSessionResponse,
} from "../../api/client";
import { ApiError } from "../../api/client";
import {
  api,
  latestStartOperationQuery,
  masterSessionQuery,
  taskHistoryQuery,
  taskIntakeQuery,
} from "../../api/queries";
import type { StartOperation } from "../../api/client";
import type {
  ContractMasterMessage,
  ContractPlanProposal,
} from "../../api/generated-contracts";
import { Icon } from "../../components/Icon";
import { TaskIntakePage } from "../task-intake/TaskIntakePage";

const ACTOR_ID = "human_operator";
type ProposalTaskCard = ContractPlanProposal["execution_cards"][number];
type ExpectedDelivery = ProposalTaskCard["expected_deliveries"][number];

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

function textParameter(card: ProposalTaskCard, name: string): string {
  const value = card.parameters[name];
  return typeof value === "string" ? value : "";
}

function numberParameter(card: ProposalTaskCard, name: string): number | "" {
  const value = card.parameters[name];
  return typeof value === "number" ? value : "";
}

function deliveryLabel(delivery: ExpectedDelivery): string {
  const kind = {
    image: "图片",
    presentation: "演示文稿",
    document: "文档",
    archive: "压缩包",
    other: "其他",
  }[delivery.kind];
  return `${kind} · ${delivery.role} · ${delivery.accepted_mime_types.join("、")}`;
}

const startStageLabels = [
  "已受理",
  "准备运行环境",
  "启动进程",
  "健康检查",
  "工作台就绪",
] as const;

function startStageIndex(operation: StartOperation | null | undefined): number {
  if (!operation) return 0;
  if (operation.state === "COMMITTED") return startStageLabels.length - 1;
  const stageByState: Record<string, number> = {
    PENDING: 0,
    PREPARING: 1,
    PROCESS_STARTING: 2,
    AGENT_STARTING: 3,
    RUNNING: 4,
  };
  const stages = Object.values(operation.instance_progress).map(
    (item) => stageByState[item.state] ?? 0,
  );
  return stages.length ? Math.min(...stages) : 0;
}

function StartProgressBar({ operation }: { operation: StartOperation | null | undefined }): React.JSX.Element {
  const failed = operation?.state === "RETRYABLE_FAILED" || operation?.state === "ABORTED";
  const current = startStageIndex(operation);
  return (
    <section
      className={`master-start-progress${failed ? " master-start-progress--failed" : ""}`}
      aria-labelledby="master-start-progress-title"
    >
      <header>
        <div>
          <p className="workbench-eyebrow">实例启动</p>
          <h2 id="master-start-progress-title">{failed ? "启动未完成" : current === 4 ? "专业工作台已就绪" : "正在启动专业工作台"}</h2>
        </div>
        <span role="status" aria-live="polite">{failed ? "需要重试" : `${current + 1} / ${startStageLabels.length}`}</span>
      </header>
      <ol
        className="master-start-progress__steps"
        role="progressbar"
        aria-label="实例启动进度"
        aria-valuemin={1}
        aria-valuemax={startStageLabels.length}
        aria-valuenow={current + 1}
      >
        {startStageLabels.map((label, index) => (
          <li
            key={label}
            className={index < current ? "is-complete" : index === current ? "is-current" : ""}
          >
            <span aria-hidden="true">{index + 1}</span>
            <strong>{label}</strong>
          </li>
        ))}
      </ol>
      {failed ? (
        <p className="master-start-progress__message" role="alert">
          {operation?.last_error?.message ?? "实例启动失败，请在任务看板中由用户手动重试。"}
        </p>
      ) : (
        <p className="master-start-progress__message">启动会在后台继续；你可以随时手动切换到任务看板查看进度。</p>
      )}
    </section>
  );
}

export function TaskTabs({ taskId }: { taskId: string }): React.JSX.Element {
  const base = `/tasks/${encodeURIComponent(taskId)}`;
  return (
    <nav className="workbench-task-tabs" aria-label="任务工作区">
      <NavLink to={`${base}/master`}><Icon name="message" />Master</NavLink>
      <NavLink to={`${base}/board`}><Icon name="board" />看板</NavLink>
      <NavLink to={`${base}/plan`}><Icon name="plan" />计划</NavLink>
      <NavLink to={`${base}/deliveries`}><Icon name="file-check" />交付</NavLink>
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

function TaskCardReview({
  card,
  workItem,
  editable,
  onEdit,
}: {
  card: ProposalTaskCard;
  workItem: ContractPlanProposal["work_items"][number] | undefined;
  editable: boolean;
  onEdit: (trigger: HTMLButtonElement) => void;
}): React.JSX.Element {
  const parameterItems = card.agent_type === "image"
    ? [
      ["画幅", textParameter(card, "aspect_ratio") || "由 Agent 决定"],
      ["候选数量", numberParameter(card, "variants") || "默认"],
      ["使用场景", textParameter(card, "usage_context") || "未填写"],
      [
        "品类版本",
        textParameter(card, "category_id")
          ? `${textParameter(card, "category_id")}@${textParameter(card, "category_version") || "未填写"}`
          : "未指定",
      ],
    ]
    : [
      ["页数", numberParameter(card, "slide_count") || "默认"],
      ["计划资产角色", textParameter(card, "planned_asset_role") || "未指定"],
    ];
  return (
    <article className="master-task-card" aria-labelledby={`task-card-${card.card_id}`}>
      <header>
        <div>
          <span className="master-agent-chip">{card.agent_type === "image" ? "Image" : "PPT"}</span>
          <span className="master-task-card__revision">TaskCard · r{card.revision}</span>
        </div>
        {editable ? (
          <button
            type="button"
            className="workbench-secondary-button"
            aria-label={`编辑任务卡 ${workItem?.title ?? card.card_id}`}
            onClick={(event) => onEdit(event.currentTarget)}
          >
            <Icon name="rename" />编辑任务卡
          </button>
        ) : null}
      </header>
      <h3 id={`task-card-${card.card_id}`}>{workItem?.title ?? card.card_id}</h3>
      <p className="master-task-card__objective">{card.objective}</p>
      <dl className="master-task-card__parameters">
        {parameterItems.map(([label, value]) => (
          <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
        ))}
        <div><dt>依赖</dt><dd>{workItem?.depends_on.length ? workItem.depends_on.join("、") : "无前置依赖"}</dd></div>
      </dl>
      <div className="master-task-card__details">
        <section aria-label="执行指令">
          <h4>执行指令</h4>
          {card.instructions.length ? <ul>{card.instructions.map((item) => <li key={item}>{item}</li>)}</ul> : <p>无附加指令</p>}
        </section>
        <section aria-label="输入资产">
          <h4>输入资产</h4>
          {card.input_assets.length ? <ul>{card.input_assets.map((item) => <li key={item.asset_id}><code>{item.asset_id}</code></li>)}</ul> : <p>无输入资产</p>}
        </section>
        <section aria-label="输出要求">
          <h4>输出要求</h4>
          <ul>{card.expected_deliveries.map((item) => <li key={`${item.kind}-${item.role}`}>{deliveryLabel(item)}{item.required ? " · 必需" : " · 可选"}</li>)}</ul>
        </section>
      </div>
      <code className="master-task-card__id">{card.card_id}</code>
    </article>
  );
}

function TaskCardEditorDialog({
  card,
  assets,
  open,
  pending,
  error,
  onCancel,
  onSave,
}: {
  card: ProposalTaskCard | null;
  assets: MasterSessionAsset[];
  open: boolean;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onSave: (editable: EditableTaskCard) => void;
}): React.JSX.Element | null {
  const ref = useRef<HTMLDialogElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const [objective, setObjective] = useState("");
  const [instructions, setInstructions] = useState("");
  const [selectedAssetIds, setSelectedAssetIds] = useState<Set<string>>(() => new Set());
  const [deliveries, setDeliveries] = useState<ExpectedDelivery[]>([]);
  const [aspectRatio, setAspectRatio] = useState("");
  const [variants, setVariants] = useState<number | "">("");
  const [usageContext, setUsageContext] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const [categoryVersion, setCategoryVersion] = useState("");
  const [slideCount, setSlideCount] = useState<number | "">("");
  const [plannedAssetRole, setPlannedAssetRole] = useState("");

  useEffect(() => {
    if (!card || !open) return;
    setObjective(card.objective);
    setInstructions(card.instructions.join("\n"));
    setSelectedAssetIds(new Set(card.input_assets.map((asset) => asset.asset_id)));
    setDeliveries(card.expected_deliveries.map((delivery) => ({ ...delivery, accepted_mime_types: [...delivery.accepted_mime_types] })));
    setAspectRatio(textParameter(card, "aspect_ratio"));
    setVariants(numberParameter(card, "variants"));
    setUsageContext(textParameter(card, "usage_context"));
    setCategoryId(textParameter(card, "category_id"));
    setCategoryVersion(textParameter(card, "category_version"));
    setSlideCount(numberParameter(card, "slide_count"));
    setPlannedAssetRole(textParameter(card, "planned_asset_role"));
  }, [card, open]);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      dialog.showModal();
    }
    if (!open && dialog.open) {
      dialog.close();
      requestAnimationFrame(() => returnFocusRef.current?.focus());
    }
  }, [open]);
  if (!card) return null;
  const knownAssets = new Map(assets.map((asset) => [asset.asset_id, asset]));
  for (const asset of card.input_assets) {
    if (!knownAssets.has(asset.asset_id)) {
      knownAssets.set(asset.asset_id, {
        ...asset,
        filename: asset.asset_id,
        description: "计划引用资产",
      });
    }
  }
  return (
    <dialog
      ref={ref}
      className="master-card-editor"
      aria-labelledby="master-card-editor-title"
      onCancel={(event) => { event.preventDefault(); if (!pending) onCancel(); }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const inputAssets = [...selectedAssetIds].map((assetId) => {
            const asset = knownAssets.get(assetId);
            if (!asset) throw new Error(`未知资产 ${assetId}`);
            return { asset_id: asset.asset_id, manifest_relpath: asset.manifest_relpath };
          });
          const parameters: Record<string, unknown> = card.agent_type === "image"
            ? {
              ...(aspectRatio.trim() ? { aspect_ratio: aspectRatio.trim() } : {}),
              ...(variants === "" ? {} : { variants }),
              usage_context: usageContext.trim(),
              ...(categoryId.trim() ? { category_id: categoryId.trim() } : {}),
              ...(categoryVersion.trim() ? { category_version: categoryVersion.trim() } : {}),
            }
            : {
              ...(slideCount === "" ? {} : { slide_count: slideCount }),
              ...(plannedAssetRole.trim() ? { planned_asset_role: plannedAssetRole.trim() } : {}),
            };
          onSave({
            objective: objective.trim(),
            instructions: instructions.split("\n").map((item) => item.trim()).filter(Boolean),
            input_assets: inputAssets,
            expected_deliveries: deliveries.map((delivery) => ({
              ...delivery,
              role: delivery.role.trim(),
              accepted_mime_types: delivery.accepted_mime_types.map((mime) => mime.trim()).filter(Boolean),
            })),
            parameters,
          });
        }}
      >
        <header className="workbench-drawer__header">
          <div><p className="workbench-eyebrow">TaskCard · r{card.revision}</p><h2 id="master-card-editor-title">编辑任务卡</h2></div>
          <button type="button" className="workbench-icon-button" aria-label="关闭任务卡编辑" disabled={pending} onClick={onCancel}><Icon name="close" /></button>
        </header>
        <div className="master-card-editor__body">
          <p className="master-card-editor__notice">保存会创建新的 TaskCard 修订和 PlanProposal 修订；当前版本保留为只读历史。</p>
          <label htmlFor="task-card-objective"><span>目标</span><textarea id="task-card-objective" required maxLength={20_000} rows={4} value={objective} onChange={(event) => setObjective(event.currentTarget.value)} /></label>
          <label htmlFor="task-card-instructions"><span>指令（每行一条）</span><textarea id="task-card-instructions" maxLength={100_000} rows={5} value={instructions} onChange={(event) => setInstructions(event.currentTarget.value)} /></label>
          <fieldset>
            <legend>输入资产</legend>
            <div className="master-card-editor__assets">
              {[...knownAssets.values()].map((asset) => (
                <label key={asset.asset_id}>
                  <input
                    type="checkbox"
                    checked={selectedAssetIds.has(asset.asset_id)}
                    onChange={(event) => {
                      const checked = event.currentTarget.checked;
                      setSelectedAssetIds((current) => {
                        const next = new Set(current);
                        if (checked) next.add(asset.asset_id); else next.delete(asset.asset_id);
                        return next;
                      });
                    }}
                  />
                  <span><strong>{asset.filename}</strong><small>{asset.description || asset.asset_id}</small></span>
                </label>
              ))}
            </div>
          </fieldset>
          <fieldset>
            <legend>输出要求</legend>
            <div className="master-card-editor__deliveries">
              {deliveries.map((delivery, index) => (
                <fieldset key={`${index}-${delivery.kind}`}>
                  <legend>交付项 {index + 1}</legend>
                  <label><span>类型</span><select value={delivery.kind} onChange={(event) => setDeliveries((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, kind: event.currentTarget.value as ExpectedDelivery["kind"] } : item))}><option value="image">图片</option><option value="presentation">演示文稿</option><option value="document">文档</option><option value="archive">压缩包</option><option value="other">其他</option></select></label>
                  <label><span>角色</span><input required maxLength={128} value={delivery.role} onChange={(event) => setDeliveries((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, role: event.currentTarget.value } : item))} /></label>
                  <label><span>MIME（逗号分隔）</span><input required value={delivery.accepted_mime_types.join(", ")} onChange={(event) => setDeliveries((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, accepted_mime_types: event.currentTarget.value.split(",") } : item))} /></label>
                  <label className="master-card-editor__checkbox"><input type="checkbox" checked={delivery.required} onChange={(event) => setDeliveries((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, required: event.currentTarget.checked } : item))} /><span>必需交付</span></label>
                  <button type="button" className="workbench-text-button" disabled={deliveries.length === 1} onClick={() => setDeliveries((current) => current.filter((_, itemIndex) => itemIndex !== index))}>移除此项</button>
                </fieldset>
              ))}
              <button type="button" className="workbench-secondary-button" onClick={() => setDeliveries((current) => [...current, { kind: card.agent_type === "image" ? "image" : "presentation", role: card.agent_type === "image" ? "supporting_visual" : "presentation", required: false, accepted_mime_types: [card.agent_type === "image" ? "image/png" : "application/pdf"] }])}>添加交付项</button>
            </div>
          </fieldset>
          {card.agent_type === "image" ? (
            <fieldset>
              <legend>Image 参数</legend>
              <div className="master-card-editor__grid">
                <label><span>画幅</span><input pattern="[1-9][0-9]{0,3}:[1-9][0-9]{0,3}" placeholder="16:9" value={aspectRatio} onChange={(event) => setAspectRatio(event.currentTarget.value)} /></label>
                <label><span>候选数量</span><input type="number" min={1} max={64} value={variants} onChange={(event) => setVariants(event.currentTarget.value ? event.currentTarget.valueAsNumber : "")} /></label>
                <label className="master-card-editor__wide"><span>使用场景</span><input required maxLength={10_000} value={usageContext} onChange={(event) => setUsageContext(event.currentTarget.value)} /></label>
                <label><span>品类 ID</span><input maxLength={128} value={categoryId} onChange={(event) => setCategoryId(event.currentTarget.value)} /></label>
                <label><span>品类版本</span><input maxLength={64} value={categoryVersion} onChange={(event) => setCategoryVersion(event.currentTarget.value)} /></label>
              </div>
            </fieldset>
          ) : (
            <fieldset>
              <legend>PPT 参数</legend>
              <div className="master-card-editor__grid">
                <label><span>页数</span><input type="number" min={1} max={500} value={slideCount} onChange={(event) => setSlideCount(event.currentTarget.value ? event.currentTarget.valueAsNumber : "")} /></label>
                <label><span>计划资产角色</span><input maxLength={128} value={plannedAssetRole} onChange={(event) => setPlannedAssetRole(event.currentTarget.value)} /></label>
              </div>
            </fieldset>
          )}
          {error ? <p className="workbench-inline-error" role="alert">{error}</p> : null}
        </div>
        <footer className="workbench-dialog-actions master-card-editor__actions">
          <button type="button" className="workbench-secondary-button" disabled={pending} onClick={onCancel}>取消</button>
          <button type="submit" className="workbench-primary-button" disabled={pending}>{pending ? "正在保存修订…" : "保存为新修订"}</button>
        </footer>
      </form>
    </dialog>
  );
}

function ProposalCard({
  proposal,
  startPolicy,
  confirming,
  onConfirm,
  onAdjust,
  onEditCard,
}: {
  proposal: ContractPlanProposal;
  startPolicy: "manual" | "auto";
  confirming: boolean;
  onConfirm: (trigger: HTMLButtonElement) => void;
  onAdjust: () => void;
  onEditCard: (card: ProposalTaskCard, trigger: HTMLButtonElement) => void;
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
      <section className="master-task-cards" aria-labelledby={`task-cards-${proposal.revision}`}>
        <div className="master-task-cards__heading">
          <div><p className="workbench-eyebrow">人工审阅门禁</p><h3 id={`task-cards-${proposal.revision}`}>任务卡</h3></div>
          <span>{proposal.execution_cards.length} 张 · 保存编辑后必须重新审阅</span>
        </div>
        <div className="master-task-cards__grid">
          {proposal.execution_cards.map((card) => (
            <TaskCardReview
              key={card.card_id}
              card={card}
              workItem={proposal.work_items.find((item) => item.task_card_ids.includes(card.card_id))}
              editable={proposal.status === "PENDING_CONFIRMATION"}
              onEdit={(trigger) => onEditCard(card, trigger)}
            />
          ))}
        </div>
      </section>
      {proposal.status === "PENDING_CONFIRMATION" ? (
        <footer className="master-proposal__actions">
          <div>
            <strong>{startPolicy === "manual" ? "人工确认模式" : "自动规划 · 人工启动"}</strong>
            <span>{startPolicy === "manual" ? "审阅或调整计划后，由你确认才会创建或启动实例。" : "计划已自动生成；审阅或调整后，仍需由你确认才会创建或启动实例。"}</span>
          </div>
          <div>
            <button type="button" className="workbench-secondary-button" onClick={onAdjust}>要求调整</button>
            <button type="button" className="workbench-primary-button" disabled={confirming} onClick={(event) => onConfirm(event.currentTarget)}>{confirming ? "正在确认…" : "确认并运行"}</button>
          </div>
        </footer>
      ) : null}
    </section>
  );
}

function ConfirmDialog({
  proposal,
  taskRevision,
  open,
  pending,
  error,
  onCancel,
  onConfirm,
}: {
  proposal: ContractPlanProposal | null;
  taskRevision: number | null;
  open: boolean;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}): React.JSX.Element | null {
  const ref = useRef<HTMLDialogElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      dialog.showModal();
    }
    if (!open && dialog.open) {
      dialog.close();
      requestAnimationFrame(() => returnFocusRef.current?.focus());
    }
  }, [open]);
  if (!proposal || taskRevision === null) return null;
  return (
    <dialog ref={ref} className="master-confirm-dialog" aria-labelledby="master-confirm-title" onCancel={(event) => { event.preventDefault(); onCancel(); }}>
      <div className="workbench-drawer__header"><div><p className="workbench-eyebrow">最终确认</p><h2 id="master-confirm-title">启动计划 r{proposal.revision}</h2></div></div>
      <div className="master-confirm-dialog__body">
        <p>确认后将绑定当前计划、全部任务卡和主任务的准确修订，并启动满足业务门禁的实例。任一版本变化都会拒绝启动并要求重新审阅。</p>
        <dl><div><dt>主任务修订</dt><dd>r{taskRevision}</dd></div><div><dt>计划修订</dt><dd>r{proposal.revision}</dd></div><div><dt>任务卡</dt><dd>{proposal.execution_cards.length}</dd></div><div><dt>预计实例</dt><dd>{proposal.work_items.length}</dd></div></dl>
        <ul className="master-confirm-dialog__cards" aria-label="确认的任务卡修订">
          {proposal.execution_cards.map((card) => <li key={card.card_id}><span><code>{card.card_id}</code><small>实例 {card.instance_id}</small></span><strong>r{card.revision}</strong></li>)}
        </ul>
        <p className="master-confirm-dialog__warning"><Icon name="status" />点击“确认并启动”表示你已知晓本次运行可能产生创作服务费用；启动仍受预算和服务可用性检查。</p>
        {error ? <p className="workbench-inline-error" role="alert">{error}</p> : null}
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
  const [pauseMasterPolling, setPauseMasterPolling] = useState(false);
  const session = useQuery({
    ...masterSessionQuery(taskId),
    enabled: !pauseMasterPolling,
  });
  const startOperation = useQuery({
    ...latestStartOperationQuery(taskId),
    enabled: session.data?.latest_proposal?.status === "CONFIRMED",
  });
  const [content, setContent] = useState("");
  const [selectedAssets, setSelectedAssets] = useState<Set<string>>(() => new Set());
  const [confirmReview, setConfirmReview] = useState<{
    proposal: ContractPlanProposal;
    taskRevision: number;
  } | null>(null);
  const [editingCardId, setEditingCardId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const cardEditorTriggerRef = useRef<HTMLButtonElement | null>(null);
  const confirmTriggerRef = useRef<HTMLButtonElement | null>(null);
  const closeCardEditor = () => {
    setEditingCardId(null);
    requestAnimationFrame(() => cardEditorTriggerRef.current?.focus());
  };
  const closeConfirmReview = () => {
    setConfirmReview(null);
    requestAnimationFrame(() => confirmTriggerRef.current?.focus());
  };

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
      const proposal = confirmReview?.proposal;
      if (!proposal || !confirmReview) throw new Error("当前没有可确认计划。");
      return api.confirmPlanProposal(taskId, proposal.revision, {
        task_expected_revision: confirmReview.taskRevision,
        expected_card_revisions: Object.fromEntries(
          proposal.execution_cards.map((card) => [card.card_id, card.revision]),
        ),
        envelope: envelope(operationId("confirm_plan"), proposal.revision),
      });
    },
    onMutate: () => {
      setPauseMasterPolling(true);
    },
    onSuccess: (response) => {
      queryClient.setQueryData(masterSessionQuery(taskId).queryKey, response.session);
      void queryClient.invalidateQueries({ queryKey: taskHistoryQuery.queryKey });
      void queryClient.invalidateQueries({ queryKey: latestStartOperationQuery(taskId).queryKey });
      closeConfirmReview();
      setNotice("计划已确认，实例启动已排队；页面会持续同步结果。");
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === "REVISION_CONFLICT") {
        closeConfirmReview();
        setNotice("计划或任务已更新，本次未启动；请重新审阅最新版本。");
        void queryClient.invalidateQueries({ queryKey: masterSessionQuery(taskId).queryKey });
      }
    },
    onSettled: () => {
      setPauseMasterPolling(false);
    },
  });
  const reviseCard = useMutation({
    mutationFn: ({ card, editable }: { card: ProposalTaskCard; editable: EditableTaskCard }) => {
      const proposal = session.data?.latest_proposal;
      if (!proposal) throw new Error("当前没有可编辑计划。");
      return api.updatePlanTaskCard(taskId, proposal.revision, card.card_id, {
        ...editable,
        expected_proposal_revision: proposal.revision,
        expected_card_revision: card.revision,
        envelope: envelope(operationId("revise_task_card"), proposal.revision),
      });
    },
    onSuccess: (response) => {
      queryClient.setQueryData(masterSessionQuery(taskId).queryKey, response);
      const revised = response.latest_proposal;
      closeCardEditor();
      setNotice(revised ? `任务卡已保存；计划已更新为 r${revised.revision}，请重新审阅全部卡片。` : "任务卡修订已保存。");
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === "REVISION_CONFLICT") {
        closeCardEditor();
        setNotice("计划或任务卡已更新，本次编辑未覆盖新版本；请重新审阅后再修改。");
        void queryClient.invalidateQueries({ queryKey: masterSessionQuery(taskId).queryKey });
      }
    },
  });

  if (session.isPending) return <section className="workbench-page"><div className="workbench-intake-card" role="status">正在恢复 Master 永久线程…</div></section>;
  if (!session.data) {
    return <section className="workbench-page"><div className="workbench-intake-card"><p className="workbench-inline-error" role="alert">{session.error?.message ?? "无法读取 Master 线程。"}</p><button type="button" className="workbench-secondary-button" onClick={() => void session.refetch()}>重新读取</button></div></section>;
  }
  const data: MasterSessionResponse = session.data;
  const active = data.thread.active_run;
  const busy = active?.status === "SUBMITTING" || active?.status === "RUNNING";
  const proposal = data.latest_proposal;
  const editingCard = proposal?.execution_cards.find((card) => card.card_id === editingCardId) ?? null;
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
      {proposal?.status === "CONFIRMED" ? (
        <StartProgressBar operation={startOperation.data?.operation} />
      ) : null}
      {session.isError ? <div className="master-alert master-alert--error" role="alert"><Icon name="status" /><div><strong>后台同步暂时失败</strong><p>已保留当前页面数据，系统会继续重试；也可稍后手动刷新。</p></div></div> : null}
      {data.thread.last_error && !busy ? (
        <div className="master-alert master-alert--error" role="alert"><Icon name="status" /><div><strong>本次智能分析未完成</strong><p>任务内容和对话记录已保留。请稍后重新发送；若持续失败，请联系支持人员。</p></div></div>
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
          onConfirm={(trigger) => {
            confirmTriggerRef.current = trigger;
            setConfirmReview({ proposal, taskRevision: data.task_revision });
          }}
          onAdjust={() => {
            setContent(`请调整计划 r${proposal.revision}：`);
            requestAnimationFrame(() => composerRef.current?.focus());
          }}
          onEditCard={(card, trigger) => {
            reviseCard.reset();
            setNotice(null);
            cardEditorTriggerRef.current = trigger;
            setEditingCardId(card.card_id);
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
      </form>
      <TaskCardEditorDialog
        card={editingCard}
        assets={data.assets}
        open={editingCard !== null}
        pending={reviseCard.isPending}
        error={reviseCard.isError ? reviseCard.error.message : null}
        onCancel={() => { if (!reviseCard.isPending) closeCardEditor(); }}
        onSave={(editable) => { if (editingCard) reviseCard.mutate({ card: editingCard, editable }); }}
      />
      <ConfirmDialog proposal={confirmReview?.proposal ?? null} taskRevision={confirmReview?.taskRevision ?? null} open={confirmReview !== null} pending={confirm.isPending} error={confirm.isError ? confirm.error.message : null} onCancel={closeConfirmReview} onConfirm={() => confirm.mutate()} />
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
