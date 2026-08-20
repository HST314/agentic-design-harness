export interface HealthResponse {
  status: string;
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
}

export interface AgentInstance {
  instance_id: string;
  task_id: string;
  agent_type: string;
  status: string;
  required: boolean;
  approval_mode: string;
  config_revision: number;
  ui_url: string | null;
  process: { pid: number; port: number; state: string; started_at: string } | null;
}

export interface TaskPlan {
  stages: Array<{
    stage_id: string;
    type: string;
    position: number;
    status: string;
    required: boolean;
    instance_ids: string[];
  }>;
  instances: AgentInstance[];
}

export interface TaskDetailResponse {
  schema_version: string;
  task: TaskSummary & { goal: string; start_policy: string; master_owner: string };
  task_revision: number;
  plan: TaskPlan | null;
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
  instance: AgentInstance;
  observation: AdapterObservation | null;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
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

  tasks(signal?: AbortSignal): Promise<{ schema_version: string; items: TaskSummary[] }> {
    return this.get("/api/v1/tasks", signal);
  }

  task(taskId: string, signal?: AbortSignal): Promise<TaskDetailResponse> {
    return this.get(`/api/v1/tasks/${encodeURIComponent(taskId)}`, signal);
  }

  instance(instanceId: string, signal?: AbortSignal): Promise<InstanceDetailResponse> {
    return this.get(`/api/v1/instances/${encodeURIComponent(instanceId)}`, signal);
  }

  private async get<Response>(path: string, signal?: AbortSignal): Promise<Response> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      headers: { Accept: "application/json" },
      signal,
    });
    if (!response.ok) {
      throw new ApiError(`Request failed with status ${response.status}.`, response.status);
    }
    return (await response.json()) as Response;
  }
}
