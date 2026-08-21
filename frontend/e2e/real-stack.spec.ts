import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { performance } from "node:perf_hooks";

const taskId = "t_browser_real_stack";
const instanceId = "i_browser_real_stack";
const maxWorkflowAdvances = 12;
const realProviderWorkflowBudgetMs = 600_000;
const deterministicProviderWorkflowBudgetMs = 120_000;
const singleModelTimeoutMs = 180_000;
const failureDiagnosticsAllowanceMs = 10_000;
const knownInstanceStatuses = new Set([
  "CREATED",
  "READY",
  "UNAVAILABLE",
  "STARTING",
  "RUNNING",
  "WAITING_APPROVAL",
  "FAILED_TO_START",
  "SUCCEEDED",
  "FAILED",
  "CRASHED",
  "CANCELLED",
  "SUPERSEDED",
  "ARCHIVED",
]);
const activeInstanceStatuses = new Set(["CREATED", "READY", "STARTING", "RUNNING"]);

type GatePhase =
  | "seed_workflow"
  | "wait_for_boundary"
  | "read_approval"
  | "advance_approval"
  | "assert_completion"
  | "assert_clarification"
  | "assert_usage"
  | "assert_usage_ui"
  | "assert_delivery_ui";

type UsageCounts = {
  reasoning_llm: number;
  text_to_image_model: number;
  vision_language_model: number;
};

function remainingWorkflowBudget(deadlineMs: number): number {
  const remainingMs = deadlineMs - performance.now();
  if (remainingMs <= 0) throw new Error("workflow_deadline_exhausted");
  return remainingMs;
}

async function jsonRequest(
  request: APIRequestContext,
  method: "get" | "post" | "put",
  path: string,
  data?: Record<string, unknown>,
  deadlineMs?: number,
): Promise<Record<string, any>> {
  const response = await request[method](path, {
    ...(data ? { data } : {}),
    ...(deadlineMs === undefined ? {} : { timeout: remainingWorkflowBudget(deadlineMs) }),
  });
  if (!response.ok()) {
    throw new Error(`${method.toUpperCase()} ${path}: ${response.status()} ${await response.text()}`);
  }
  return response.json() as Promise<Record<string, any>>;
}

function envelope(key: string, revision: number): Record<string, unknown> {
  return {
    idempotency_key: key,
    actor_type: "human",
    actor_id: "browser_operator",
    expected_revision: revision,
  };
}

async function seedRealWorkflow(request: APIRequestContext, deadlineMs: number): Promise<void> {
  const providerUrl = process.env.HARNESS_BROWSER_PROVIDER_URL;
  if (!providerUrl) throw new Error("HARNESS_BROWSER_PROVIDER_URL is required");
  const textModel = process.env.HARNESS_BROWSER_TEXT_MODEL ?? "browser-text";
  const imageModel = process.env.HARNESS_BROWSER_IMAGE_MODEL ?? "browser-image";
  const vlmModel = process.env.HARNESS_BROWSER_VLM_MODEL ?? "browser-vlm";

  const current = await jsonRequest(request, "get", "/api/v1/config/global", undefined, deadlineMs);
  const { revision, ...config } = current.config as Record<string, any>;
  config.image_provider = "ark";
  config.image_runtime_policy = {
    ...config.image_runtime_policy,
    offline_mode: false,
    category_constraint: { release: "off" },
    style_direction: { release: "off" },
    skill_invocation: { release: "off" },
    self_check: {
      termination: "solo",
      fixed_rounds: 1,
      max_rounds: 1,
      stop_early_on_pass: true,
      release: "manual",
    },
  };
  config.image_model_config = {
    model_config_id: "browser_deterministic_provider",
    state_bindings: [
      ["intake_clarify", "reasoning_llm", textModel],
      ["confirmation_build", "reasoning_llm", textModel],
      ["initial_candidate_generation", "text_to_image_model", imageModel],
      ["self_check_inspection", "vision_language_model", vlmModel],
      ["self_check_rework", "text_to_image_model", imageModel],
      ["human_prompt_rework", "text_to_image_model", imageModel],
    ].map(([state, model_role, model]) => ({ state, model_role, provider: "ark", model })),
  };
  await jsonRequest(request, "put", "/api/v1/config/global", {
    config,
    operation_id: "configure_browser_real_stack",
    envelope: envelope("configure-browser-real-stack", Number(revision)),
  }, deadlineMs);
  await jsonRequest(request, "put", "/api/v1/key-pool", {
    pairs: [{
      credential_pair_id: "cred_browser_real_stack",
      provider: "ark",
      key_id: "key_browser_real_stack",
      base_url: providerUrl,
      api_key: process.env.HARNESS_BROWSER_PROVIDER_API_KEY ?? "local-provider-value",
      api_key_env: "ARK_API_KEY",
      base_url_env: "ARK_BASE_URL",
      revision: 1,
      enabled: true,
    }],
    envelope: envelope("configure-browser-credential", 0),
  }, deadlineMs);

  await jsonRequest(request, "post", "/api/v1/tasks", {
    task_id: taskId,
    title: "生产链路无 Mock 验收",
    goal: "通过生产构建、真实 Harness 与确定性 Provider 交付图片。",
    master_owner: "master_default",
    start_policy: "manual",
    input_manifest: "inputs/manifests/browser-real-stack.json",
    envelope: envelope("create-browser-real-stack", 0),
  }, deadlineMs);
  const imported = await jsonRequest(request, "post", `/api/v1/tasks/${taskId}/assets`, {
    filename: "brief.md",
    content_base64: Buffer.from("# 生产链路验收任务书\n").toString("base64"),
    description: "浏览器真实链路的受控输入。",
    operation_id: "import_browser_brief",
    envelope: envelope("import-browser-brief", 1),
  }, deadlineMs);
  const assetId = String(imported.manifest.asset_id);
  const plan = await jsonRequest(request, "put", `/api/v1/tasks/${taskId}/plan`, {
    stages: [{
      stage_id: "s_browser_image",
      task_id: taskId,
      type: "image",
      position: 1,
      depends_on: [],
      required: true,
      instance_ids: [instanceId],
    }],
    instances: [{
      instance_id: instanceId,
      task_id: taskId,
      stage_id: "s_browser_image",
      agent_type: "image",
      required: true,
      approval_mode: "human",
      config_revision: 1,
      credential_pair_ref: "pending_assignment",
      credential_pair_revision: 1,
      workspace_relpath: `instances/${instanceId}`,
      task_card_relpath: `instances/${instanceId}/task-card.json`,
    }],
    task_cards: [{
      schema_version: "1.1",
      card_id: "card_browser_real_stack",
      revision: 1,
      task_id: taskId,
      stage_id: "s_browser_image",
      instance_id: instanceId,
      agent_type: "image",
      objective: "生成一张可验证的最终图片。",
      instructions: ["仅使用已登记输入，交付最终 PNG。"],
      input_assets: [{
        asset_id: assetId,
        manifest_relpath: `inputs/manifests/${assetId}.json`,
      }],
      expected_deliveries: [{
        kind: "image",
        role: "final_artwork",
        required: true,
        accepted_mime_types: ["image/png"],
      }],
      parameters: { variants: 1, usage_context: "P1 no-mock browser acceptance" },
      created_at: new Date().toISOString(),
    }],
    providers: { [instanceId]: "ark" },
    operation_id: "save_browser_real_stack_plan",
    envelope: envelope("save-browser-real-stack-plan", 1),
  }, deadlineMs);
  await jsonRequest(request, "post", `/api/v1/tasks/${taskId}/confirm-start`, {
    operation_id: "start_browser_real_stack",
    envelope: envelope("start-browser-real-stack", Number(plan.task_revision)),
  }, deadlineMs);
}

async function waitForBoundary(
  request: APIRequestContext,
  deadlineMs: number,
): Promise<Record<string, any>> {
  let latest: Record<string, any> | undefined;
  await expect.poll(async () => {
    latest = await jsonRequest(
      request,
      "get",
      `/api/v1/instances/${instanceId}`,
      undefined,
      deadlineMs,
    );
    return !activeInstanceStatuses.has(safeInstanceStatus(latest.instance.status));
  }, {
    // The previous 90-second cap was shorter than one legal 180-second model call.
    // Spending the shared deadline's remaining budget keeps every wait inside the
    // single workflow budget while allowing any full model timeout that still fits.
    timeout: remainingWorkflowBudget(deadlineMs),
  }).toBe(true);
  return latest as Record<string, any>;
}

function safeInstanceStatus(value: unknown): string {
  const status = String(value ?? "");
  return knownInstanceStatuses.has(status) ? status : "UNKNOWN";
}

function countUsageEvents(payload: Record<string, any>): UsageCounts {
  const events = Array.isArray(payload.events) ? payload.events : [];
  return {
    reasoning_llm: events.filter(
      (event: Record<string, any>) => event?.call_type === "reasoning_llm",
    ).length,
    text_to_image_model: events.filter(
      (event: Record<string, any>) => event?.call_type === "text_to_image_model",
    ).length,
    vision_language_model: events.filter(
      (event: Record<string, any>) => event?.call_type === "vision_language_model",
    ).length,
  };
}

async function redactedFailureDiagnostics(
  request: APIRequestContext,
  phase: GatePhase,
  advancedActions: string[],
  lastStatus: string,
): Promise<Record<string, unknown>> {
  let status = safeInstanceStatus(lastStatus);
  let usageCounts: UsageCounts = {
    reasoning_llm: 0,
    text_to_image_model: 0,
    vision_language_model: 0,
  };
  let usageAvailable = false;
  try {
    const response = await request.get(`/api/v1/instances/${instanceId}`, { timeout: 2_000 });
    if (response.ok()) {
      const detail = await response.json() as Record<string, any>;
      status = safeInstanceStatus(detail.instance?.status);
    }
  } catch {
    // Diagnostics are best effort and must never replace the original gate failure.
  }
  try {
    const response = await request.get(`/api/v1/tasks/${taskId}/usage`, { timeout: 2_000 });
    if (response.ok()) {
      usageCounts = countUsageEvents(await response.json() as Record<string, any>);
      usageAvailable = true;
    }
  } catch {
    // Do not include response bodies or exception messages: either could contain secrets.
  }
  return {
    phase,
    action_sequence: advancedActions.slice(0, maxWorkflowAdvances),
    last_status: status,
    usage_counts: usageCounts,
    usage_available: usageAvailable,
    answer_clarification_observed: advancedActions.includes("answer_clarification"),
  };
}

function clarificationAnswers(context: Record<string, any>): Record<string, unknown> {
  const card = context.question_card as Record<string, any> | undefined;
  const questions = Array.isArray(card?.questions) ? card.questions : [];
  if (!card?.question_card_id || questions.length === 0) {
    throw new Error("The current clarification approval has no answerable question card");
  }
  return {
    question_card_id: String(card.question_card_id),
    answers: questions.map((question: Record<string, any>) => {
      const options = Array.isArray(question.options) ? question.options : [];
      const recommended = options.find(
        (option: Record<string, any>) => option.option_id === question.recommended_option_id,
      ) ?? options[0];
      if (!recommended?.option_id) {
        throw new Error(`Clarification question ${question.question_id} has no legal option`);
      }
      return {
        question_id: String(question.question_id),
        selected_option_id: String(recommended.option_id),
        free_text: recommended.requires_free_text
          ? "按已登记任务卡约束完成本次生产链路验收。"
          : null,
        skipped: false,
      };
    }),
  };
}

function chooseApprovalAction(payload: Record<string, any>): string {
  const actions = Array.isArray(payload.available_actions)
    ? payload.available_actions.map(String)
    : [];
  const questionCard = payload.context?.question_card;
  const hasQuestions = Array.isArray(questionCard?.questions) && questionCard.questions.length > 0;
  const priority = [
    ...(hasQuestions ? ["answer_clarification", "answer_taskbook_revision"] : []),
    "apply_clarification_safe_defaults",
    "continue_clarification_after_budget_change",
    "apply_taskbook_scope_boundaries",
    "regenerate_taskbook",
    "approve_taskbook",
    "approve_category_constraint",
    "approve_skill_invocations",
    "select_master",
    "review_calibration",
    "approve_final",
    "start_category_match",
    "start_clarification",
    "build_taskbook",
    "prepare_style_direction",
    "render_candidates",
    "choose_master",
    "start_quality_inspection",
    "open_final_approval",
    "resume_quality_inspection",
  ];
  const action = priority.find((candidate) => actions.includes(candidate));
  if (!action) throw new Error(`No supported action in current approval: ${actions.join(", ")}`);
  return action;
}

function approvalPayload(action: string, context: Record<string, any>): Record<string, unknown> {
  if (action === "answer_clarification" || action === "answer_taskbook_revision") {
    return { clarification_answers: clarificationAnswers(context) };
  }
  if (action === "select_master") {
    const candidate = context.candidates?.[0];
    const selectedId = candidate?.id ?? candidate?.candidate_id;
    if (!selectedId) throw new Error("Master selection approval has no candidate id");
    return { selected_id: String(selectedId) };
  }
  if (action === "review_calibration") return { manual_action: "accept_current" };
  return {};
}

async function approveInUi(
  page: Page,
  approvalId: string,
  action: string,
  payload: Record<string, unknown>,
  deadlineMs: number,
): Promise<void> {
  await page.goto(`/instances/${instanceId}`, { timeout: remainingWorkflowBudget(deadlineMs) });
  await page.getByRole("link", { name: /前往收件箱/ }).click({
    timeout: remainingWorkflowBudget(deadlineMs),
  });
  const form = page.locator(`form[data-approval-id="${approvalId}"]`);
  await expect(form).toBeVisible({ timeout: remainingWorkflowBudget(deadlineMs) });
  await form.getByLabel("推进动作").selectOption(action, {
    timeout: remainingWorkflowBudget(deadlineMs),
  });
  await form.getByLabel("动作参数（JSON）").fill(JSON.stringify(payload), {
    timeout: remainingWorkflowBudget(deadlineMs),
  });
  await form.getByLabel("操作人 ID").fill("browser_operator", {
    timeout: remainingWorkflowBudget(deadlineMs),
  });
  await form.getByRole("button", { name: "批准并推进" }).click({
    timeout: remainingWorkflowBudget(deadlineMs),
  });
  await expect(form).toHaveCount(0, { timeout: remainingWorkflowBudget(deadlineMs) });
}

test("production build completes a real backend workflow without browser API mocks", async ({ page, request }) => {
  const deterministicProvider = process.env.HARNESS_BROWSER_REAL_PROVIDER !== "1";
  const workflowBudgetMs = deterministicProvider
    ? deterministicProviderWorkflowBudgetMs
    : realProviderWorkflowBudgetMs;
  if (!deterministicProvider && workflowBudgetMs < singleModelTimeoutMs) {
    throw new Error("The real-Provider workflow budget cannot contain one model timeout");
  }
  // The workflow itself has one absolute deadline. The runner gets only a small
  // reporting allowance so a deadline failure can emit the redacted diagnostics.
  test.setTimeout(workflowBudgetMs + failureDiagnosticsAllowanceMs);
  const workflowDeadlineMs = performance.now() + workflowBudgetMs;
  const imageModel = process.env.HARNESS_BROWSER_IMAGE_MODEL ?? "browser-image";
  const failedRequests: string[] = [];
  const advancedActions: string[] = [];
  let phase: GatePhase = "seed_workflow";
  let lastStatus = "UNKNOWN";
  page.on("requestfailed", (failed) => {
    if (failed.failure()?.errorText !== "net::ERR_ABORTED") {
      failedRequests.push(`${failed.method()} ${failed.url()}: ${failed.failure()?.errorText}`);
    }
  });

  try {
    await seedRealWorkflow(request, workflowDeadlineMs);

    let completed: Record<string, any> | undefined;
    for (let advance = 0; advance < maxWorkflowAdvances; advance += 1) {
      phase = "wait_for_boundary";
      const detail = await waitForBoundary(request, workflowDeadlineMs);
      const status = safeInstanceStatus(detail.instance.status);
      lastStatus = status;
      if (status === "SUCCEEDED") {
        completed = detail;
        break;
      }
      if (status !== "WAITING_APPROVAL") throw new Error("image_workflow_terminal");
      const approvalId = String(detail.pending_approval?.approval_id ?? "");
      if (!approvalId) throw new Error("pending_approval_missing");
      phase = "read_approval";
      const approval = await jsonRequest(
        request,
        "get",
        `/api/v1/approvals/${approvalId}`,
        undefined,
        workflowDeadlineMs,
      );
      const action = chooseApprovalAction(approval.payload);
      phase = "advance_approval";
      await approveInUi(
        page,
        approvalId,
        action,
        approvalPayload(action, approval.payload.context),
        workflowDeadlineMs,
      );
      advancedActions.push(action);
    }

    phase = "assert_completion";
    expect(completed?.instance.status).toBe("SUCCEEDED");
    phase = "assert_clarification";
    expect(advancedActions).toContain("answer_clarification");

    phase = "assert_usage";
    const usage = await jsonRequest(
      request,
      "get",
      `/api/v1/tasks/${taskId}/usage`,
      undefined,
      workflowDeadlineMs,
    );
    expect(usage.completeness).toBe("COMPLETE");
    const textEvents = usage.events.filter(
      (event: Record<string, any>) => event.call_type === "reasoning_llm",
    );
    expect(textEvents.length).toBeGreaterThan(0);
    expect(textEvents.every((event: Record<string, any>) =>
      event.usage_basis === "tokens" && event.total_tokens > 0,
    )).toBe(true);
    const imageEvents = usage.events.filter(
      (event: Record<string, any>) => event.call_type === "text_to_image_model",
    );
    if (deterministicProvider) expect(imageEvents).toHaveLength(5);
    else expect(imageEvents.length).toBeGreaterThan(0);
    expect(imageEvents.every((event: Record<string, any>) =>
      event.usage_basis === "image_units"
        && event.total_tokens === 0
        && event.billing_units[0].unit === "image"
        && event.billing_units[0].attributes.resolution === "2560x1440",
    )).toBe(true);
    const vlmEvents = usage.events.filter(
      (event: Record<string, any>) => event.call_type === "vision_language_model",
    );
    expect(vlmEvents.length).toBeGreaterThan(0);
    expect(vlmEvents.every((event: Record<string, any>) =>
      event.usage_basis === "tokens" && event.total_tokens > 0,
    )).toBe(true);
    expect(usage.tokens.total_tokens).toBeGreaterThan(0);
    expect(usage.cost.completeness).toBe("UNKNOWN");

    phase = "assert_usage_ui";
    await page.goto(`/tasks/${taskId}/usage`, {
      timeout: remainingWorkflowBudget(workflowDeadlineMs),
    });
    await expect(page.getByRole("heading", { name: "用量观测" })).toBeVisible({
      timeout: remainingWorkflowBudget(workflowDeadlineMs),
    });
    await expect(page.getByText("完整上报", { exact: true }).first()).toBeVisible({
      timeout: remainingWorkflowBudget(workflowDeadlineMs),
    });
    await page.getByText(/查看最近调用/).click({
      timeout: remainingWorkflowBudget(workflowDeadlineMs),
    });
    await expect(page.getByText(`1 张图片 · 2560x1440 · ${imageModel}`).first()).toBeVisible({
      timeout: remainingWorkflowBudget(workflowDeadlineMs),
    });

    phase = "assert_delivery_ui";
    await page.goto(`/tasks/${taskId}/resources`, {
      timeout: remainingWorkflowBudget(workflowDeadlineMs),
    });
    await expect(page.getByRole("heading", { name: "任务文件" })).toBeVisible({
      timeout: remainingWorkflowBudget(workflowDeadlineMs),
    });
    const shared = page.locator("section.resource-group").filter({ hasText: "公共交付" });
    await expect(shared.locator("article.resource-card")).toHaveCount(1, {
      timeout: remainingWorkflowBudget(workflowDeadlineMs),
    });
    await expect(shared.getByText("VERIFIED", { exact: true })).toBeVisible({
      timeout: remainingWorkflowBudget(workflowDeadlineMs),
    });
    expect(failedRequests).toEqual([]);
  } catch {
    const diagnostics = await redactedFailureDiagnostics(
      request,
      phase,
      advancedActions,
      lastStatus,
    );
    throw new Error(
      `Real-provider smoke failed; redacted diagnostics only: ${JSON.stringify(diagnostics)}`,
    );
  }
});
