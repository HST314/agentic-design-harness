import type {
  ContractAgentInstance,
  ContractApprovalRequest,
  ContractAssetManifest,
  ContractInboxItem,
  ContractMainTask,
  ContractMasterMessage,
  ContractMasterThread,
  ContractPlanProposal,
  ContractStage,
  ContractTaskIntake,
  ContractTaskNavigationMetadata,
  ContractTokenUsageEvent,
  ContractWorkItemProjection,
} from "./generated-contracts";

export interface HealthResponse {
  status: "ok";
  version: string;
}

export interface ReadyResponse {
  status: "ready" | "degraded" | "not_ready";
  disabled_adapters?: string[];
}

export interface TaskSummary {
  task_id: string;
  status: string;
  title: string;
  updated_at: string;
  revision: number;
  goal?: string;
  start_policy?: ContractMainTask["start_policy"];
  stage_count?: number;
  instance_count?: number;
  instance_status_counts?: Record<string, number>;
  total_tokens?: number;
  usage_completeness?: "COMPLETE" | "PARTIAL" | "NOT_REPORTED";
  has_unavailable_ppt?: boolean;
  latest_notification?: InboxItem | null;
  pinned_at?: string | null;
  archived_at?: string | null;
  presentation_revision?: number;
  intake_status?: ContractTaskIntake["status"] | null;
}

export interface CommandEnvelope {
  idempotency_key: string;
  actor_type: "human" | "master" | "system" | "adapter";
  actor_id: string;
  expected_revision: number;
}

export interface TaskIntakeAsset {
  asset_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  description: string;
  created_at: string;
  integrity_status?: string;
}

export interface TaskIntakeResponse {
  schema_version: string;
  intake: ContractTaskIntake;
  intake_revision: number;
  task: ContractMainTask;
  task_revision: number;
  navigation?: ContractTaskNavigationMetadata | null;
  presentation_revision?: number;
  assets: TaskIntakeAsset[];
}

export interface TaskIntakeMutationResponse {
  schema_version: string;
  intake: ContractTaskIntake;
  intake_revision: number;
  asset?: TaskIntakeAsset;
  removed_asset_id?: string;
  task?: ContractMainTask;
  task_revision?: number;
  assets?: TaskIntakeAsset[];
}

export interface MasterAssetReference {
  asset_id: string;
  manifest_relpath: string;
}

export interface MasterSessionAsset extends MasterAssetReference {
  filename: string;
  description: string;
  mime_type: string;
  size_bytes: number;
}

export type MasterSessionProposal = ContractPlanProposal & {
  message_id: string | null;
};

export interface MasterSessionResponse {
  schema_version: string;
  thread: ContractMasterThread;
  thread_revision: number;
  messages: ContractMasterMessage[];
  latest_proposal: ContractPlanProposal | null;
  proposals: MasterSessionProposal[];
  editable_card_ids: string[];
  instance_statuses: Partial<Record<string, ContractAgentInstance["status"]>>;
  unfinished_image_instance_ids: string[];
  task: ContractMainTask;
  task_revision: number;
  gateway_available: boolean;
  assets: MasterSessionAsset[];
}

export interface ConfirmPlanResponse {
  schema_version: string;
  proposal: ContractPlanProposal;
  plan_result: Record<string, unknown>;
  start_result: Record<string, unknown>;
  session: MasterSessionResponse;
}

export type EditableTaskCard = Pick<
  ContractPlanProposal["execution_cards"][number],
  "objective" | "instructions" | "input_assets" | "expected_deliveries" | "parameters"
>;

export interface WorkItemStageProjection {
  stage_id: string;
  position: number;
  type: "general" | "image" | "ppt";
  required: boolean;
  depends_on: string[];
  status: string;
  available: boolean;
  work_item_ids: string[];
}

export interface WorkItemListResponse {
  schema_version: string;
  task: ContractMainTask;
  task_revision: number;
  stages: WorkItemStageProjection[];
  items: ContractWorkItemProjection[];
  summary: Record<ContractWorkItemProjection["business_status"], number>;
  refresh_after_ms: 3000 | 5000;
  projection_revision: string;
}

export interface WorkItemDetailResponse {
  schema_version: string;
  task: ContractMainTask;
  item: ContractWorkItemProjection;
  refresh_after_ms: 3000 | 5000;
  projection_revision: string;
}

export interface AgentWorkbenchLinkResponse {
  schema_version: "1.0";
  task_id: string;
  work_item_id: string;
  instance_id: string;
  agent_type: "general" | "image" | "ppt";
  instance_status: string;
  task_revision: number;
  ui_url: string | null;
  link_status: "STARTING" | "START_FAILED" | "READY" | "NO_UI_URL" | "ADAPTER_UNAVAILABLE" | "FRAME_BLOCKED";
  start_operation: StartOperation | null;
  embeddable: boolean;
  frame_policy: string;
  diagnostic: string;
}

export interface ManualFinishedResponse {
  schema_version: string;
  task_revision: number;
  plan: {
    instances: Array<{ instance_id: string; manual_finished?: boolean }>;
  };
}

export interface InstanceOperationResponse {
  schema_version: "1.1";
  operation_id: string;
  task_id: string;
  state: StartOperation["state"];
  instance_progress: StartOperation["instance_progress"];
  last_error: StartOperation["last_error"];
  retry_allowed: boolean;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface InstanceRuntimeSettingsResponse {
  schema_version: "2.0";
  scope: { task_id: string; work_item_id: string; instance_id: string };
  revision: {
    current: number;
    revision_id: string;
    task_config_revision_id: string;
    config_hash: string;
    pending_revision_id: string | null;
  };
  values: Record<string, {
    inherited: unknown;
    effective: unknown;
    overridden: boolean;
    source: string;
    explicit?: unknown;
  }>;
  editable: boolean;
  editable_schema: Record<string, unknown>;
  model_options: Record<string, Array<{ id: string; label: string }>>;
  workflow_boundary: Record<string, unknown>;
  sync_candidates: Array<{ instance_id: string; work_item_id?: string }>;
  sync_to_peers: boolean;
  sync_peers: Array<{ instance_id: string; work_item_id: string; started: boolean }>;
  pending_application: Record<string, unknown> | null;
  last_application_failure: Record<string, unknown> | null;
}

export interface RuntimeSettingsProposalResponse {
  schema_version: "2.0";
  proposal_id: string;
  status: string;
  base_revision: number;
  effective_runtime: Record<string, unknown>;
  effective_model_ids: Record<string, string>;
  diff: Array<{
    field: string;
    before: unknown;
    after: unknown;
    consumer_state: string;
    history_effect: "future_only";
    message: string;
  }>;
  apply_mode: string;
  workflow_boundary: Record<string, unknown>;
  sync_instance_ids: string[];
}

export interface RuntimeSettingsConfirmationResponse {
  schema_version: "2.0";
  proposal_id: string;
  instance_id: string;
  status: "APPLIED_BEFORE_START" | "APPLIED_ON_BRANCH" | "WAITING_SAFE_POINT" | "FAILED";
  saga_state: string;
  revision_id: string;
  config_hash: string | null;
  branch_id: string | null;
  checkpoint_id: string | null;
  sync_instance_ids: string[];
  last_error: Record<string, unknown> | null;
  peer_sync?: PeerSyncSummary;
}

export interface PeerSyncSummary {
  enabled: boolean;
  updated: number;
  waiting_safe_point: number;
  unchanged: number;
  failed: number;
  completed_history_unchanged: number;
  items: Array<{
    instance_id: string | null;
    status: string;
    revision_id?: string;
    branch_id?: string | null;
    error_code?: string;
    message?: string;
  }>;
}

export interface WorkItemSyncToggleResponse {
  schema_version: "1.0";
  task_id: string;
  work_item_id: string;
  instance_id: string;
  sync_to_peers: boolean;
  sync_peers: Array<{ instance_id: string; work_item_id: string; started: boolean }>;
  updated_at: string;
}

export interface TaskSettingsBroadcastResponse {
  schema_version: "1.0";
  task_id: string;
  revision: string;
  updated: number;
  waiting_safe_point: number;
  unchanged: number;
  failed: number;
  completed_history_unchanged: number;
  items: Array<{
    task_id: string;
    instance_id: string | null;
    status: string;
    revision_id?: string;
    branch_id?: string | null;
    error_code?: string;
    message?: string;
  }>;
}

export interface HarnessSettingsDocument {
  schema_version: "1.0";
  server: { host: string; port: number; log_level: string };
  models: {
    master: string;
    text_reasoning: string;
    vision_understanding: string;
    image_generation: string;
  };
  master: {
    model_timeout_seconds: number;
    max_tool_rounds: number;
    max_clarification_questions: number;
    require_plan_confirmation: boolean;
    default_start_policy: "manual" | "auto";
  };
  document_processing: {
    max_files_per_task: number;
    max_total_bytes: number;
    max_pdf_pages: number;
    text_chunk_chars: number;
    visual_analysis: "auto" | "always" | "never";
    require_source_citations: boolean;
  };
  supervisor: {
    port_range_start: number;
    port_range_end: number;
    startup_timeout_seconds: number;
    shutdown_grace_seconds: number;
  };
}

export interface ImageAgentSettingsDocument {
  schema_version: "1.0";
  question_preference: "proactive" | "blocking_only" | "on_demand";
  max_auto_questions: number;
  clarification_total_budget: number;
  category_constraint: { release: "auto" | "manual" | "off" };
  style_direction: { release: "auto" | "manual" | "off" };
  candidate_concurrency: number;
  default_output_size: string;
  response_format: "url" | "b64_json";
  watermark: boolean;
  self_check: {
    termination: "fix" | "solo";
    fixed_rounds: number;
    max_rounds: number;
    stop_early_on_pass: boolean;
  };
  advanced_model_overrides: Record<string, string | null>;
}

export interface SystemSettingsResponse {
  schema_version: "1.0";
  revision: string;
  harness_settings: HarnessSettingsDocument;
  image_agent_settings: ImageAgentSettingsDocument;
  editable_schema: Record<string, unknown>;
  model_options: Record<string, Array<{ id: string; label: string }>>;
  last_publication: SystemSettingsPublication | null;
}

export interface SystemSettingsPreview {
  schema_version: "1.0";
  preview_id: string;
  base_revision: string;
  candidate_revision: string;
  changes: Array<{ field: string; before: unknown; after: unknown }>;
  harness_settings: HarnessSettingsDocument;
  image_agent_settings: ImageAgentSettingsDocument;
}

export interface SystemSettingsPublication {
  schema_version: "1.0";
  status: "PUBLISHED" | "PARTIAL" | "UNCHANGED";
  revision: string;
  previous_revision?: string;
  published_at?: string;
  changes: Array<{ field: string; before: unknown; after: unknown }>;
  distribution: {
    updated: number;
    waiting_safe_point: number;
    failed: number;
    completed_history_unchanged: number;
    items: Array<Record<string, unknown>>;
  };
}

export interface StartFailure {
  code: string;
  message: string;
  details: Record<string, unknown>;
  phase: string;
  operation_id: string;
  attempt: number;
  retryable: boolean;
  failed_at: string;
}

export interface StartOperation {
  schema_version: "1.1";
  operation_id: string;
  task_id: string;
  state: "QUEUED" | "RUNNING" | "COMMITTED" | "RETRYABLE_FAILED" | "ABORTED" | "SUPERSEDED";
  instance_progress: Record<string, {
    state: string;
    attempt: number;
    launch_id: string | null;
    side_effect_stage: string;
    last_error: StartFailure | null;
    updated_at: string;
  }>;
  last_error: StartFailure | null;
  retry_allowed: boolean;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface LatestStartOperationResponse {
  schema_version: "1.1";
  operation: StartOperation | null;
}

export interface PageInfo {
  limit: number;
  order: "asc" | "desc";
  has_more: boolean;
  next_cursor: string | null;
}

export type AgentInstance = ContractAgentInstance;

export interface TaskPlan {
  stages: ContractStage[];
  instances: AgentInstance[];
}

export interface TaskDetailResponse {
  schema_version: string;
  task: TaskSummary & { goal: string; start_policy: string; master_owner: string };
  task_revision: number;
  plan: TaskPlan | null;
  recent_notifications?: InboxItem[];
}

export interface AdapterObservation {
  status: string;
  step_id: string | null;
  capabilities: string[];
  details: {
    job_id?: string | null;
    job_status?: string | null;
    timeline_cursor?: number;
    phase?: string;
    compatibility_error?: string;
  };
}

export interface InstanceStartProgress {
  state: "PENDING" | "PREPARING" | "PROCESS_STARTING" | "AGENT_STARTING" | "RUNNING";
  attempt: number;
  launch_id: string | null;
  side_effect_stage: string;
  last_error: { code?: string; message?: string } | null;
  updated_at: string;
}

export interface InstanceDetailResponse {
  schema_version: string;
  task_id: string;
  task_revision: number;
  instance: AgentInstance;
  observation: AdapterObservation | null;
  pending_approval: Approval | null;
  start_operation_id: string | null;
  start_progress: InstanceStartProgress | null;
  start_in_progress: boolean;
  start_retry_allowed: boolean;
}

export interface AuditEvent {
  event_id: string;
  event_type: "OBJECT_COMMITTED";
  object_type: string;
  object_id: string;
  revision: number;
  actor: { actor_type: string; actor_id: string };
  command: string;
  result: "COMMITTED";
  occurred_at: string;
}

export interface Approval extends ContractApprovalRequest {
  decision?: string;
  action?: string | null;
}

export interface ApprovalDetailResponse {
  schema_version: string;
  approval: Approval;
  approval_revision: number;
  payload: {
    available_actions: string[];
    context: Record<string, unknown>;
  };
}

export type InboxItem = ContractInboxItem;

export interface TaskFile {
  relative_path: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  previewable: boolean;
}

export interface AssetManifest {
  integrity_status: string;
  manifest: ContractAssetManifest;
}

export interface TokenTotals {
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
}

export interface CostSummary {
  completeness: "COMPLETE" | "PARTIAL" | "UNKNOWN";
  known_micros: number;
  priced_event_count: number;
  unpriced_event_count: number;
  price_catalog_revisions: string[];
}

export interface UsageSummary {
  schema_version: string;
  task_id: string;
  instance_id: string | null;
  completeness: "COMPLETE" | "PARTIAL" | "NOT_REPORTED";
  event_count: number;
  tokens: TokenTotals;
  cost: CostSummary;
  instances: Array<{
    instance_id: string;
    agent_type: string;
    completeness: "COMPLETE" | "PARTIAL" | "NOT_REPORTED";
    event_count: number;
    tokens: TokenTotals;
    cost: CostSummary;
    last_checked_at: string | null;
  }>;
  models: Array<{
    model: string;
    event_count: number;
    tokens: TokenTotals;
    cost: CostSummary;
  }>;
  time_buckets: Array<{ hour: string; event_count: number; tokens: TokenTotals }>;
  events: Array<{
    event_id: string;
    instance_id: string;
    request_id: string;
    provider_request_id?: string | null;
    provider?: string;
    model: string;
    call_type?: string;
    usage_basis?: ContractTokenUsageEvent["usage_basis"];
    billing_units?: Array<{
      unit: string;
      quantity: number;
      attributes?: Record<string, string | number | boolean>;
    }>;
    total_tokens: number;
    occurred_at: string;
  }>;
}

export interface RetryBudget {
  revision: number;
  retry_policy: {
    max_auto_retries_per_retry_group: number;
    max_auto_retry_tokens_task: number;
    retry_token_reservation_by_agent: Record<string, number>;
    max_auto_retry_cost_micros: number | null;
    price_catalog_revision: string | null;
  };
  retry_budget_ledger: {
    retry_tokens_reserved: number;
    retry_tokens_settled: number;
    retry_cost_micros_reserved: number;
    retry_cost_micros_settled: number | null;
    frozen: boolean;
    frozen_reason: string | null;
  };
  attempts: Array<{ attempt_id: string; status: string }>;
}

export interface DeliveryBundleFile {
  private_relative_path: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
}

export interface DeliveryBundleCandidate {
  schema_version: string;
  bundle_id: string;
  task_id: string;
  work_item_id: string;
  instance_id: string;
  task_card_revision: number;
  branch_id: string;
  checkpoint_id: string;
  image: DeliveryBundleFile & { width: number; height: number };
  design_note: DeliveryBundleFile;
  status: "PENDING_CONFIRMATION" | "PUBLISHED" | "REJECTED" | "CORRUPTED";
  created_at: string;
  decided_at: string | null;
  actor: { type: string; id: string } | null;
  publication_batch_id: string | null;
}

export interface BundleManifest {
  schema_version: string;
  bundle_id: string;
  task_id: string;
  work_item_id: string;
  instance_id: string;
  task_card_revision: number;
  branch_id: string;
  checkpoint_id: string;
  publication_batch_id: string;
  image_asset: MasterAssetReference;
  design_note_asset: MasterAssetReference;
  actor: { type: string; id: string };
  created_at: string;
  published_at: string;
}

const DEPLOYMENT_ERROR_PREFIXES = [
  "CONFIG_",
  "MODEL_PROVIDER_",
] as const;

const DEPLOYMENT_ERROR_CODES = new Set([
  "ADAPTER_UNAVAILABLE",
  "MASTER_RUN_FAILED",
  "PROCESS_START_FAILED",
  "UI_LINK_REJECTED",
]);

const DEPLOYMENT_ERROR_TERMS = /(?:api\s*key|credential|endpoint|mastergateway|model_list\.yaml|provider(?:\.yaml)?|runtime\.yaml|\.env|凭据|模型路由|配置(?:文件|快照|修订|错误|缺失))/i;

const ADAPTER_VALIDATION_MESSAGES: Record<string, string> = {
  "Task card agent_type must be image.": "任务卡的智能体类型必须为图片。",
  "Image TaskCard 1.1 requires parameters.usage_context.": "图片任务卡缺少使用场景。",
  "Image category_id and category_version must be supplied together.": "图片类别标识和类别版本必须同时填写。",
  "Image Agent requires at least one verified source asset.": "图片任务缺少已验证的源素材。",
  "Image Agent requires exactly one required final image delivery.": "图片任务必须且只能包含一个必需的最终图片交付项。",
};

export function designerErrorMessage(
  message: string,
  code?: string,
  details?: Record<string, unknown>,
): string {
  const deploymentCode = code !== undefined && (
    DEPLOYMENT_ERROR_CODES.has(code)
    || DEPLOYMENT_ERROR_PREFIXES.some((prefix) => code.startsWith(prefix))
  );
  const validationErrors = Array.isArray(details?.errors)
    ? details.errors
      .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
      .map((item) => item.trim())
    : [];
  if (
    deploymentCode
    || [message, ...validationErrors].some((item) => DEPLOYMENT_ERROR_TERMS.test(item))
  ) {
    return "智能创作服务暂时不可用，请稍后重试；当前任务内容已保留。如持续失败，请联系支持人员。";
  }
  if (code === "VALIDATION_ERROR" && validationErrors.length > 0) {
    return `任务卡未通过智能体校验：${validationErrors
      .map((item) => ADAPTER_VALIDATION_MESSAGES[item] ?? `校验项不合法（${item}）`)
      .join("；")}`;
  }
  return message;
}

export interface DeliveryReview {
  bundle_id: string;
  approval: Approval;
  approval_revision: number;
}

export interface DeliveryBundlesResponse {
  schema_version: string;
  candidates: DeliveryBundleCandidate[];
  manifests: BundleManifest[];
  reviews: DeliveryReview[];
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly details?: Record<string, unknown>,
  ) {
    super(message);
  }
}

export class ApiClient {
  constructor(private readonly baseUrl = "") {}

  health(signal?: AbortSignal): Promise<HealthResponse> {
    return this.get<HealthResponse>("/healthz", signal);
  }

  readiness(signal?: AbortSignal): Promise<ReadyResponse> {
    return this.get<ReadyResponse>("/readyz", signal);
  }

  tasks(signal?: AbortSignal): Promise<{ schema_version: string; items: TaskSummary[]; page?: PageInfo }> {
    return this.get("/api/v1/tasks", signal);
  }

  task(taskId: string, signal?: AbortSignal): Promise<TaskDetailResponse> {
    return this.get(`/api/v1/tasks/${encodeURIComponent(taskId)}`, signal);
  }

  taskIntake(taskId: string, signal?: AbortSignal): Promise<TaskIntakeResponse> {
    return this.get(`/api/v1/task-intakes/${encodeURIComponent(taskId)}`, signal);
  }

  createTaskIntake(body: {
    prompt: string;
    start_policy?: "manual" | "auto";
    envelope: CommandEnvelope;
  }): Promise<TaskIntakeResponse> {
    return this.send("POST", "/api/v1/task-intakes", body);
  }

  removeTaskIntakeAsset(
    taskId: string,
    assetId: string,
    envelope: CommandEnvelope,
  ): Promise<TaskIntakeMutationResponse> {
    return this.send(
      "DELETE",
      `/api/v1/task-intakes/${encodeURIComponent(taskId)}/assets/${encodeURIComponent(assetId)}`,
      { envelope },
    );
  }

  submitTaskIntake(
    taskId: string,
    taskExpectedRevision: number,
    envelope: CommandEnvelope,
  ): Promise<TaskIntakeMutationResponse> {
    return this.send("POST", `/api/v1/task-intakes/${encodeURIComponent(taskId)}/submit`, {
      task_expected_revision: taskExpectedRevision,
      envelope,
    });
  }

  updateTaskPresentation(
    taskId: string,
    patch: { title?: string; pinned?: boolean; archived?: boolean; envelope: CommandEnvelope },
  ): Promise<{
    schema_version: string;
    task: ContractMainTask;
    task_revision: number;
    navigation: ContractTaskNavigationMetadata | null;
    presentation_revision: number;
  }> {
    return this.send("PATCH", `/api/v1/tasks/${encodeURIComponent(taskId)}/presentation`, patch);
  }

  masterSession(taskId: string, signal?: AbortSignal): Promise<MasterSessionResponse> {
    return this.get(`/api/v1/tasks/${encodeURIComponent(taskId)}/master/messages`, signal);
  }

  appendMasterMessage(
    taskId: string,
    body: {
      content: string;
      asset_refs: MasterAssetReference[];
      envelope: CommandEnvelope;
    },
  ): Promise<MasterSessionResponse> {
    return this.send(
      "POST",
      `/api/v1/tasks/${encodeURIComponent(taskId)}/master/messages`,
      body,
    );
  }

  confirmPlanProposal(
    taskId: string,
    proposalRevision: number,
    body: {
      task_expected_revision: number;
      expected_card_revisions: Record<string, number>;
      envelope: CommandEnvelope;
      instance_ids?: string[];
    },
  ): Promise<ConfirmPlanResponse> {
    return this.send(
      "POST",
      `/api/v1/tasks/${encodeURIComponent(taskId)}/plan-proposals/${proposalRevision}/confirm`,
      body,
    );
  }

  updatePlanTaskCard(
    taskId: string,
    proposalRevision: number,
    cardId: string,
    body: EditableTaskCard & {
      expected_proposal_revision: number;
      expected_card_revision: number;
      envelope: CommandEnvelope;
    },
  ): Promise<MasterSessionResponse> {
    return this.send(
      "PATCH",
      `/api/v1/tasks/${encodeURIComponent(taskId)}/plan-proposals/${proposalRevision}/task-cards/${encodeURIComponent(cardId)}`,
      body,
    );
  }

  workItems(taskId: string, signal?: AbortSignal): Promise<WorkItemListResponse> {
    return this.get(`/api/v1/tasks/${encodeURIComponent(taskId)}/work-items`, signal);
  }

  workItem(
    taskId: string,
    workItemId: string,
    signal?: AbortSignal,
  ): Promise<WorkItemDetailResponse> {
    return this.get(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/work-items/${encodeURIComponent(workItemId)}`,
      signal,
    );
  }

  updateWorkItemStatus(
    taskId: string,
    workItemId: string,
    businessStatus: "TODO" | "RUNNING" | "WAITING_APPROVAL" | "COMPLETED",
    envelope: CommandEnvelope,
  ): Promise<WorkItemListResponse> {
    return this.send(
      "PATCH",
      `/api/v1/tasks/${encodeURIComponent(taskId)}/work-items/${encodeURIComponent(workItemId)}/status`,
      { business_status: businessStatus, envelope },
    );
  }

  instanceUiLink(
    taskId: string,
    workItemId: string,
    instanceId: string,
    signal?: AbortSignal,
  ): Promise<AgentWorkbenchLinkResponse> {
    const query = new URLSearchParams({ task_id: taskId, work_item_id: workItemId });
    return this.get(
      `/api/v1/instances/${encodeURIComponent(instanceId)}/ui-link?${query.toString()}`,
      signal,
    );
  }

  setManualFinished(
    instanceId: string,
    manualFinished: boolean,
    envelope: CommandEnvelope,
  ): Promise<ManualFinishedResponse> {
    const action = manualFinished ? "manual-finished" : "manual-in-progress";
    return this.send(
      "POST",
      `/api/v1/instances/${encodeURIComponent(instanceId)}/${action}`,
      { envelope },
    );
  }

  startInstance(
    instanceId: string,
    operationId: string,
    envelope: CommandEnvelope,
  ): Promise<InstanceOperationResponse> {
    return this.send(
      "POST",
      `/api/v1/instances/${encodeURIComponent(instanceId)}/start`,
      { operation_id: operationId, envelope },
    );
  }

  instanceRuntimeSettings(
    instanceId: string,
    signal?: AbortSignal,
  ): Promise<InstanceRuntimeSettingsResponse> {
    return this.get(
      `/api/v1/instances/${encodeURIComponent(instanceId)}/runtime-settings`,
      signal,
    );
  }

  proposeInstanceRuntimeSettings(
    instanceId: string,
    body: {
      base_revision: number;
      overrides: Record<string, unknown>;
      sync_unstarted_image_work_items: boolean;
      expected_sync_instance_ids: string[];
      envelope: CommandEnvelope;
    },
  ): Promise<RuntimeSettingsProposalResponse> {
    return this.send(
      "POST",
      `/api/v1/instances/${encodeURIComponent(instanceId)}/runtime-setting-proposals`,
      body,
    );
  }

  confirmInstanceRuntimeSettings(
    instanceId: string,
    proposalId: string,
    envelope: CommandEnvelope,
  ): Promise<RuntimeSettingsConfirmationResponse> {
    return this.send(
      "POST",
      `/api/v1/instances/${encodeURIComponent(instanceId)}/runtime-setting-proposals/${encodeURIComponent(proposalId)}/confirm`,
      { envelope },
    );
  }

  setWorkItemSyncToggle(
    taskId: string,
    workItemId: string,
    body: {
      sync_to_peers: boolean;
      envelope: CommandEnvelope;
    },
  ): Promise<WorkItemSyncToggleResponse> {
    return this.send(
      "POST",
      `/api/v1/tasks/${encodeURIComponent(taskId)}/work-items/${encodeURIComponent(workItemId)}/sync-toggle`,
      body,
    );
  }

  broadcastTaskSettings(
    taskId: string,
    envelope: CommandEnvelope,
  ): Promise<TaskSettingsBroadcastResponse> {
    return this.send(
      "POST",
      `/api/v1/tasks/${encodeURIComponent(taskId)}/settings/broadcast`,
      { envelope },
    );
  }

  systemSettings(signal?: AbortSignal): Promise<SystemSettingsResponse> {
    return this.get("/api/v1/system-settings", signal);
  }

  previewSystemSettings(body: {
    base_revision: string;
    harness_settings: HarnessSettingsDocument;
    image_agent_settings: ImageAgentSettingsDocument;
  }): Promise<SystemSettingsPreview> {
    return this.send("POST", "/api/v1/system-settings/preview", body);
  }

  publishSystemSettings(body: {
    preview_id: string;
    base_revision: string;
    harness_settings: HarnessSettingsDocument;
    image_agent_settings: ImageAgentSettingsDocument;
    actor_id: string;
  }): Promise<SystemSettingsPublication> {
    return this.send("POST", "/api/v1/system-settings/publish", body);
  }

  retryStartOperation(
    operationId: string,
    envelope: CommandEnvelope,
  ): Promise<StartOperation> {
    return this.send(
      "POST",
      `/api/v1/start-operations/${encodeURIComponent(operationId)}/retry`,
      { envelope },
    );
  }

  latestStartOperation(
    taskId: string,
    signal?: AbortSignal,
  ): Promise<LatestStartOperationResponse> {
    return this.get(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/start-operations/latest`,
      signal,
    );
  }

  uploadTaskIntakeAsset(
    taskId: string,
    input: {
      file: File;
      declaredMimeType: string;
      description: string;
      envelope: CommandEnvelope;
    },
    onProgress: (percent: number) => void,
    signal: AbortSignal,
  ): Promise<TaskIntakeMutationResponse> {
    return this.uploadAssetToPath(
      `/api/v1/task-intakes/${encodeURIComponent(taskId)}/assets`,
      input,
      onProgress,
      signal,
    );
  }

  uploadTaskAsset(
    taskId: string,
    input: {
      file: File;
      declaredMimeType: string;
      description: string;
      envelope: CommandEnvelope;
    },
    onProgress: (percent: number) => void,
    signal: AbortSignal,
  ): Promise<TaskIntakeMutationResponse> {
    return this.uploadAssetToPath(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/asset-uploads`,
      input,
      onProgress,
      signal,
    );
  }

  private uploadAssetToPath(
    path: string,
    input: {
      file: File;
      declaredMimeType: string;
      description: string;
      envelope: CommandEnvelope;
    },
    onProgress: (percent: number) => void,
    signal: AbortSignal,
  ): Promise<TaskIntakeMutationResponse> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const abort = (): void => xhr.abort();
      xhr.open("POST", `${this.baseUrl}${path}`);
      xhr.setRequestHeader("Accept", "application/json");
      xhr.responseType = "json";
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && event.total > 0) {
          onProgress(Math.min(99, Math.round((event.loaded / event.total) * 100)));
        }
      });
      xhr.addEventListener("load", () => {
        signal.removeEventListener("abort", abort);
        const payload = xhr.response as
          | TaskIntakeMutationResponse
          | { error?: { message?: string; code?: string } }
          | null;
        if (xhr.status >= 200 && xhr.status < 300 && payload) {
          onProgress(100);
          resolve(payload as TaskIntakeMutationResponse);
          return;
        }
        const error = payload && "error" in payload ? payload.error : undefined;
        const message = error?.message
          ? designerErrorMessage(error.message, error.code)
          : `上传失败（${xhr.status || "网络中断"}）。`;
        reject(new ApiError(message, xhr.status, error?.code));
      });
      xhr.addEventListener("error", () => {
        signal.removeEventListener("abort", abort);
        reject(new ApiError("网络错误，文件尚未确认上传。", 0));
      });
      xhr.addEventListener("abort", () => {
        signal.removeEventListener("abort", abort);
        reject(new DOMException("Upload cancelled", "AbortError"));
      });
      const form = new FormData();
      form.append("file", input.file, input.file.name);
      form.append("declared_mime_type", input.declaredMimeType);
      form.append("description", input.description);
      form.append("idempotency_key", input.envelope.idempotency_key);
      form.append("actor_id", input.envelope.actor_id);
      form.append("expected_revision", String(input.envelope.expected_revision));
      signal.addEventListener("abort", abort, { once: true });
      xhr.send(form);
    });
  }

  instance(instanceId: string, signal?: AbortSignal): Promise<InstanceDetailResponse> {
    return this.get(`/api/v1/instances/${encodeURIComponent(instanceId)}`, signal);
  }

  instanceStartProgress(instanceId: string, signal?: AbortSignal): Promise<InstanceDetailResponse> {
    return this.get(
      `/api/v1/instances/${encodeURIComponent(instanceId)}?refresh=false`,
      signal,
    );
  }

  approval(approvalId: string, signal?: AbortSignal): Promise<ApprovalDetailResponse> {
    return this.get(`/api/v1/approvals/${encodeURIComponent(approvalId)}`, signal);
  }

  inbox(signal?: AbortSignal): Promise<{
    schema_version: string;
    items: InboxItem[];
    unread_count: number;
  }> {
    return this.get("/api/v1/inbox?owner=human", signal);
  }

  viewInstance(instanceId: string): Promise<{
    schema_version: string;
    items: InboxItem[];
    unread_count: number;
  }> {
    return this.send(
      "POST",
      `/api/v1/instances/${encodeURIComponent(instanceId)}/view`,
      {},
    );
  }

  taskApprovals(
    taskId: string,
    signal?: AbortSignal,
  ): Promise<{ schema_version: string; items: Approval[]; page?: PageInfo }> {
    return this.get(`/api/v1/tasks/${encodeURIComponent(taskId)}/approvals?limit=200`, signal);
  }

  taskEvents(
    taskId: string,
    actorType?: "human" | "master" | "system" | "adapter",
    signal?: AbortSignal,
  ): Promise<{ schema_version: string; items: AuditEvent[]; page?: PageInfo }> {
    const query = new URLSearchParams({ limit: "200" });
    if (actorType) query.set("actor_type", actorType);
    return this.get(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/events?${query.toString()}`,
      signal,
    );
  }

  taskFiles(
    taskId: string,
    signal?: AbortSignal,
  ): Promise<{ schema_version: string; items: TaskFile[]; assets: AssetManifest[] }> {
    return this.get(`/api/v1/tasks/${encodeURIComponent(taskId)}/files?group=all`, signal);
  }

  taskSharedFiles(
    taskId: string,
    signal?: AbortSignal,
  ): Promise<{ schema_version: string; items: TaskFile[]; assets: AssetManifest[] }> {
    return this.get(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/files?group=shared&limit=200`,
      signal,
    );
  }

  sharedArchiveUrl(taskId: string): string {
    return `${this.baseUrl}/api/v1/tasks/${encodeURIComponent(taskId)}/files/download-archive?group=shared`;
  }

  deliveryBundles(taskId: string, signal?: AbortSignal): Promise<DeliveryBundlesResponse> {
    return this.get(`/api/v1/tasks/${encodeURIComponent(taskId)}/delivery-bundles`, signal);
  }

  async previewDeliveryMarkdown(taskId: string, bundleId: string, signal?: AbortSignal): Promise<string> {
    const response = await fetch(this.deliveryPreviewUrl(taskId, bundleId, "design_note"), {
      headers: { Accept: "text/markdown" },
      signal,
    });
    if (!response.ok) throw await this.error(response);
    return response.text();
  }

  deliveryPreviewUrl(
    taskId: string,
    bundleId: string,
    asset: "image" | "design_note",
    nonce?: number,
  ): string {
    const query = new URLSearchParams({ asset });
    if (nonce !== undefined) query.set("retry", String(nonce));
    return `${this.baseUrl}/api/v1/tasks/${encodeURIComponent(taskId)}/delivery-bundles/${encodeURIComponent(bundleId)}/preview?${query.toString()}`;
  }

  taskUsage(taskId: string, signal?: AbortSignal): Promise<UsageSummary> {
    return this.get(`/api/v1/tasks/${encodeURIComponent(taskId)}/usage`, signal);
  }

  instanceUsage(instanceId: string, signal?: AbortSignal): Promise<UsageSummary> {
    return this.get(`/api/v1/instances/${encodeURIComponent(instanceId)}/usage`, signal);
  }

  retryBudget(
    taskId: string,
    signal?: AbortSignal,
  ): Promise<{ schema_version: string; budget: RetryBudget }> {
    return this.get(`/api/v1/tasks/${encodeURIComponent(taskId)}/retry-budget`, signal);
  }

  resolveApproval(
    approvalId: string,
    body: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    return this.send("POST", `/api/v1/approvals/${encodeURIComponent(approvalId)}/resolve`, body);
  }

  updateInboxStatus(inboxId: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.send("POST", `/api/v1/inbox/${encodeURIComponent(inboxId)}/status`, body);
  }

  updateApprovalMode(
    instanceId: string,
    body: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    return this.send(
      "PUT",
      `/api/v1/instances/${encodeURIComponent(instanceId)}/approval-mode`,
      body,
    );
  }

  confirmTaskStart(
    taskId: string,
    body: {
      operation_id: string;
      envelope: CommandEnvelope;
      instance_ids?: string[];
    },
  ): Promise<Record<string, unknown>> {
    return this.send("POST", `/api/v1/tasks/${encodeURIComponent(taskId)}/confirm-start`, body);
  }

  cancelTask(taskId: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.send("POST", `/api/v1/tasks/${encodeURIComponent(taskId)}/cancel`, body);
  }

  instanceOperation(
    instanceId: string,
    operation: "start" | "restart" | "cancel" | "archive",
    body: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    return this.send(
      "POST",
      `/api/v1/instances/${encodeURIComponent(instanceId)}/${operation}`,
      body,
    );
  }

  retryDelivery(
    instanceId: string,
    body: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    return this.send(
      "POST",
      `/api/v1/instances/${encodeURIComponent(instanceId)}/deliveries/retry`,
      body,
    );
  }

  async previewText(taskId: string, path: string): Promise<string> {
    const response = await fetch(this.previewUrl(taskId, path), {
      headers: { Accept: "text/plain, application/json, text/markdown" },
    });
    if (!response.ok) throw await this.error(response);
    return response.text();
  }

  previewUrl(taskId: string, path: string): string {
    return `${this.baseUrl}/api/v1/tasks/${encodeURIComponent(taskId)}/files/preview?path=${encodeURIComponent(path)}`;
  }

  downloadUrl(taskId: string, path: string): string {
    return `${this.baseUrl}/api/v1/tasks/${encodeURIComponent(taskId)}/files/download?path=${encodeURIComponent(path)}`;
  }

  private async get<Response>(path: string, signal?: AbortSignal): Promise<Response> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      headers: { Accept: "application/json" },
      signal,
    });
    if (!response.ok) {
      throw await this.error(response);
    }
    return (await response.json()) as Response;
  }

  private async send<Response>(
    method: "DELETE" | "PATCH" | "POST" | "PUT",
    path: string,
    body: object,
  ): Promise<Response> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw await this.error(response);
    return (await response.json()) as Response;
  }

  private async error(response: Response): Promise<ApiError> {
    let message = `请求失败（${response.status}）。`;
    let code: string | undefined;
    let details: Record<string, unknown> | undefined;
    try {
      const payload = (await response.json()) as {
        error?: { message?: string; code?: string; details?: Record<string, unknown> };
        detail?: string | Array<{ msg?: string }>;
      };
      if (payload.error?.message) message = payload.error.message;
      else if (typeof payload.detail === "string") message = payload.detail;
      else if (Array.isArray(payload.detail)) {
        message = payload.detail.map((item) => item.msg).filter(Boolean).join("；") || message;
      }
      code = payload.error?.code;
      details = payload.error?.details;
    } catch {
      // The status fallback remains useful when an intermediary returns a non-JSON body.
    }
    return new ApiError(
      designerErrorMessage(message, code, details),
      response.status,
      code,
      details,
    );
  }
}
