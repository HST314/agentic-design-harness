import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }, testInfo) => {
  const startPolicy: "manual" | "auto" = testInfo.title.includes("auto mode") ? "auto" : "manual";
  let threadRevision = 4;
  let confirmed = false;
  let busy = false;
  let proposalRevision = 1;
  let cardRevision = 1;
  let cardObjective = "生成自然光主视觉方向。";
  let cardInstructions = ["保持品牌安全区。"];
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
  const proposal = () => ({
    schema_version: "1.0",
    proposal_id: `proposal_master_e2e_${proposalRevision}`,
    task_id: "task_master_e2e",
    revision: proposalRevision,
    status: confirmed ? "CONFIRMED" : "PENDING_CONFIRMATION",
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
    ],
    execution_cards: [
      {
        schema_version: "1.1",
        card_id: "card_direction_a",
        revision: cardRevision,
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
    ],
    created_at: "2026-08-22T10:01:00Z",
    updated_at: "2026-08-22T10:02:00Z",
    confirmed_at: confirmed ? "2026-08-22T10:02:00Z" : null,
  });
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
    threadRevision += 1;
    messages.push({ schema_version: "1.0", message_id: "message_card_revision", task_id: "task_master_e2e", sequence: messages.length + 1, role: "system", kind: "plan_proposal", content: `任务卡 card_direction_a 已保存为 r${cardRevision}，计划已更新为 r${proposalRevision}，请重新审阅后确认。`, asset_refs: [], created_at: "2026-08-22T10:02:30Z" });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(session()) });
  });
  await page.route("**/api/v1/tasks/task_master_e2e/plan-proposals/*/confirm", async (route) => {
    const body = route.request().postDataJSON() as { task_expected_revision: number; expected_card_revisions: Record<string, number>; envelope: { expected_revision: number } };
    expect(body.task_expected_revision).toBe(2);
    expect(body.expected_card_revisions).toEqual({ card_direction_a: cardRevision });
    expect(body.envelope.expected_revision).toBe(proposalRevision);
    confirmed = true;
    messages.push({ schema_version: "1.0", message_id: "message_confirm", task_id: "task_master_e2e", sequence: 3, role: "system", kind: "plan_confirmation", content: "计划 r1 已确认。", asset_refs: [], created_at: "2026-08-22T10:02:00Z" });
    threadRevision += 1;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", proposal: proposal(), plan_result: {}, start_result: { launches: [{ instance_id: "instance_direction_a" }] }, session: session() }) });
  });
});

test("reviews a durable plan and sends revision feedback with existing resources", async ({ page }) => {
  await page.goto("/tasks/task_master_e2e/master");
  await expect(page.getByRole("heading", { name: "秋季发布会主视觉" })).toBeVisible();
  await expect(page.getByRole("log", { name: "Master 消息记录" })).toContainText("已生成两条视觉探索路径");
  await expect(page.getByRole("heading", { name: "执行计划预览" })).toBeVisible();
  await expect(page.getByLabel("计划子任务").getByRole("heading", { name: "自然光主视觉方向" })).toBeVisible();

  await page.getByRole("button", { name: "要求调整" }).click();
  await expect(page.getByLabel("发送给 Master")).toHaveValue("请调整计划 r1：");
  await page.getByLabel("发送给 Master").fill("请调整计划 r1：降低整体饱和度。");
  await page.getByRole("checkbox", { name: /brief\.md/ }).check();
  await page.locator(".master-composer button[type='submit']").click();
  await expect(page.getByText("消息已保存，Master 正在处理。")).toBeVisible();
  await expect(page.getByText("等待 Master 完成")).toBeVisible();
});

test("requires an explicit final confirmation before starting the reviewed revision", async ({ page }) => {
  await page.goto("/tasks/task_master_e2e/master");
  await page.getByRole("button", { name: "确认并运行" }).click();
  const dialog = page.getByRole("dialog", { name: "启动计划 r1" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("启动满足业务门禁的实例");
  await dialog.getByRole("button", { name: "确认并启动" }).click();
  await expect(page.getByText("计划已确认，实例启动结果已记录。")).toBeVisible();
  await expect(page.getByText("已确认", { exact: true })).toBeVisible();
});

test("auto mode keeps human review and starts only after the final revision and cost confirmation", async ({ page }) => {
  let confirmationRequests = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/plan-proposals/1/confirm")) confirmationRequests += 1;
  });

  await page.goto("/tasks/task_master_e2e/master");
  await expect(page.getByText("自动规划 · 人工启动")).toBeVisible();
  await expect(page.getByRole("button", { name: "编辑任务卡 自然光主视觉方向" })).toBeVisible();
  await page.getByRole("button", { name: "要求调整" }).click();
  await expect(page.getByLabel("发送给 Master")).toHaveValue("请调整计划 r1：");
  expect(confirmationRequests).toBe(0);

  await page.getByRole("button", { name: "确认并运行" }).click();
  const dialog = page.getByRole("dialog", { name: "启动计划 r1" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("主任务修订");
  await expect(dialog).toContainText("计划修订");
  await expect(dialog.locator("dl").getByText("r2", { exact: true })).toBeVisible();
  await expect(dialog.locator("dl").getByText("r1", { exact: true })).toBeVisible();
  await expect(dialog).toContainText("实例 instance_direction_a");
  await expect(dialog).toContainText("可能产生创作服务费用");
  expect(confirmationRequests).toBe(0);

  await dialog.getByRole("button", { name: "确认并启动" }).click();
  await expect.poll(() => confirmationRequests).toBe(1);
  await expect(page.getByText("计划已确认，实例启动结果已记录。")).toBeVisible();
  await expect(page.getByText("已确认", { exact: true })).toBeVisible();
});

test("edits a Master TaskCard into a new plan revision before confirmation", async ({ page }) => {
  await page.goto("/tasks/task_master_e2e/master");
  await expect(page.getByRole("heading", { name: "任务卡" })).toBeVisible();
  await expect(page.getByText("TaskCard · r1")).toBeVisible();
  await expect(page.getByText("发布会主屏")).toBeVisible();

  await page.getByRole("button", { name: "编辑任务卡 自然光主视觉方向" }).click();
  const editor = page.getByRole("dialog", { name: "编辑任务卡" });
  await expect(editor).toBeVisible();
  await editor.getByLabel("目标").fill("生成低饱和、克制的自然光主视觉。");
  await editor.getByLabel("指令（每行一条）").fill("保持品牌安全区。\n降低整体饱和度。");
  await editor.getByLabel("候选数量").fill("2");
  await editor.getByRole("button", { name: "保存为新修订" }).click();

  await expect(page.getByRole("status").filter({ hasText: "任务卡已保存；计划已更新为 r2" })).toBeVisible();
  await expect(page.getByText("TaskCard · r2")).toBeVisible();
  await expect(page.getByText("生成低饱和、克制的自然光主视觉。")).toBeVisible();
  await page.getByRole("button", { name: "确认并运行" }).click();
  const confirm = page.getByRole("dialog", { name: "启动计划 r2" });
  await expect(confirm).toContainText("card_direction_a");
  await expect(confirm).toContainText("r2");
});

test("keeps the permanent thread usable at supported desktop widths", async ({ page }) => {
  for (const width of [1280, 1440, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/tasks/task_master_e2e/master");
    await expect(page.getByRole("heading", { name: "秋季发布会主视觉" })).toBeVisible();
    await expect
      .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth))
      .toBe(true);
  }

  await page.getByRole("button", { name: "要求调整" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("发送给 Master")).toBeFocused();
});

test("keeps TaskCard review dialogs accessible and returns keyboard focus", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.goto("/tasks/task_master_e2e/master");

  const edit = page.getByRole("button", { name: "编辑任务卡 自然光主视觉方向" });
  await edit.focus();
  await edit.click();
  await expect(page.getByRole("dialog", { name: "编辑任务卡" })).toBeVisible();
  let audit = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(audit.violations, audit.violations.map((violation) => violation.id).join(", ")).toEqual([]);
  await page.getByRole("button", { name: "取消" }).click();
  await expect(edit).toBeFocused();

  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  const confirm = page.getByRole("button", { name: "确认并运行" });
  await confirm.focus();
  await confirm.click();
  await expect(page.getByRole("dialog", { name: "启动计划 r1" })).toBeVisible();
  audit = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(audit.violations, audit.violations.map((violation) => violation.id).join(", ")).toEqual([]);
  await page.getByRole("button", { name: "返回审阅" }).click();
  await expect(confirm).toBeFocused();
});
