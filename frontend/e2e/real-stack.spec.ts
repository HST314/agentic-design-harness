import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const taskId = "t_browser_real_stack";
const instanceId = "i_browser_real_stack";
const maxWorkflowAdvances = 12;

async function jsonRequest(
  request: APIRequestContext,
  method: "get" | "post" | "put",
  path: string,
  data?: Record<string, unknown>,
): Promise<Record<string, any>> {
  const response = await request[method](path, data ? { data } : undefined);
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

async function seedRealWorkflow(request: APIRequestContext): Promise<void> {
  const providerUrl = process.env.HARNESS_BROWSER_PROVIDER_URL;
  if (!providerUrl) throw new Error("HARNESS_BROWSER_PROVIDER_URL is required");
  const textModel = process.env.HARNESS_BROWSER_TEXT_MODEL ?? "browser-text";
  const imageModel = process.env.HARNESS_BROWSER_IMAGE_MODEL ?? "browser-image";
  const vlmModel = process.env.HARNESS_BROWSER_VLM_MODEL ?? "browser-vlm";

  const current = await jsonRequest(request, "get", "/api/v1/config/global");
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
  });
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
  });

  await jsonRequest(request, "post", "/api/v1/tasks", {
    task_id: taskId,
    title: "生产链路无 Mock 验收",
    goal: "通过生产构建、真实 Harness 与确定性 Provider 交付图片。",
    master_owner: "master_default",
    start_policy: "manual",
    input_manifest: "inputs/manifests/browser-real-stack.json",
    envelope: envelope("create-browser-real-stack", 0),
  });
  const imported = await jsonRequest(request, "post", `/api/v1/tasks/${taskId}/assets`, {
    filename: "brief.md",
    content_base64: Buffer.from("# 生产链路验收任务书\n").toString("base64"),
    description: "浏览器真实链路的受控输入。",
    operation_id: "import_browser_brief",
    envelope: envelope("import-browser-brief", 1),
  });
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
  });
  await jsonRequest(request, "post", `/api/v1/tasks/${taskId}/confirm-start`, {
    operation_id: "start_browser_real_stack",
    envelope: envelope("start-browser-real-stack", Number(plan.task_revision)),
  });
}

async function waitForBoundary(request: APIRequestContext): Promise<Record<string, any>> {
  let latest: Record<string, any> | undefined;
  await expect.poll(async () => {
    latest = await jsonRequest(request, "get", `/api/v1/instances/${instanceId}`);
    return ["WAITING_APPROVAL", "SUCCEEDED", "FAILED"].includes(
      String(latest.instance.status),
    );
  }, { timeout: 90_000 }).toBe(true);
  return latest as Record<string, any>;
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
): Promise<void> {
  await page.goto(`/instances/${instanceId}`);
  await page.getByRole("link", { name: /前往收件箱/ }).click();
  const form = page.locator(`form[data-approval-id="${approvalId}"]`);
  await expect(form).toBeVisible();
  await form.getByLabel("推进动作").selectOption(action);
  await form.getByLabel("动作参数（JSON）").fill(JSON.stringify(payload));
  await form.getByLabel("操作人 ID").fill("browser_operator");
  await form.getByRole("button", { name: "批准并推进" }).click();
  await expect(form).toHaveCount(0);
}

test("production build completes a real backend workflow without browser API mocks", async ({ page, request }) => {
  const deterministicProvider = process.env.HARNESS_BROWSER_REAL_PROVIDER !== "1";
  test.setTimeout(deterministicProvider ? 120_000 : 600_000);
  const imageModel = process.env.HARNESS_BROWSER_IMAGE_MODEL ?? "browser-image";
  const failedRequests: string[] = [];
  page.on("requestfailed", (failed) => {
    if (failed.failure()?.errorText !== "net::ERR_ABORTED") {
      failedRequests.push(`${failed.method()} ${failed.url()}: ${failed.failure()?.errorText}`);
    }
  });

  await seedRealWorkflow(request);

  const advancedActions: string[] = [];
  let completed: Record<string, any> | undefined;
  for (let advance = 0; advance < maxWorkflowAdvances; advance += 1) {
    const detail = await waitForBoundary(request);
    const status = String(detail.instance.status);
    if (status === "SUCCEEDED") {
      completed = detail;
      break;
    }
    if (status === "FAILED") throw new Error(`Image workflow failed: ${JSON.stringify(detail)}`);
    const approvalId = String(detail.pending_approval?.approval_id ?? "");
    if (!approvalId) throw new Error("Waiting Image workflow has no pending approval");
    const approval = await jsonRequest(request, "get", `/api/v1/approvals/${approvalId}`);
    const action = chooseApprovalAction(approval.payload);
    await approveInUi(page, approvalId, action, approvalPayload(action, approval.payload.context));
    advancedActions.push(action);
  }
  expect(completed?.instance.status, {
    message: `workflow did not succeed within ${maxWorkflowAdvances} advances: ${advancedActions.join(", ")}`,
  }).toBe("SUCCEEDED");
  if (deterministicProvider) expect(advancedActions).toContain("answer_clarification");

  const usage = await jsonRequest(request, "get", `/api/v1/tasks/${taskId}/usage`);
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

  await page.goto(`/tasks/${taskId}/usage`);
  await expect(page.getByRole("heading", { name: "用量观测" })).toBeVisible();
  await expect(page.getByText("完整上报", { exact: true }).first()).toBeVisible();
  await page.getByText(/查看最近调用/).click();
  await expect(page.getByText(`1 张图片 · 2560x1440 · ${imageModel}`).first()).toBeVisible();

  await page.goto(`/tasks/${taskId}/resources`);
  await expect(page.getByRole("heading", { name: "任务文件" })).toBeVisible();
  const shared = page.locator("section.resource-group").filter({ hasText: "公共交付" });
  await expect(shared.locator("article.resource-card")).toHaveCount(1);
  await expect(shared.getByText("VERIFIED", { exact: true })).toBeVisible();
  expect(failedRequests).toEqual([]);
});
