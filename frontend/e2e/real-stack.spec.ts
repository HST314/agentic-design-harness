import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { mkdir, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
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
  | "assert_favicon"
  | "seed_workflow"
  | "wait_for_boundary"
  | "read_approval"
  | "advance_approval"
  | "assert_completion"
  | "assert_clarification"
  | "assert_usage_completeness"
  | "assert_text_usage"
  | "assert_image_usage"
  | "assert_vlm_usage"
  | "assert_cost_completeness"
  | "assert_usage_ui"
  | "assert_delivery_ui"
  | "persist_evidence";

type UsageCounts = {
  reasoning_llm: number;
  text_to_image_model: number;
  vision_language_model: number;
};

type BrowserErrorCounts = {
  failed_requests: number;
  console_errors: number;
  page_errors: number;
  http_error_responses: number;
};

type BrowserErrorDiagnostics = BrowserErrorCounts & {
  console_error_codes: string[];
  page_error_codes: string[];
  http_error_routes: string[];
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
      parameters: {
        variants: 1,
        usage_context: "P1 no-mock browser acceptance",
        category_id: "generic_visual_delivery",
        category_version: "1.0",
      },
      created_at: new Date().toISOString(),
    }],
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
  lastApprovalActions: string[],
  lastStatus: string,
  browserErrors: BrowserErrorDiagnostics,
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
    last_approval_actions: lastApprovalActions.slice(0, maxWorkflowAdvances),
    last_status: status,
    usage_counts: usageCounts,
    usage_available: usageAvailable,
    answer_clarification_observed: advancedActions.includes("answer_clarification"),
    browser_error_counts: browserErrors,
  };
}

function consoleErrorCode(message: string): string {
  const normalized = message.toLowerCase();
  if (normalized.includes("failed to load resource")) return "RESOURCE_LOAD_ERROR";
  if (normalized.includes("content security policy")) return "CONTENT_SECURITY_POLICY_ERROR";
  if (normalized.includes("uncaught")) return "UNCAUGHT_CONSOLE_ERROR";
  return "OTHER_CONSOLE_ERROR";
}

function localErrorRoute(status: number, method: string, rawUrl: string): string {
  try {
    const url = new URL(rawUrl);
    const local = url.hostname === "127.0.0.1" || url.hostname === "localhost";
    return `${status} ${method} ${local ? url.pathname : "EXTERNAL_ORIGIN"}`;
  } catch {
    return `${status} ${method} INVALID_URL`;
  }
}

async function assertFaviconLoads(page: Page, deadlineMs: number): Promise<void> {
  await page.goto("/", {
    waitUntil: "domcontentloaded",
    timeout: remainingWorkflowBudget(deadlineMs),
  });
  await expect(page.locator('link[rel~="icon"]')).toHaveAttribute("href", "/favicon.svg");
  const favicon = await page.evaluate(async () => {
    const link = document.querySelector<HTMLLinkElement>('link[rel~="icon"]');
    if (!link) return null;
    const response = await fetch(link.href);
    return {
      path: new URL(link.href).pathname,
      status: response.status,
      contentType: response.headers.get("content-type"),
    };
  });
  expect(favicon).toEqual({
    path: "/favicon.svg",
    status: 200,
    contentType: expect.stringContaining("image/svg+xml"),
  });
}

function usageCallEvidence(
  usage: Record<string, any>,
  callType: keyof UsageCounts,
): Record<string, number> {
  const events = Array.isArray(usage.events)
    ? usage.events.filter((event: Record<string, any>) => event?.call_type === callType)
    : [];
  return {
    event_count: events.length,
    total_tokens: events.reduce(
      (total: number, event: Record<string, any>) => total + Number(event.total_tokens ?? 0),
      0,
    ),
    image_units: events.reduce(
      (total: number, event: Record<string, any>) => total + (
        Array.isArray(event.billing_units)
          ? event.billing_units
              .filter((unit: Record<string, any>) => unit?.unit === "image")
              .reduce(
                (units: number, unit: Record<string, any>) => units + Number(unit.quantity ?? 0),
                0,
              )
          : 0
      ),
      0,
    ),
  };
}

async function persistRedactedEvidence(
  usage: Record<string, any>,
  delivery: Record<string, any>,
  deliveryStatus: string,
  publicDeliveryCount: number,
  advancedActions: string[],
  browserErrors: BrowserErrorCounts,
  startedAtMs: number,
): Promise<void> {
  const evidencePath = process.env.HARNESS_BROWSER_EVIDENCE_PATH;
  if (!evidencePath) return;
  const payload = {
    schema_version: "real-provider-browser-evidence.v2",
    generated_at_utc: new Date().toISOString(),
    baseline: {
      harness_commit: process.env.HARNESS_BROWSER_HARNESS_COMMIT ?? "UNKNOWN",
      image_agent_commit: process.env.HARNESS_BROWSER_IMAGE_AGENT_COMMIT ?? "UNKNOWN",
      harness_worktree_clean: process.env.HARNESS_BROWSER_HARNESS_CLEAN === "1",
      image_agent_worktree_clean: process.env.HARNESS_BROWSER_IMAGE_AGENT_CLEAN === "1",
      playwright_version: process.env.HARNESS_BROWSER_PLAYWRIGHT_VERSION ?? "UNKNOWN",
      chromium_revision: process.env.HARNESS_BROWSER_CHROMIUM_REVISION ?? "UNKNOWN",
      browser_version: process.env.HARNESS_BROWSER_VERSION ?? "UNKNOWN",
    },
    execution: {
      provider_mode: process.env.HARNESS_BROWSER_REAL_PROVIDER === "1"
        ? "real_external"
        : "deterministic_local",
      browser_api_mocked: false,
      production_frontend_build: true,
      real_harness_process: true,
      real_image_agent_subprocess: true,
      result: "PASSED",
      duration_ms: Math.round(performance.now() - startedAtMs),
    },
    models: {
      reasoning_llm: process.env.HARNESS_BROWSER_TEXT_MODEL ?? "UNKNOWN",
      text_to_image_model: process.env.HARNESS_BROWSER_IMAGE_MODEL ?? "UNKNOWN",
      vision_language_model: process.env.HARNESS_BROWSER_VLM_MODEL ?? "UNKNOWN",
    },
    assertions: {
      instance_terminal_status: "SUCCEEDED",
      action_sequence: advancedActions,
      answer_clarification_observed: advancedActions.includes("answer_clarification"),
      usage_completeness: String(usage.completeness),
      cost_completeness: String(usage.cost?.completeness ?? "UNKNOWN"),
      usage: {
        reasoning_llm: usageCallEvidence(usage, "reasoning_llm"),
        text_to_image_model: usageCallEvidence(usage, "text_to_image_model"),
        vision_language_model: usageCallEvidence(usage, "vision_language_model"),
      },
      public_delivery_count: publicDeliveryCount,
      public_delivery: {
        mime_type: String(delivery.mime_type),
        sha256: String(delivery.sha256),
        integrity_status: deliveryStatus,
      },
      browser_error_counts: browserErrors,
      favicon: {
        path: "/favicon.svg",
        status: 200,
        content_type: "image/svg+xml",
      },
    },
    secrets: {
      credential_value_recorded: false,
      provider_url_recorded: false,
      provider_response_body_recorded: false,
    },
  };
  await mkdir(dirname(evidencePath), { recursive: true });
  const temporaryPath = `${evidencePath}.tmp-${process.pid}`;
  await writeFile(temporaryPath, `${JSON.stringify(payload, null, 2)}\n`, {
    encoding: "utf-8",
    mode: 0o600,
  });
  await rename(temporaryPath, evidencePath);
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
    "publish_bundle",
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
  const startedAtMs = performance.now();
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
  const consoleErrorCodes: string[] = [];
  const pageErrorCodes: string[] = [];
  const httpErrorRoutes: string[] = [];
  let lastApprovalActions: string[] = [];
  let consoleErrorCount = 0;
  let pageErrorCount = 0;
  let phase: GatePhase = "seed_workflow";
  let lastStatus = "UNKNOWN";
  page.on("requestfailed", (failed) => {
    if (failed.failure()?.errorText !== "net::ERR_ABORTED") {
      failedRequests.push(`${failed.method()} ${failed.url()}: ${failed.failure()?.errorText}`);
    }
  });
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrorCount += 1;
      consoleErrorCodes.push(consoleErrorCode(message.text()));
    }
  });
  page.on("pageerror", (error) => {
    pageErrorCount += 1;
    pageErrorCodes.push(error.name || "UNKNOWN_PAGE_ERROR");
  });
  page.on("response", (response) => {
    if (response.status() >= 400) {
      httpErrorRoutes.push(
        localErrorRoute(response.status(), response.request().method(), response.url()),
      );
    }
  });

  try {
    phase = "assert_favicon";
    await assertFaviconLoads(page, workflowDeadlineMs);
    phase = "seed_workflow";
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
      lastApprovalActions = Array.isArray(approval.payload.available_actions)
        ? approval.payload.available_actions.map(String)
        : [];
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

    phase = "assert_usage_completeness";
    const usage = await jsonRequest(
      request,
      "get",
      `/api/v1/tasks/${taskId}/usage`,
      undefined,
      workflowDeadlineMs,
    );
    expect(usage.completeness).toBe("COMPLETE");
    phase = "assert_text_usage";
    const textEvents = usage.events.filter(
      (event: Record<string, any>) => event.call_type === "reasoning_llm",
    );
    expect(textEvents.length).toBeGreaterThan(0);
    expect(textEvents.every((event: Record<string, any>) =>
      event.usage_basis === "tokens" && event.total_tokens > 0,
    )).toBe(true);
    phase = "assert_image_usage";
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
    phase = "assert_vlm_usage";
    const vlmEvents = usage.events.filter(
      (event: Record<string, any>) => event.call_type === "vision_language_model",
    );
    expect(vlmEvents.length).toBeGreaterThan(0);
    expect(vlmEvents.every((event: Record<string, any>) =>
      event.usage_basis === "tokens" && event.total_tokens > 0,
    )).toBe(true);
    expect(usage.tokens.total_tokens).toBeGreaterThan(0);
    phase = "assert_cost_completeness";
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
    await expect(shared.locator("article.resource-card")).toHaveCount(2, {
      timeout: remainingWorkflowBudget(workflowDeadlineMs),
    });
    await expect(shared.getByText("VERIFIED", { exact: true })).toHaveCount(2, {
      timeout: remainingWorkflowBudget(workflowDeadlineMs),
    });
    const sharedFiles = await jsonRequest(
      request,
      "get",
      `/api/v1/tasks/${taskId}/files?group=shared`,
      undefined,
      workflowDeadlineMs,
    );
    const publicAssets = sharedFiles.assets.filter(
      (asset: Record<string, any>) => asset?.manifest?.producer_instance_id === instanceId,
    );
    expect(publicAssets).toHaveLength(2);
    expect(publicAssets.every(
      (asset: Record<string, any>) => asset.integrity_status === "VERIFIED",
    )).toBe(true);
    expect(new Set(publicAssets.map(
      (asset: Record<string, any>) => String(asset.manifest.role),
    ))).toEqual(new Set(["final_artwork", "design_note"]));
    const artworkAsset = publicAssets.find(
      (asset: Record<string, any>) => asset.manifest.role === "final_artwork",
    );
    const designNoteAsset = publicAssets.find(
      (asset: Record<string, any>) => asset.manifest.role === "design_note",
    );
    expect(artworkAsset?.manifest.mime_type).toBe("image/png");
    expect(designNoteAsset?.manifest.mime_type).toBe("text/markdown");
    const delivery = sharedFiles.items.find(
      (item: Record<string, any>) =>
        item?.relative_path === artworkAsset?.manifest.relative_path,
    );
    expect(delivery).toBeDefined();
    const browserErrors: BrowserErrorCounts = {
      failed_requests: failedRequests.length,
      console_errors: consoleErrorCount,
      page_errors: pageErrorCount,
      http_error_responses: httpErrorRoutes.length,
    };
    expect(browserErrors.failed_requests).toBe(0);
    expect(browserErrors.console_errors).toBe(0);
    expect(browserErrors.page_errors).toBe(0);
    expect(browserErrors.http_error_responses).toBe(0);

    phase = "persist_evidence";
    await persistRedactedEvidence(
      usage,
      delivery,
      String(artworkAsset?.integrity_status),
      publicAssets.length,
      advancedActions,
      browserErrors,
      startedAtMs,
    );
  } catch {
    const diagnostics = await redactedFailureDiagnostics(
      request,
      phase,
      advancedActions,
      lastApprovalActions,
      lastStatus,
      {
        failed_requests: failedRequests.length,
        console_errors: consoleErrorCount,
        page_errors: pageErrorCount,
        http_error_responses: httpErrorRoutes.length,
        console_error_codes: consoleErrorCodes,
        page_error_codes: pageErrorCodes,
        http_error_routes: httpErrorRoutes,
      },
    );
    throw new Error(
      `Real-provider smoke failed; redacted diagnostics only: ${JSON.stringify(diagnostics)}`,
    );
  }
});
