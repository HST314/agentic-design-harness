import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, useParams } from "react-router-dom";
import type {
  CommandEnvelope,
  ConfirmPlanResponse,
  EditableTaskCard,
  InstanceStartProgress,
  MasterAssetReference,
  MasterSessionAsset,
  MasterSessionProposal,
  MasterSessionResponse,
  TaskIntakeMutationResponse,
  TaskIntakeResponse,
} from "../../api/client";
import { ApiError } from "../../api/client";
import {
  api,
  instanceStartQuery,
  masterSessionQuery,
  taskHistoryQuery,
  taskIntakeQuery,
} from "../../api/queries";
import type {
  ContractAgentInstance,
  ContractMasterMessage,
  ContractPlanProposal,
} from "../../api/generated-contracts";
import { ExpandableComposerTextarea } from "../../components/ExpandableComposerTextarea";
import { Icon } from "../../components/Icon";
import {
  LocalUploadList,
  MAX_TASK_FILES,
  TaskAssetFilePicker,
  formatBytes,
  validateTaskAssetFiles,
  type LocalUpload,
} from "../../components/TaskAssetUploads";
import { DisabledMasterComposer, PendingThinking, PendingUserMessage } from "./MasterChatBridge";
import { TaskIntakePage } from "../task-intake/TaskIntakePage";

const ACTOR_ID = "human_operator";
type ProposalTaskCard = ContractPlanProposal["execution_cards"][number];
type ExpectedDelivery = ProposalTaskCard["expected_deliveries"][number];

function agentLabel(agentType: ProposalTaskCard["agent_type"]): string {
  return agentType === "general" ? "通用" : agentType === "image" ? "图片" : "PPT";
}

function agentTaskTitle(agentType: ProposalTaskCard["agent_type"]): string {
  return agentType === "general" ? "通用任务" : agentType === "image" ? "图片设计任务" : "演示文稿设计任务";
}

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
  const role = {
    primary: "主视觉",
    supporting_visual: "辅助视觉",
    presentation: "演示文稿",
    design_note: "设计说明",
  }[delivery.role] ?? "交付文件";
  return `${kind} · ${role}`;
}

export function designerFacingMessage(message: ContractMasterMessage): string {
  if (message.role === "user") return message.content;
  if (message.kind === "plan_confirmation") return "计划已确认，可以开始执行各项设计任务。";
  return message.content
    .replace(/PlanProposal(?:\s*·?\s*r?\d+)?/gi, "执行计划")
    .replace(/TaskCard(?:\s*·?\s*r?\d+)?/gi, "任务卡")
    .replace(/计划\s*r\d+/gi, "计划")
    .replace(/任务卡\s*r\d+/gi, "任务卡")
    .replace(/已保存为\s*r\d+/gi, "已保存")
    .replace(/已更新为\s*r\d+/gi, "已更新")
    .replace(/实例\s+instance_[A-Za-z0-9_-]+/gi, "该子任务")
    .replace(/\b(?:work|stage|instance|card|proposal|attempt|message|task)_[A-Za-z0-9_-]+\b/gi, "")
    .replace(/\b[A-Za-z]+(?:_[A-Za-z0-9]+)+\b/g, "")
    .replace(/\br\d+\b/gi, "新版本")
    .replace(/\s+([，。；：])/g, "$1")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}

const startStageLabels: Record<InstanceStartProgress["state"], string> = {
  PENDING: "已受理",
  PREPARING: "准备运行环境",
  PROCESS_STARTING: "启动进程",
  AGENT_STARTING: "健康检查",
  RUNNING: "已就绪",
};

export function startStageLabel(progress: InstanceStartProgress | null | undefined): string {
  if (!progress) return startStageLabels.PENDING;
  return startStageLabels[progress.state] ?? startStageLabels.PENDING;
}

export function isPptLaunchBlocked(
  card: ProposalTaskCard,
  unfinishedImageInstanceIds: readonly string[],
): boolean {
  return card.agent_type === "ppt" && unfinishedImageInstanceIds.length > 0;
}

export type MasterTimelineItem =
  | { kind: "message"; message: ContractMasterMessage }
  | { kind: "proposal"; proposal: MasterSessionProposal };

export function buildMasterTimeline(
  messages: ContractMasterMessage[],
  proposals: MasterSessionProposal[],
): MasterTimelineItem[] {
  const messageIds = new Set(messages.map((message) => message.message_id));
  const byMessage = new Map<string, MasterSessionProposal[]>();
  const orphans: MasterSessionProposal[] = [];
  for (const proposal of proposals) {
    if (proposal.message_id && messageIds.has(proposal.message_id)) {
      const list = byMessage.get(proposal.message_id) ?? [];
      list.push(proposal);
      byMessage.set(proposal.message_id, list);
    } else {
      orphans.push(proposal);
    }
  }
  const items: MasterTimelineItem[] = [];
  for (const message of messages) {
    items.push({ kind: "message", message });
    for (const proposal of byMessage.get(message.message_id) ?? []) {
      items.push({ kind: "proposal", proposal });
    }
  }
  for (const proposal of orphans) {
    items.push({ kind: "proposal", proposal });
  }
  return items;
}

export function TaskTabs({ taskId, trailing }: { taskId: string; trailing?: React.ReactNode }): React.JSX.Element {
  const base = `/tasks/${encodeURIComponent(taskId)}`;
  return (
    <nav className="workbench-task-tabs" aria-label="任务工作区">
      <NavLink to={`${base}/master`}><Icon name="message" />Master</NavLink>
      <NavLink to={`${base}/board`}><Icon name="board" />看板</NavLink>
      <NavLink to={`${base}/plan`}><Icon name="plan" />计划</NavLink>
      <NavLink to={`${base}/deliveries`}><Icon name="file-check" />交付</NavLink>
      {trailing ? <span className="workbench-task-tabs__trailing">{trailing}</span> : null}
    </nav>
  );
}

function MessageCard({ message }: { message: ContractMasterMessage }): React.JSX.Element {
  const pending = message.message_id.startsWith("local_");
  return (
    <article
      className={`master-message master-message--${message.role} master-message--${message.kind}${pending ? " master-message--pending" : ""}`}
      aria-label={pending ? "用户消息，正在发送" : `${messageLabel[message.kind]}，${formatTime(message.created_at)}`}
    >
      <header>
        <span>{messageLabel[message.kind]}</span>
        {pending ? <span>正在发送</span> : <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>}
      </header>
      <p>{designerFacingMessage(message)}</p>
      {message.asset_refs.length ? (
        <ul className="master-message__assets" aria-label="引用资源">
          <li><Icon name="file-check" />已引用 {message.asset_refs.length} 个任务资源</li>
        </ul>
      ) : null}
    </article>
  );
}

function MasterThinking(): React.JSX.Element {
  return (
    <div className="master-thinking" role="status" aria-live="polite">
      <span className="master-mini-card__spinner" aria-hidden="true" />
      <span><strong>Master 正在分析与思考</strong><small>新的回复与任务卡会在这里实时出现</small></span>
    </div>
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
  const fallbackTitle = agentTaskTitle(card.agent_type);
  const parameterItems = card.agent_type === "image"
    ? [
      ["画幅", textParameter(card, "aspect_ratio") || "由专业助手决定"],
      ["候选数量", numberParameter(card, "variants") || "默认"],
      ["使用场景", textParameter(card, "usage_context") || "未填写"],
      [
        "品类版本",
        textParameter(card, "category_id") ? "已指定" : "未指定",
      ],
    ]
    : card.agent_type === "ppt" ? [
      ["页数", numberParameter(card, "slide_count") || "默认"],
      ["计划资产角色", textParameter(card, "planned_asset_role") || "未指定"],
    ] : [["工具边界", "共享文件夹读写"]];
  return (
    <article className="master-task-card" aria-labelledby={`task-card-${card.card_id}`}>
      <header>
        <div>
          <span className="master-agent-chip">{agentLabel(card.agent_type)}</span>
        </div>
        {editable ? (
          <button
            type="button"
            className="workbench-secondary-button"
            aria-label={`编辑任务卡 ${workItem?.title ?? fallbackTitle}`}
            onClick={(event) => onEdit(event.currentTarget)}
          >
            <Icon name="rename" />编辑任务卡
          </button>
        ) : null}
      </header>
      <h3 id={`task-card-${card.card_id}`}>{workItem?.title ?? fallbackTitle}</h3>
      <p className="master-task-card__objective">{card.objective}</p>
      <dl className="master-task-card__parameters">
        {parameterItems.map(([label, value]) => (
          <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
        ))}
        <div><dt>依赖</dt><dd>{workItem?.depends_on.length ? `${workItem.depends_on.length} 项前置任务` : "无前置依赖"}</dd></div>
      </dl>
      <div className="master-task-card__details">
        <section aria-label="执行指令">
          <h4>执行指令</h4>
          {card.instructions.length ? <ul>{card.instructions.map((item) => <li key={item}>{item}</li>)}</ul> : <p>无附加指令</p>}
        </section>
        <section aria-label="输入资产">
          <h4>输入资产</h4>
          {card.input_assets.length ? <p>已引用 {card.input_assets.length} 个任务资源</p> : <p>无输入资产</p>}
        </section>
        <section aria-label="输出要求">
          <h4>输出要求</h4>
          <ul>{card.expected_deliveries.map((item) => <li key={`${item.kind}-${item.role}`}>{deliveryLabel(item)}{item.required ? " · 必需" : " · 可选"}</li>)}</ul>
        </section>
      </div>
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
        filename: "未命名任务资源",
        description: "计划引用资产",
        mime_type: "application/octet-stream",
        size_bytes: 0,
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
            : card.agent_type === "ppt" ? {
              ...(slideCount === "" ? {} : { slide_count: slideCount }),
              ...(plannedAssetRole.trim() ? { planned_asset_role: plannedAssetRole.trim() } : {}),
            } : {};
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
          <div><p className="workbench-eyebrow">任务设置</p><h2 id="master-card-editor-title">编辑任务卡</h2></div>
          <button type="button" className="workbench-icon-button" aria-label="关闭任务卡编辑" disabled={pending} onClick={onCancel}><Icon name="close" /></button>
        </header>
        <div className="master-card-editor__body">
          <p className="master-card-editor__notice">保存后会生成新的任务设置和执行计划，当前版本仍保留在历史记录中。</p>
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
                  <label><span>文件格式（逗号分隔）</span><input required value={delivery.accepted_mime_types.join(", ")} onChange={(event) => setDeliveries((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, accepted_mime_types: event.currentTarget.value.split(",") } : item))} /></label>
                  <label className="master-card-editor__checkbox"><input type="checkbox" checked={delivery.required} onChange={(event) => setDeliveries((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, required: event.currentTarget.checked } : item))} /><span>必需交付</span></label>
                  <button type="button" className="workbench-text-button" disabled={deliveries.length === 1} onClick={() => setDeliveries((current) => current.filter((_, itemIndex) => itemIndex !== index))}>移除此项</button>
                </fieldset>
              ))}
              <button type="button" className="workbench-secondary-button" onClick={() => setDeliveries((current) => [...current, card.agent_type === "general" ? { kind: "document", role: "output", required: false, accepted_mime_types: ["text/plain"] } : { kind: card.agent_type === "image" ? "image" : "presentation", role: card.agent_type === "image" ? "supporting_visual" : "presentation", required: false, accepted_mime_types: [card.agent_type === "image" ? "image/png" : "application/pdf"] }])}>添加交付项</button>
            </div>
          </fieldset>
          {card.agent_type === "image" ? (
            <fieldset>
              <legend>图片设置</legend>
              <div className="master-card-editor__grid">
                <label><span>画幅</span><input pattern="[1-9][0-9]{0,3}:[1-9][0-9]{0,3}" placeholder="16:9" value={aspectRatio} onChange={(event) => setAspectRatio(event.currentTarget.value)} /></label>
                <label><span>候选数量</span><input type="number" min={1} max={64} value={variants} onChange={(event) => setVariants(event.currentTarget.value ? event.currentTarget.valueAsNumber : "")} /></label>
                <label className="master-card-editor__wide"><span>使用场景</span><input required maxLength={10_000} value={usageContext} onChange={(event) => setUsageContext(event.currentTarget.value)} /></label>
                <label><span>设计品类</span><input maxLength={128} value={categoryId} onChange={(event) => setCategoryId(event.currentTarget.value)} /></label>
                <label><span>品类版本</span><input maxLength={64} value={categoryVersion} onChange={(event) => setCategoryVersion(event.currentTarget.value)} /></label>
              </div>
            </fieldset>
          ) : card.agent_type === "ppt" ? (
            <fieldset>
              <legend>PPT 参数</legend>
              <div className="master-card-editor__grid">
                <label><span>页数</span><input type="number" min={1} max={500} value={slideCount} onChange={(event) => setSlideCount(event.currentTarget.value ? event.currentTarget.valueAsNumber : "")} /></label>
                <label><span>计划资产角色</span><input maxLength={128} value={plannedAssetRole} onChange={(event) => setPlannedAssetRole(event.currentTarget.value)} /></label>
              </div>
            </fieldset>
          ) : null}
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

function MiniTaskCard({
  card,
  title,
  proposalStatus,
  instanceStatus,
  upstreamBlockedCount,
  launchPending,
  launchBlocked,
  onLaunch,
  onShowDetail,
}: {
  card: ProposalTaskCard;
  title: string;
  proposalStatus: ContractPlanProposal["status"];
  instanceStatus: ContractAgentInstance["status"] | undefined;
  upstreamBlockedCount: number;
  launchPending: boolean;
  launchBlocked: boolean;
  onLaunch: () => void;
  onShowDetail: (trigger: HTMLButtonElement) => void;
}): React.JSX.Element {
  const queryClient = useQueryClient();
  const start = useQuery(instanceStartQuery(
    card.instance_id,
    proposalStatus !== "SUPERSEDED" && instanceStatus !== undefined,
  ));
  const detail = start.data;
  const retry = useMutation({
    mutationFn: () => {
      const startOperationId = detail?.start_operation_id;
      const taskRevision = detail?.task_revision;
      if (!startOperationId || taskRevision === undefined) {
        throw new Error("当前没有可重试的启动操作。");
      }
      return api.retryStartOperation(
        startOperationId,
        envelope(operationId("retry_start"), taskRevision),
      );
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: instanceStartQuery(card.instance_id, true).queryKey });
    },
  });

  const currentStatus = detail?.instance.status ?? instanceStatus;
  const starting = launchPending || currentStatus === "STARTING" || Boolean(detail?.start_in_progress) || retry.isPending;
  const instanceReady = currentStatus === "RUNNING" || currentStatus === "WAITING_APPROVAL";
  const ready = !starting && (instanceReady || detail?.start_progress?.state === "RUNNING");
  const failed = !starting && !ready && (
    currentStatus === "FAILED_TO_START" || Boolean(detail?.start_progress?.last_error)
  );
  const unavailable = !starting && !ready && !failed && currentStatus === "UNAVAILABLE";
  const failureMessage = detail?.start_progress?.last_error?.message ?? detail?.instance.start_failure?.message ?? "实例启动失败。";
  const retryable = failed && Boolean(detail?.start_retry_allowed && detail.start_operation_id);

  let action: React.ReactNode;
  if (proposalStatus === "SUPERSEDED") {
    action = <span className="master-mini-card__state">已被替换</span>;
  } else if (starting) {
    action = (
      <span className="master-mini-card__state master-mini-card__state--busy" role="status">
        <span className="master-mini-card__spinner" aria-hidden="true" />
        {launchPending && !detail ? startStageLabels.PENDING : startStageLabel(detail?.start_progress)}
      </span>
    );
  } else if (ready) {
    action = <span className="master-mini-card__state master-mini-card__state--ready">已就绪</span>;
  } else if (upstreamBlockedCount > 0) {
    action = (
      <span className="master-mini-card__state master-mini-card__state--blocked">
        <strong>待上游就绪</strong>
        <small>{upstreamBlockedCount} 个图片任务状态设为已完成后激活</small>
      </span>
    );
  } else if (failed) {
    action = (
      <span className="master-mini-card__state master-mini-card__state--failed" title={failureMessage}>
        启动失败
        {retryable ? (
          <button type="button" className="workbench-text-button" onClick={() => retry.mutate()}>重试</button>
        ) : null}
      </span>
    );
  } else if (unavailable) {
    action = <span className="master-mini-card__state">待上游就绪</span>;
  } else {
    action = (
      <button
        type="button"
        className="workbench-primary-button master-mini-card__launch"
        aria-label={`启动 ${title}`}
        disabled={launchBlocked}
        onClick={onLaunch}
      >
        启动
      </button>
    );
  }

  return (
    <article className="master-mini-card">
      <div className="master-mini-card__lead">
        <span className="master-agent-chip">{agentLabel(card.agent_type)}</span>
        <h4>{title}</h4>
      </div>
      <div className="master-mini-card__actions">
        {action}
        <button
          type="button"
          className="workbench-secondary-button"
          aria-label={`查看详情 ${title}`}
          onClick={(event) => onShowDetail(event.currentTarget)}
        >
          查看详情
        </button>
      </div>
    </article>
  );
}

function ProposalCardGroup({
  proposal,
  instanceStatuses,
  unfinishedImageInstanceIds,
  launchingCardId,
  onLaunchCard,
  onShowDetail,
  onAdjust,
}: {
  proposal: MasterSessionProposal;
  instanceStatuses: MasterSessionResponse["instance_statuses"];
  unfinishedImageInstanceIds: MasterSessionResponse["unfinished_image_instance_ids"];
  launchingCardId: string | null;
  onLaunchCard: (proposal: MasterSessionProposal, card: ProposalTaskCard) => void;
  onShowDetail: (proposal: MasterSessionProposal, card: ProposalTaskCard, trigger: HTMLButtonElement) => void;
  onAdjust: (proposal: MasterSessionProposal) => void;
}): React.JSX.Element {
  const titleByCardId = new Map(
    proposal.work_items.flatMap((item) => item.task_card_ids.map((cardId) => [cardId, item.title] as const)),
  );
  const planLabel = proposal.status === "SUPERSEDED" ? "历史执行计划" : "当前执行计划";
  const singleCard = proposal.execution_cards.length === 1;
  return (
    <section className={`master-plan-cards${singleCard ? " master-plan-cards--single" : ""}`} aria-label={`${planLabel}任务卡`}>
      <header className="master-plan-cards__header">
        <p className="workbench-eyebrow">{planLabel}</p>
        <div className="master-plan-cards__meta">
          <span className={`master-proposal__status master-proposal__status--${proposal.status.toLowerCase()}`}>
            {proposal.status === "PENDING_CONFIRMATION" ? "待确认" : proposal.status === "CONFIRMED" ? "已确认" : "已被替换"}
          </span>
          {proposal.status === "PENDING_CONFIRMATION" ? (
            <button type="button" className="workbench-text-button" onClick={() => onAdjust(proposal)}>要求调整</button>
          ) : null}
        </div>
      </header>
      <div className="master-plan-cards__grid">
        {proposal.execution_cards.map((card) => (
          <MiniTaskCard
            key={card.card_id}
            card={card}
            title={titleByCardId.get(card.card_id) ?? agentTaskTitle(card.agent_type)}
            proposalStatus={proposal.status}
            instanceStatus={instanceStatuses[card.instance_id]}
            upstreamBlockedCount={
              isPptLaunchBlocked(card, unfinishedImageInstanceIds)
                ? unfinishedImageInstanceIds.length
                : 0
            }
            launchPending={launchingCardId === card.card_id}
            launchBlocked={launchingCardId !== null}
            onLaunch={() => onLaunchCard(proposal, card)}
            onShowDetail={(trigger) => onShowDetail(proposal, card, trigger)}
          />
        ))}
      </div>
    </section>
  );
}

function TaskCardDetailDialog({
  detail,
  editable,
  onClose,
  onEdit,
}: {
  detail: { proposal: MasterSessionProposal; card: ProposalTaskCard } | null;
  editable: boolean;
  onClose: () => void;
  onEdit: () => void;
}): React.JSX.Element | null {
  const ref = useRef<HTMLDialogElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const open = detail !== null;

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

  if (!detail) return null;
  const workItem = detail.proposal.work_items.find((item) => item.task_card_ids.includes(detail.card.card_id));
  return (
    <dialog
      ref={ref}
      className="master-card-detail"
      aria-labelledby="master-card-detail-title"
      onCancel={(event) => { event.preventDefault(); onClose(); }}
    >
      <header className="workbench-drawer__header">
        <div><p className="workbench-eyebrow">执行计划</p><h2 id="master-card-detail-title">任务卡详情</h2></div>
        <button type="button" className="workbench-icon-button" aria-label="关闭任务卡详情" onClick={onClose}><Icon name="close" /></button>
      </header>
      <div className="master-card-detail__body">
        <TaskCardReview
          card={detail.card}
          workItem={workItem}
          editable={editable}
          onEdit={() => onEdit()}
        />
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
  const intake = useQuery(taskIntakeQuery(taskId));
  const [content, setContent] = useState("");
  const [selectedAssets, setSelectedAssets] = useState<Set<string>>(() => new Set());
  const [detail, setDetail] = useState<{ proposal: MasterSessionProposal; card: ProposalTaskCard } | null>(null);
  const [editingCard, setEditingCard] = useState<{ proposal: MasterSessionProposal; card: ProposalTaskCard } | null>(null);
  const [launchingCardId, setLaunchingCardId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pageAlert, setPageAlert] = useState<string | null>(null);
  const [uploads, setUploads] = useState<LocalUpload[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const threadEndRef = useRef<HTMLDivElement>(null);
  const uploadControllers = useRef(new Map<string, AbortController>());
  const intakeRevisionRef = useRef(0);
  const cardEditorTriggerRef = useRef<HTMLButtonElement | null>(null);
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null);
  const closeCardEditor = () => {
    setEditingCard(null);
    requestAnimationFrame(() => cardEditorTriggerRef.current?.focus());
  };
  const closeDetail = () => {
    setDetail(null);
    requestAnimationFrame(() => detailTriggerRef.current?.focus());
  };

  useEffect(() => {
    if (intake.data) {
      intakeRevisionRef.current = Math.max(intakeRevisionRef.current, intake.data.intake_revision);
    }
  }, [intake.data]);

  useEffect(() => () => {
    uploadControllers.current.forEach((controller) => controller.abort());
  }, []);

  const mergeUploadResult = useCallback((response: TaskIntakeMutationResponse): void => {
    intakeRevisionRef.current = Math.max(intakeRevisionRef.current, response.intake_revision);
    queryClient.setQueryData<TaskIntakeResponse>(taskIntakeQuery(taskId).queryKey, (current) => {
      if (!current) return current;
      const assets = response.asset && !current.assets.some((asset) => asset.asset_id === response.asset?.asset_id)
        ? [...current.assets, response.asset]
        : current.assets;
      return response.intake_revision < current.intake_revision
        ? { ...current, assets }
        : { ...current, intake: response.intake, intake_revision: response.intake_revision, assets };
    });
    if (response.asset) {
      const asset = response.asset;
      const assetId = asset.asset_id;
      queryClient.setQueryData<MasterSessionResponse>(masterSessionQuery(taskId).queryKey, (current) => {
        if (!current || current.assets.some((item) => item.asset_id === assetId)) return current;
        return {
          ...current,
          assets: [...current.assets, {
            asset_id: assetId,
            filename: asset.filename,
            description: asset.description,
            mime_type: asset.mime_type,
            size_bytes: asset.size_bytes,
            manifest_relpath: `inputs/manifests/${assetId}.json`,
          }],
        };
      });
      setSelectedAssets((current) => new Set(current).add(assetId));
    }
    void queryClient.invalidateQueries({ queryKey: masterSessionQuery(taskId).queryKey });
  }, [queryClient, taskId]);

  const uploadOne = useCallback((item: LocalUpload): void => {
    if (uploadControllers.current.has(item.id)) return;
    const controller = new AbortController();
    uploadControllers.current.set(item.id, controller);
    void api.uploadTaskAsset(
      taskId,
      {
        file: item.file,
        declaredMimeType: item.declaredMimeType,
        description: item.description,
        envelope: envelope(item.id, intakeRevisionRef.current),
      },
      (progress) => setUploads((current) => current.map((entry) => entry.id === item.id ? { ...entry, progress } : entry)),
      controller.signal,
    ).then((response) => {
      uploadControllers.current.delete(item.id);
      mergeUploadResult(response);
      setUploads((current) => current.filter((entry) => entry.id !== item.id));
    }).catch((error: unknown) => {
      uploadControllers.current.delete(item.id);
      if (error instanceof DOMException && error.name === "AbortError") {
        setUploads((current) => current.filter((entry) => entry.id !== item.id));
        return;
      }
      const message = error instanceof Error ? error.message : "上传失败，请重试。";
      setUploads((current) => current.map((entry) => entry.id === item.id ? { ...entry, status: "failed", error: message, progress: 0 } : entry));
      void queryClient.invalidateQueries({ queryKey: taskIntakeQuery(taskId).queryKey });
    });
  }, [mergeUploadResult, queryClient, taskId]);

  useEffect(() => {
    if (!session.data || !intake.data) return;
    const uploading = uploads.filter((item) => item.status === "uploading").length;
    const next = uploads.filter((item) => item.status === "queued").slice(0, Math.max(0, 3 - uploading));
    if (!next.length) return;
    const ids = new Set(next.map((item) => item.id));
    setUploads((current) => current.map((item) => ids.has(item.id) ? { ...item, status: "uploading", error: null } : item));
    next.forEach(uploadOne);
  }, [intake.data, session.data, uploadOne, uploads]);

  const append = useMutation({
    mutationFn: ({ text, refs }: { text: string; refs: MasterAssetReference[] }) => api.appendMasterMessage(taskId, {
      content: text,
      asset_refs: refs,
      envelope: envelope(operationId("master_message"), session.data?.thread_revision ?? 0),
    }),
    onMutate: async ({ text, refs }) => {
      const queryKey = masterSessionQuery(taskId).queryKey;
      await queryClient.cancelQueries({ queryKey });
      const previous = queryClient.getQueryData<MasterSessionResponse>(queryKey);
      const optimisticId = `local_${operationId("message")}`;
      if (previous) {
        const sequence = previous.messages.reduce((highest, message) => Math.max(highest, message.sequence), 0) + 1;
        const optimisticMessage: ContractMasterMessage = {
          schema_version: "1.0",
          message_id: optimisticId,
          task_id: taskId,
          sequence,
          role: "user",
          kind: "text",
          content: text,
          asset_refs: refs,
          created_at: new Date().toISOString(),
        };
        queryClient.setQueryData<MasterSessionResponse>(queryKey, {
          ...previous,
          messages: [...previous.messages, optimisticMessage],
        });
      }
      setContent("");
      setSelectedAssets(new Set());
      return { previous, text, refs };
    },
    onSuccess: (response) => {
      queryClient.setQueryData(masterSessionQuery(taskId).queryKey, response);
      setNotice("消息已保存，Master 正在处理。");
    },
    onError: (error, _variables, context) => {
      if (context?.previous) {
        queryClient.setQueryData(masterSessionQuery(taskId).queryKey, context.previous);
      }
      setContent(context?.text ?? "");
      setSelectedAssets(new Set(context?.refs.map((asset) => asset.asset_id) ?? []));
      if (error instanceof ApiError && error.code === "REVISION_CONFLICT") {
        setNotice("线程已更新，已重新读取；请确认内容后再次发送。");
        void queryClient.invalidateQueries({ queryKey: masterSessionQuery(taskId).queryKey });
      }
    },
  });
  const launch = useMutation({
    mutationFn: async ({ proposal, card }: { proposal: MasterSessionProposal; card: ProposalTaskCard }): Promise<{ confirmation: ConfirmPlanResponse } | { confirmation: null }> => {
      const taskRevision = session.data?.task_revision;
      if (taskRevision === undefined) throw new Error("任务信息尚未载入。");
      if (proposal.status === "PENDING_CONFIRMATION") {
        const confirmation = await api.confirmPlanProposal(taskId, proposal.revision, {
          task_expected_revision: taskRevision,
          expected_card_revisions: Object.fromEntries(
            proposal.execution_cards.map((item) => [item.card_id, item.revision]),
          ),
          envelope: envelope(operationId("confirm_plan"), proposal.revision),
          instance_ids: [card.instance_id],
        });
        return { confirmation };
      }
      const operation = operationId("start_card");
      await api.startInstance(
        card.instance_id,
        operation,
        envelope(operation, taskRevision),
      );
      return { confirmation: null };
    },
    onMutate: ({ card }) => {
      setLaunchingCardId(card.card_id);
      setPageAlert(null);
      setPauseMasterPolling(true);
    },
    onSuccess: (result, { card }) => {
      if (result.confirmation) {
        queryClient.setQueryData(masterSessionQuery(taskId).queryKey, result.confirmation.session);
        void queryClient.invalidateQueries({ queryKey: taskHistoryQuery.queryKey });
        setNotice("计划已确认，该子任务正在启动；其余子任务可继续单独启动。");
      } else {
        setNotice("已受理，该子任务正在启动。");
      }
      void queryClient.invalidateQueries({ queryKey: instanceStartQuery(card.instance_id, true).queryKey });
    },
    onError: (error) => {
      setPageAlert(error instanceof Error ? error.message : "子任务启动失败，请重试。");
      if (error instanceof ApiError && (error.code === "REVISION_CONFLICT" || error.code === "INVALID_STATE_TRANSITION")) {
        void queryClient.invalidateQueries({ queryKey: masterSessionQuery(taskId).queryKey });
      }
    },
    onSettled: () => {
      setLaunchingCardId(null);
      setPauseMasterPolling(false);
    },
  });
  const reviseCard = useMutation({
    mutationFn: ({ editable }: { editable: EditableTaskCard }) => {
      if (!editingCard) throw new Error("当前没有可编辑计划。");
      const { proposal, card } = editingCard;
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
      setNotice(revised ? "任务卡已保存；计划已更新，可继续启动未启动任务。" : "任务卡更新已保存。");
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === "REVISION_CONFLICT") {
        closeCardEditor();
        setNotice("计划或任务卡已更新，本次编辑未覆盖新版本；请重新审阅后再修改。");
        void queryClient.invalidateQueries({ queryKey: masterSessionQuery(taskId).queryKey });
      }
    },
  });

  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: "auto", block: "nearest" });
  }, [
    append.isPending,
    session.data?.messages.length,
    session.data?.proposals.length,
    session.data?.thread.active_run?.status,
  ]);

  if (session.isPending) {
    const firstPrompt = intake.data?.intake.prompt ?? "";
    return (
      <section className="workbench-page master-workspace" aria-label="正在恢复 Master 线程">
        <TaskTabs taskId={taskId} />
        <div className="master-thread" role="log" aria-live="polite" aria-label="Master 消息记录">
          {firstPrompt ? <PendingUserMessage prompt={firstPrompt} attachmentCount={intake.data?.assets.length ?? 0} /> : null}
          <PendingThinking title="正在恢复 Master 线程" detail="即将载入完整对话记录。" />
        </div>
        <DisabledMasterComposer placeholder="正在恢复 Master 线程…" />
      </section>
    );
  }
  if (!session.data) {
    return <section className="workbench-page"><div className="workbench-intake-card"><p className="workbench-inline-error" role="alert">{session.error?.message ?? "无法读取 Master 线程。"}</p><button type="button" className="workbench-secondary-button" onClick={() => void session.refetch()}>重新读取</button></div></section>;
  }
  const data: MasterSessionResponse = session.data;
  const active = data.thread.active_run;
  const busy = active?.status === "SUBMITTING" || active?.status === "RUNNING";
  const activeUploads = uploads.some((item) => item.status === "queued" || item.status === "uploading");
  const failedUploads = uploads.some((item) => item.status === "failed");
  const serverBytes = data.assets.reduce((sum, asset) => sum + asset.size_bytes, 0);
  const localBytes = uploads.reduce((sum, item) => sum + item.file.size, 0);
  const addFiles = (files: File[]): void => {
    const result = validateTaskAssetFiles(files, data.assets.length + uploads.length, serverBytes + localBytes);
    setFileError(result.error);
    if (result.uploads.length) setUploads((current) => [...current, ...result.uploads]);
  };
  const timeline = buildMasterTimeline(data.messages, data.proposals);
  const refs = data.assets
    .filter((asset) => selectedAssets.has(asset.asset_id))
    .map(({ asset_id, manifest_relpath }) => ({ asset_id, manifest_relpath }));
  const sendMessage = (): void => {
    const text = content.trim();
    if (!text || busy || append.isPending || activeUploads || failedUploads) return;
    setNotice(null);
    append.mutate({ text, refs });
  };

  return (
    <section className="workbench-page master-workspace" aria-labelledby="master-title">
      <h1 id="master-title" className="sr-only">{data.task.title}</h1>
      <TaskTabs taskId={taskId} />
      {pageAlert ? <div className="master-alert master-alert--error" role="alert"><Icon name="status" /><div><strong>子任务启动未完成</strong><p>{pageAlert}</p></div></div> : null}
      {session.isError ? <div className="master-alert master-alert--error" role="alert"><Icon name="status" /><div><strong>后台同步暂时失败</strong><p>已保留当前页面数据，系统会继续重试；也可稍后手动刷新。</p></div></div> : null}
      {data.thread.last_error && !busy ? (
        <div className="master-alert master-alert--error" role="alert"><Icon name="status" /><div><strong>本次智能分析未完成</strong><p>任务内容和对话记录已保留。请稍后重新发送；若持续失败，请联系支持人员。</p></div></div>
      ) : null}
      <div className="master-thread" role="log" aria-live="polite" aria-label="Master 消息记录">
        {timeline.length ? timeline.map((item) => item.kind === "message" ? (
          <MessageCard key={`message-${item.message.role}-${item.message.sequence}`} message={item.message} />
        ) : (
          <ProposalCardGroup
            key={`proposal-${item.proposal.proposal_id}-r${item.proposal.revision}`}
            proposal={item.proposal}
            instanceStatuses={data.instance_statuses ?? {}}
            unfinishedImageInstanceIds={data.unfinished_image_instance_ids}
            launchingCardId={launchingCardId}
            onLaunchCard={(proposal, card) => {
              launch.reset();
              setNotice(null);
              launch.mutate({ proposal, card });
            }}
            onShowDetail={(proposal, card, trigger) => {
              reviseCard.reset();
              detailTriggerRef.current = trigger;
              setDetail({ proposal, card });
            }}
            onAdjust={(proposal) => {
              setContent("请调整当前计划：");
              requestAnimationFrame(() => composerRef.current?.focus());
            }}
          />
        )) : <p className="master-thread__empty">尚无消息。发送目标或补充要求以开始规划。</p>}
        {append.isPending || busy ? <MasterThinking /> : null}
        <div ref={threadEndRef} aria-hidden="true" />
      </div>
      <form
        className="master-composer"
        onSubmit={(event) => {
          event.preventDefault();
          sendMessage();
        }}
      >
        <ExpandableComposerTextarea
          id="master-message"
          label="发送给 Master"
          textareaRef={composerRef}
          value={content}
          placeholder="补充目标、回答澄清，或说明需要调整的计划内容…"
          onChange={(event) => setContent(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              sendMessage();
            }
          }}
        />
        <div className="master-composer__asset-tools">
          <TaskAssetFilePicker onFiles={addFiles} disabled={data.assets.length + uploads.length >= MAX_TASK_FILES} />
          <span>{data.assets.length + uploads.length} / 20 个 · {formatBytes(serverBytes + localBytes)} / 200 MiB</span>
        </div>
        {fileError ? <p className="workbench-inline-error" role="alert">{fileError}</p> : null}
        <LocalUploadList
          items={uploads}
          onDescription={(id, value) => setUploads((current) => current.map((item) => item.id === id ? { ...item, description: value } : item))}
          onCancel={(id) => {
            uploadControllers.current.get(id)?.abort();
            if (!uploadControllers.current.has(id)) setUploads((current) => current.filter((item) => item.id !== id));
          }}
          onRetry={(id) => setUploads((current) => current.map((item) => item.id === id ? { ...item, status: "queued", error: null } : item))}
        />
        {data.assets.length ? (
          <fieldset><legend>引用任务资源</legend><div>{data.assets.map((asset) => <label key={asset.asset_id}><input type="checkbox" checked={selectedAssets.has(asset.asset_id)} onChange={(event) => { const checked = event.currentTarget.checked; setSelectedAssets((current) => { const next = new Set(current); if (checked) next.add(asset.asset_id); else next.delete(asset.asset_id); return next; }); }} /><Icon name="file-check" /><span><strong>{asset.filename}</strong>{asset.description ? <small>{asset.description}</small> : null}</span></label>)}</div></fieldset>
        ) : null}
        <footer><span>已输入 {content.length.toLocaleString("zh-CN")} 字 · Enter 发送，Shift + Enter 换行</span><button type="submit" className="workbench-primary-button" aria-label="发送 Master 消息" disabled={!content.trim() || busy || append.isPending || activeUploads || failedUploads}>{append.isPending ? "正在保存…" : activeUploads ? "等待附件上传" : failedUploads ? "处理失败附件" : busy ? "等待 Master 完成" : "发送消息"}</button></footer>
        {failedUploads ? <p className="workbench-inline-error" role="alert">请重试或移除失败文件后再发送消息。</p> : null}
        {notice ? <p className="master-composer__notice" role="status">{notice}</p> : null}
        {append.isError ? <p className="workbench-inline-error" role="alert">{append.error.message}</p> : null}
      </form>
      <TaskCardEditorDialog
        card={editingCard?.card ?? null}
        assets={data.assets}
        open={editingCard !== null}
        pending={reviseCard.isPending}
        error={reviseCard.isError ? reviseCard.error.message : null}
        onCancel={() => { if (!reviseCard.isPending) closeCardEditor(); }}
        onSave={(editable) => reviseCard.mutate({ editable })}
      />
      <TaskCardDetailDialog
        detail={detail}
        editable={Boolean(
          detail
          && detail.proposal.revision === data.thread.latest_proposal_revision
          && (data.editable_card_ids ?? []).includes(detail.card.card_id)
        )}
        onClose={closeDetail}
        onEdit={() => {
          if (!detail) return;
          cardEditorTriggerRef.current = detailTriggerRef.current;
          setDetail(null);
          setEditingCard(detail);
        }}
      />
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
  return <MasterWorkspace key={taskId} taskId={taskId} />;
}
