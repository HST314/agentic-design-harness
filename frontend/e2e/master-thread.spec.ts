import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }, testInfo) => {
  const startPolicy: "manual" | "auto" = testInfo.title.includes("auto mode") ? "auto" : "manual";
  let threadRevision = 4;
  let confirmed = false;
  let busy = false;
  let proposalRevision = 1;
  let latestProposalStatus: "PENDING_CONFIRMATION" | "CONFIRMED" = "PENDING_CONFIRMATION";
  let cardRevision = 1;
  let peerCardRevision = 1;
  let cardObjective = "生成自然光主视觉方向。";
  let peerCardObjective = "生成棚拍光主视觉方向。";
  let cardInstructions = ["保持品牌安全区。"];
  let peerCardInstructions = ["保持主体轮廓清晰。"];
  let instancePhase: "idle" | "starting" | "running" = "idle";
  let peerInstancePhase: "idle" | "starting" | "running" = "idle";
  let startPolls = 0;
  const messages = [
    {
      schema_version: "1.0",
      message_id: "message_brief",
      task_id: "task_master_e2e",
      sequence: 1,
      role: "user",
      kind: "text",
      content: "为秋季发布会生成主视觉。",
      asset_refs: [{ asset_id: "asset_brief", manifest_relpath: "inputs/manifests/asset_brief.json" }],
      created_at: "2026-08-22T10:00:00Z",
    },
    {
      schema_version: "1.0",
      message_id: "message_plan",
      task_id: "task_master_e2e",
      sequence: 2,
      role: "master",
      kind: "plan_proposal",
      content: "已生成两条视觉探索路径，请审阅。",
      asset_refs: [],
      created_at: "2026-08-22T10:01:00Z",
    },
  ];
  const proposal = (overrides: { revision?: number; cardRev?: number; peerCardRev?: number; status?: string } = {}) => {
    const rev = overrides.revision ?? proposalRevision;
    const cardRev = overrides.cardRev ?? cardRevision;
    const peerCardRev = overrides.peerCardRev ?? peerCardRevision;
    const status = overrides.status ?? latestProposalStatus;
    return {
      schema_version: "1.0",
      proposal_id: `proposal_master_e2e_${rev}`,
      task_id: "task_master_e2e",
      revision: rev,
      status,
      stages: [{ stage_id: "stage_image", type: "image", position: 1, depends_on: [], required: true }],
      work_items: [
        {
          schema_version: "1.0",
          work_item_id: "work_direction_a",
          task_id: "task_master_e2e",
          stage_id: "stage_image",
          title: "自然光主视觉方向",
          agent_type: "image",
          required: true,
          depends_on: [],
          current_instance_id: "instance_direction_a",
          instance_ids: ["instance_direction_a"],
          task_card_ids: ["card_direction_a"],
        },
        {
          schema_version: "1.0",
          work_item_id: "work_direction_b",
          task_id: "task_master_e2e",
          stage_id: "stage_image",
          title: "棚拍光主视觉方向",
          agent_type: "image",
          required: true,
          depends_on: [],
          current_instance_id: "instance_direction_b",
          instance_ids: ["instance_direction_b"],
          task_card_ids: ["card_direction_b"],
        },
      ],
      execution_cards: [
        {
          schema_version: "1.1",
          card_id: "card_direction_a",
          revision: cardRev,
          task_id: "task_master_e2e",
          stage_id: "stage_image",
          instance_id: "instance_direction_a",
          agent_type: "image",
          objective: cardObjective,
          instructions: cardInstructions,
          input_assets: [{ asset_id: "asset_brief", manifest_relpath: "inputs/manifests/asset_brief.json" }],
          expected_deliveries: [{ kind: "image", role: "key_visual", required: true, accepted_mime_types: ["image/png"] }],
          parameters: { usage_context: "发布会主屏", variants: 3 },
          created_at: "2026-08-22T10:01:00Z",
        },
        {
          schema_version: "1.1",
          card_id: "card_direction_b",
          revision: peerCardRev,
          task_id: "task_master_e2e",
          stage_id: "stage_image",
          instance_id: "instance_direction_b",
          agent_type: "image",
          objective: peerCardObjective,
          instructions: peerCardInstructions,
          input_assets: [{ asset_id: "asset_brief", manifest_relpath: "inputs/manifests/asset_brief.json" }],
          expected_deliveries: [{ kind: "image", role: "key_visual", required: true, accepted_mime_types: ["image/png"] }],
          parameters: { usage_context: "发布会侧屏", variants: 2 },
          created_at: "2026-08-22T10:01:00Z",
        },
      ],
      created_at: "2026-08-22T10:01:00Z",
      updated_at: "2026-08-22T10:02:00Z",
      confirmed_at: status === "CONFIRMED" ? "2026-08-22T10:02:00Z" : null,
    };
  };
  const proposals = () => {
    const list = [];
    if (proposalRevision > 1) {
      list.push({ ...proposal({ revision: 1, cardRev: 1, peerCardRev: 1, status: "SUPERSEDED" }), message_id: "message_plan" });
    }
    list.push({ ...proposal(), message_id: proposalRevision > 1 ? "message_card_revision" : "message_plan" });
    return list;
  };
  const session = () => ({
    schema_version: "1.0",
    thread: {
      schema_version: "1.0",
      task_id: "task_master_e2e",
      latest_sequence: messages.length,
      latest_proposal_revision: proposalRevision,
      active_run: busy ? { run_id: "run_adjust", message_id: "message_adjust", status: "RUNNING", started_at: "2026-08-22T10:03:00Z", updated_at: "2026-08-22T10:03:00Z" } : { run_id: "run_plan", message_id: "message_brief", status: "PLAN_READY", started_at: "2026-08-22T10:00:00Z", updated_at: "2026-08-22T10:01:00Z" },
      last_error: null,
      revision: threadRevision,
      created_at: "2026-08-22T10:00:00Z",
      updated_at: "2026-08-22T10:02:00Z",
    },
    thread_revision: threadRevision,
    messages,
    latest_proposal: proposal(),
    proposals: proposals(),
    editable_card_ids: !confirmed
      ? ["card_direction_a", "card_direction_b"]
      : [
        ...(instancePhase === "idle" ? ["card_direction_a"] : []),
        ...(peerInstancePhase === "idle" ? ["card_direction_b"] : []),
      ],
    instance_statuses: !confirmed ? {} : {
      instance_direction_a: instancePhase === "running" ? "RUNNING" : instancePhase === "starting" ? "STARTING" : "READY",
      instance_direction_b: peerInstancePhase === "running" ? "RUNNING" : peerInstancePhase === "starting" ? "STARTING" : "READY",
    },
    task: {
      schema_version: "1.0",
      task_id: "task_master_e2e",
      title: "秋季发布会主视觉",
      goal: "为秋季发布会生成主视觉。",
      master_owner: "master_default",
      start_policy: startPolicy,
      status: confirmed ? "RUNNING" : "DRAFT",
      created_at: "2026-08-22T10:00:00Z",
      updated_at: "2026-08-22T10:02:00Z",
      input_manifest: "inputs/manifests/selected.json",
      plan_revision: 1,
    },
    task_revision: confirmed ? 4 : 2,
    gateway_available: true,
    assets: [{ asset_id: "asset_brief", filename: "brief.md", description: "品牌与活动约束", manifest_relpath: "inputs/manifests/asset_brief.json" }],
  });

  await page.route("**/readyz", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ready" }) }));
  await page.route("**/api/v1/tasks", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", items: [{ task_id: "task_master_e2e", status: confirmed ? "RUNNING" : "DRAFT", title: "秋季发布会主视觉", updated_at: "2026-08-22T10:02:00Z", revision: confirmed ? 4 : 2 }] }) }));
  await page.route("**/api/v1/task-intakes/task_master_e2e", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", intake: { schema_version: "1.0", task_id: "task_master_e2e", prompt: "为秋季发布会生成主视觉。", upload_session: { session_id: "upload_master", status: "LOCKED", accepted_mime_types: ["text/markdown"], max_files: 20, max_total_bytes: 209715200 }, asset_ids: ["asset_brief"], status: "SUBMITTED", start_policy: startPolicy, revision: 3, created_at: "2026-08-22T10:00:00Z", updated_at: "2026-08-22T10:01:00Z", submitted_at: "2026-08-22T10:01:00Z" }, intake_revision: 3, task: session().task, task_revision: session().task_revision, assets: [] }) }));
  await page.route("**/api/v1/instances/instance_direction_a*", async (route) => {
    if (instancePhase === "starting") {
      startPolls += 1;
      if (startPolls >= 3) instancePhase = "running";
    }
    const running = instancePhase === "running";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        task_id: "task_master_e2e",
        task_revision: 4,
        instance: {
          instance_id: "instance_direction_a",
          task_id: "task_master_e2e",
          stage_id: "stage_image",
          agent_type: "image",
          required: true,
          status: running ? "RUNNING" : "READY",
          start_failure: null,
        },
        observation: null,
        pending_approval: null,
        start_operation_id: confirmed ? "start_master_e2e" : null,
        start_progress: confirmed ? {
          state: running ? "RUNNING" : "PREPARING",
          attempt: 1,
          launch_id: null,
          side_effect_stage: "NONE",
          last_error: null,
          updated_at: "2026-08-22T10:02:01Z",
        } : null,
        start_in_progress: instancePhase === "starting",
        start_retry_allowed: false,
      }),
    });
  });
  await page.route("**/api/v1/instances/instance_direction_b*", async (route) => {
    const running = peerInstancePhase === "running";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        task_id: "task_master_e2e",
        task_revision: 4,
        instance: {
          instance_id: "instance_direction_b",
          task_id: "task_master_e2e",
          stage_id: "stage_image",
          agent_type: "image",
          required: true,
          status: running ? "RUNNING" : peerInstancePhase === "starting" ? "STARTING" : "READY",
          start_failure: null,
        },
        observation: null,
        pending_approval: null,
        start_operation_id: peerInstancePhase === "idle" ? null : "start_master_e2e_b",
        start_progress: peerInstancePhase === "idle" ? null : {
          state: running ? "RUNNING" : "PREPARING",
          attempt: 1,
          launch_id: null,
          side_effect_stage: "NONE",
          last_error: null,
          updated_at: "2026-08-22T10:02:01Z",
        },
        start_in_progress: peerInstancePhase === "starting",
        start_retry_allowed: false,
      }),
    });
  });
  await page.route("**/api/v1/tasks/task_master_e2e/master/messages", async (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() as { content: string; asset_refs: unknown[]; envelope: { expected_revision: number } };
      expect(body.content).toContain("调整计划 r1");
      expect(body.asset_refs).toHaveLength(1);
      expect(body.envelope.expected_revision).toBe(threadRevision);
      messages.push({ schema_version: "1.0", message_id: "message_adjust", task_id: "task_master_e2e", sequence: 3, role: "user", kind: "text", content: body.content, asset_refs: [{ asset_id: "asset_brief", manifest_relpath: "inputs/manifests/asset_brief.json" }], created_at: "2026-08-22T10:03:00Z" });
      threadRevision += 1;
      busy = true;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(session()) });
  });
  await page.route("**/api/v1/tasks/task_master_e2e/plan-proposals/*/task-cards/card_direction_a", async (route) => {
    const body = route.request().postDataJSON() as {
      objective: string;
      instructions: string[];
      expected_proposal_revision: number;
      expected_card_revision: number;
      envelope: { expected_revision: number };
    };
    expect(body.expected_proposal_revision).toBe(proposalRevision);
    expect(body.expected_card_revision).toBe(cardRevision);
    expect(body.envelope.expected_revision).toBe(proposalRevision);
    proposalRevision += 1;
    cardRevision += 1;
    cardObjective = body.objective;
    cardInstructions = body.instructions;
    latestProposalStatus = "PENDING_CONFIRMATION";
    threadRevision += 1;
    messages.push({ schema_version: "1.0", message_id: "message_card_revision", task_id: "task_master_e2e", sequence: messages.length + 1, role: "system", kind: "plan_proposal", content: `任务卡 card_direction_a 已保存为 r${cardRevision}，计划已更新为 r${proposalRevision}，请审阅未启动任务卡后继续。`, asset_refs: [], created_at: "2026-08-22T10:02:30Z" });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(session()) });
  });
  await page.route("**/api/v1/tasks/task_master_e2e/plan-proposals/*/task-cards/card_direction_b", async (route) => {
    const body = route.request().postDataJSON() as {
      objective: string;
      instructions: string[];
      expected_proposal_revision: number;
      expected_card_revision: number;
      envelope: { expected_revision: number };
    };
    expect(body.expected_proposal_revision).toBe(proposalRevision);
    expect(body.expected_card_revision).toBe(peerCardRevision);
    expect(body.envelope.expected_revision).toBe(proposalRevision);
    proposalRevision += 1;
    peerCardRevision += 1;
    peerCardObjective = body.objective;
    peerCardInstructions = body.instructions;
    latestProposalStatus = "PENDING_CONFIRMATION";
    threadRevision += 1;
    messages.push({ schema_version: "1.0", message_id: "message_peer_card_revision", task_id: "task_master_e2e", sequence: messages.length + 1, role: "system", kind: "plan_proposal", content: `任务卡 card_direction_b 已保存为 r${peerCardRevision}，计划已更新为 r${proposalRevision}。`, asset_refs: [], created_at: "2026-08-22T10:02:30Z" });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(session()) });
  });
  await page.route("**/api/v1/tasks/task_master_e2e/plan-proposals/*/confirm", async (route) => {
    const body = route.request().postDataJSON() as { task_expected_revision: number; expected_card_revisions: Record<string, number>; instance_ids?: string[]; envelope: { expected_revision: number } };
    expect(body.task_expected_revision).toBe(confirmed ? 4 : 2);
    expect(body.expected_card_revisions).toEqual({
      card_direction_a: cardRevision,
      card_direction_b: peerCardRevision,
    });
    expect(body.envelope.expected_revision).toBe(proposalRevision);
    expect(body.instance_ids).toHaveLength(1);
    confirmed = true;
    if (body.instance_ids?.[0] === "instance_direction_a") instancePhase = "starting";
    if (body.instance_ids?.[0] === "instance_direction_b") peerInstancePhase = "starting";
    latestProposalStatus = "CONFIRMED";
    startPolls = 0;
    messages.push({ schema_version: "1.0", message_id: "message_confirm", task_id: "task_master_e2e", sequence: messages.length + 1, role: "system", kind: "plan_confirmation", content: "计划 r1 已确认。", asset_refs: [], created_at: "2026-08-22T10:02:00Z" });
    threadRevision += 1;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", proposal: proposal(), plan_result: {}, start_result: { launches: [{ instance_id: "instance_direction_a" }] }, session: session() }) });
  });
});

test("shows plan cards inside the message flow and sends revision feedback with existing resources", async ({ page }) => {
  await page.goto("/tasks/task_master_e2e/master");
  await expect(page.locator(".workbench-topbar")).toContainText("秋季发布会主视觉");
  await expect(page.getByRole("log", { name: "Master 消息记录" })).toContainText("已生成两条视觉探索路径");

  const group = page.getByRole("region", { name: "计划 r1 任务卡" });
  await expect(group).toBeVisible();
  await expect(group.getByText("图片", { exact: true }).first()).toBeVisible();
  await expect(group.getByRole("heading", { name: "自然光主视觉方向" })).toBeVisible();
  await expect(group.getByRole("button", { name: "启动 自然光主视觉方向" })).toBeVisible();
  await expect(group.getByRole("button", { name: "查看详情 自然光主视觉方向" })).toBeVisible();
  await expect(group).not.toContainText("work_");
  await expect(group).not.toContainText("必需");
  await expect(page.getByRole("heading", { name: "执行计划预览" })).toHaveCount(0);

  await group.getByRole("button", { name: "要求调整" }).click();
  await expect(page.getByLabel("发送给 Master")).toHaveValue("请调整计划 r1：");
  await page.getByLabel("发送给 Master").fill("请调整计划 r1：降低整体饱和度。");
  await page.getByRole("checkbox", { name: /brief\.md/ }).check();
  await page.locator(".master-composer button[type='submit']").click();
  await expect(page.getByText("消息已保存，Master 正在处理。")).toBeVisible();
  await expect(page.getByText("等待 Master 完成")).toBeVisible();
});

test("launches one card from the chat flow and streams its live start status", async ({ page }) => {
  await page.goto("/tasks/task_master_e2e/master");
  const group = page.getByRole("region", { name: "计划 r1 任务卡" });
  await group.getByRole("button", { name: "启动 自然光主视觉方向" }).click();

  await expect(page.getByText("计划已确认，该子任务实例正在启动；其余子任务可继续单独启动。")).toBeVisible();
  await expect(group.getByRole("status")).toContainText("准备运行环境");
  await expect(group.getByText("已就绪")).toBeVisible();
  await expect(group.getByRole("button", { name: "启动 自然光主视觉方向" })).toHaveCount(0);
  await expect(page.getByRole("progressbar")).toHaveCount(0);
});

test("locks the started card while its unstarted peer remains editable", async ({ page }) => {
  await page.goto("/tasks/task_master_e2e/master");
  const firstPlan = page.getByRole("region", { name: "计划 r1 任务卡" });
  await firstPlan.getByRole("button", { name: "启动 自然光主视觉方向" }).click();
  await expect(firstPlan.getByText("已就绪")).toBeVisible();

  await firstPlan.getByRole("button", { name: "查看详情 自然光主视觉方向" }).click();
  let detail = page.getByRole("dialog", { name: "任务卡详情" });
  await expect(detail.getByRole("button", { name: "编辑任务卡 自然光主视觉方向" })).toHaveCount(0);
  await detail.getByRole("button", { name: "关闭任务卡详情" }).click();

  await firstPlan.getByRole("button", { name: "查看详情 棚拍光主视觉方向" }).click();
  detail = page.getByRole("dialog", { name: "任务卡详情" });
  await detail.getByRole("button", { name: "编辑任务卡 棚拍光主视觉方向" }).click();
  const editor = page.getByRole("dialog", { name: "编辑任务卡" });
  await editor.getByLabel("目标").fill("生成克制、低饱和的棚拍光主视觉方向。");
  await editor.getByRole("button", { name: "保存为新修订" }).click();

  await expect(page.getByRole("status").filter({ hasText: "任务卡已保存；计划已更新为 r2" })).toBeVisible();
  const revisedPlan = page.getByRole("region", { name: "计划 r2 任务卡" });
  await revisedPlan.getByRole("button", { name: "查看详情 自然光主视觉方向" }).click();
  detail = page.getByRole("dialog", { name: "任务卡详情" });
  await expect(detail.getByRole("button", { name: "编辑任务卡 自然光主视觉方向" })).toHaveCount(0);
  await detail.getByRole("button", { name: "关闭任务卡详情" }).click();

  await revisedPlan.getByRole("button", { name: "查看详情 棚拍光主视觉方向" }).click();
  detail = page.getByRole("dialog", { name: "任务卡详情" });
  await expect(detail).toContainText("生成克制、低饱和的棚拍光主视觉方向。");
  await expect(detail.getByRole("button", { name: "编辑任务卡 棚拍光主视觉方向" })).toBeVisible();
});

test("shows concrete Chinese adapter validation details when a card launch fails", async ({ page }) => {
  await page.route("**/api/v1/tasks/task_master_e2e/plan-proposals/*/confirm", async (route) => {
    await route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({
        error: {
          code: "VALIDATION_ERROR",
          message: "The Agent adapter rejected its task card.",
          details: {
            errors: ["Image TaskCard 1.1 requires parameters.usage_context."],
          },
        },
      }),
    });
  });

  await page.goto("/tasks/task_master_e2e/master");
  await page.getByRole("button", { name: "启动 自然光主视觉方向" }).click();

  await expect(page.getByRole("alert")).toContainText(
    "任务卡未通过智能体校验：图片任务卡缺少使用场景。",
  );
});

test("auto mode still waits for an explicit per-card launch click", async ({ page }) => {
  let confirmationRequests = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/plan-proposals/1/confirm")) confirmationRequests += 1;
  });

  await page.goto("/tasks/task_master_e2e/master");
  const group = page.getByRole("region", { name: "计划 r1 任务卡" });
  await expect(group.getByRole("button", { name: "启动 自然光主视觉方向" })).toBeVisible();
  expect(confirmationRequests).toBe(0);

  await group.getByRole("button", { name: "启动 自然光主视觉方向" }).click();
  await expect.poll(() => confirmationRequests).toBe(1);
  await expect(group.getByText("已就绪")).toBeVisible();
});

test("edits a task card from the detail dialog and keeps every plan version in the flow", async ({ page }) => {
  await page.goto("/tasks/task_master_e2e/master");
  await page.getByRole("button", { name: "查看详情 自然光主视觉方向" }).click();
  const detail = page.getByRole("dialog", { name: "任务卡详情" });
  await expect(detail).toBeVisible();
  await expect(detail).toContainText("TaskCard · r1");
  await expect(detail).toContainText("发布会主屏");

  await detail.getByRole("button", { name: "编辑任务卡 自然光主视觉方向" }).click();
  const editor = page.getByRole("dialog", { name: "编辑任务卡" });
  await expect(editor).toBeVisible();
  await editor.getByLabel("目标").fill("生成低饱和、克制的自然光主视觉。");
  await editor.getByLabel("指令（每行一条）").fill("保持品牌安全区。\n降低整体饱和度。");
  await editor.getByLabel("候选数量").fill("2");
  await editor.getByRole("button", { name: "保存为新修订" }).click();

  await expect(page.getByRole("status").filter({ hasText: "任务卡已保存；计划已更新为 r2" })).toBeVisible();
  const oldGroup = page.getByRole("region", { name: "计划 r1 任务卡" });
  const newGroup = page.getByRole("region", { name: "计划 r2 任务卡" });
  await expect(oldGroup).toBeVisible();
  await expect(oldGroup).toContainText("已被替换");
  await expect(oldGroup.getByRole("button", { name: "启动 自然光主视觉方向" })).toHaveCount(0);
  await expect(newGroup).toBeVisible();
  await newGroup.getByRole("button", { name: "查看详情 自然光主视觉方向" }).click();
  await expect(page.getByRole("dialog", { name: "任务卡详情" })).toContainText("生成低饱和、克制的自然光主视觉。");
  await expect(page.getByRole("dialog", { name: "任务卡详情" })).toContainText("TaskCard · r2");
});

test("keeps the permanent thread usable at supported desktop widths", async ({ page }) => {
  for (const width of [1280, 1440, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/tasks/task_master_e2e/master");
    await expect(page.locator(".workbench-topbar")).toContainText("秋季发布会主视觉");
    await expect
      .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth))
      .toBe(true);
  }

  await page.getByRole("button", { name: "要求调整" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("发送给 Master")).toBeFocused();
});

test("keeps task card dialogs accessible and returns keyboard focus", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.goto("/tasks/task_master_e2e/master");

  const detailButton = page.getByRole("button", { name: "查看详情 自然光主视觉方向" });
  await detailButton.focus();
  await detailButton.click();
  await expect(page.getByRole("dialog", { name: "任务卡详情" })).toBeVisible();
  let audit = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(audit.violations, audit.violations.map((violation) => violation.id).join(", ")).toEqual([]);
  await page.getByRole("button", { name: "关闭任务卡详情" }).click();
  await expect(detailButton).toBeFocused();

  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await detailButton.click();
  await page.getByRole("dialog", { name: "任务卡详情" })
    .getByRole("button", { name: "编辑任务卡 自然光主视觉方向" })
    .click();
  await expect(page.getByRole("dialog", { name: "编辑任务卡" })).toBeVisible();
  audit = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(audit.violations, audit.violations.map((violation) => violation.id).join(", ")).toEqual([]);
  await page.getByRole("button", { name: "取消" }).click();
  await expect(detailButton).toBeFocused();
});
