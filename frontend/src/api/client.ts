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
  status: "ready" | "not_ready";
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
}

export interface MasterSessionResponse {
  schema_version: string;
  thread: ContractMasterThread;
  thread_revision: number;
  messages: ContractMasterMessage[];
  latest_proposal: ContractPlanProposal | null;
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

export interface WorkItemStageProjection {
  stage_id: string;
  position: number;
  type: "image" | "ppt";
  required: boolean;
  depends_on: string[];
  status: string;
  available: boolean;
  work_item_ids: string[];
}

export interface WorkItemListResponse {
  schema_version: string;
  task: ContractMainTask;
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

export interface InstanceDetailResponse {
  schema_version: string;
  task_id: string;
  task_revision: number;
  instance: AgentInstance;
  observation: AdapterObservation | null;
  pending_approval: Approval | null;
  credential: CredentialSummary | null;
  config: {
    config_revision: number;
    restart_required: boolean;
    config: Record<string, unknown>;
  };
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

export interface GlobalConfigResponse {
  schema_version: string;
  config: Record<string, unknown> & { revision: number };
}

export interface CredentialSummary {
  credential_pair_id: string;
  provider: string;
  key_id: string;
  key_tail: string;
  base_url_hint: string;
  revision: number;
  enabled: boolean;
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
    start_policy: "manual" | "auto";
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
    body: { task_expected_revision: number; envelope: CommandEnvelope },
  ): Promise<ConfirmPlanResponse> {
    return this.send(
      "POST",
      `/api/v1/tasks/${encodeURIComponent(taskId)}/plan-proposals/${proposalRevision}/confirm`,
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
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const abort = (): void => xhr.abort();
      xhr.open("POST", `${this.baseUrl}/api/v1/task-intakes/${encodeURIComponent(taskId)}/assets`);
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
          | { error?: { message?: string } }
          | null;
        if (xhr.status >= 200 && xhr.status < 300 && payload) {
          onProgress(100);
          resolve(payload as TaskIntakeMutationResponse);
          return;
        }
        const message = payload && "error" in payload && payload.error?.message
          ? payload.error.message
          : `上传失败（${xhr.status || "网络中断"}）。`;
        reject(new ApiError(message, xhr.status));
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

  approval(approvalId: string, signal?: AbortSignal): Promise<ApprovalDetailResponse> {
    return this.get(`/api/v1/approvals/${encodeURIComponent(approvalId)}`, signal);
  }

  inbox(signal?: AbortSignal): Promise<{ schema_version: string; items: InboxItem[] }> {
    return this.get("/api/v1/inbox?owner=human", signal);
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

  globalConfig(signal?: AbortSignal): Promise<GlobalConfigResponse> {
    return this.get("/api/v1/config/global", signal);
  }

  keyPool(
    signal?: AbortSignal,
  ): Promise<{ schema_version: string; items: CredentialSummary[] }> {
    return this.get("/api/v1/key-pool", signal);
  }

  updateGlobalConfig(body: Record<string, unknown>): Promise<GlobalConfigResponse> {
    return this.send("PUT", "/api/v1/config/global", body);
  }

  updateKeyPool(body: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.send("PUT", "/api/v1/key-pool", body);
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

  confirmTaskStart(taskId: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
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

  updateInstanceConfig(
    instanceId: string,
    body: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    return this.send("PUT", `/api/v1/instances/${encodeURIComponent(instanceId)}/config`, body);
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
    return new ApiError(message, response.status, code, details);
  }
}
