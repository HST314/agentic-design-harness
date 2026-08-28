import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  let created = false;
  let startPolicy: "manual" | "auto" = "manual";
  let submitted = false;
  let intakeRevision = 1;
  let taskRevision = 1;
  let uploaded = false;
  let historyTitle = "品牌手册更新";
  let historyPinned: string | null = null;
  let historyArchived: string | null = null;
  let historyPresentationRevision = 1;

  const task = () => ({
    schema_version: "1.0",
    task_id: "task_e2e_intake",
    title: "秋季发布会三套主视觉方向",
    goal: "秋季发布会三套主视觉方向",
    master_owner: "master_default",
    start_policy: startPolicy,
    status: "DRAFT",
    created_at: "2026-08-22T10:00:00Z",
    updated_at: "2026-08-22T10:00:00Z",
    input_manifest: uploaded ? "inputs/manifests/selected_task_e2e_intake.json" : "inputs/manifests/intake-empty.json",
    plan_revision: 1,
  });
  const asset = {
    asset_id: "a_imp_e2e",
    filename: "brief.md",
    mime_type: "text/markdown",
    size_bytes: 22,
    sha256: "a".repeat(64),
    description: "发布会核心需求",
    created_at: "2026-08-22T10:01:00Z",
    integrity_status: "VERIFIED",
  };
  const response = () => ({
    schema_version: "1.0",
    intake: {
      schema_version: "1.0",
      task_id: "task_e2e_intake",
      prompt: "秋季发布会三套主视觉方向",
      upload_session: {
        session_id: "upload_e2e",
        status: submitted ? "LOCKED" : "OPEN",
        accepted_mime_types: ["image/jpeg", "image/png", "image/webp", "application/pdf", "text/plain", "text/markdown"],
        max_files: 20,
        max_total_bytes: 209715200,
      },
      asset_ids: uploaded ? [asset.asset_id] : [],
      status: submitted ? "SUBMITTED" : "DRAFT",
      start_policy: startPolicy,
      revision: intakeRevision,
      created_at: "2026-08-22T10:00:00Z",
      updated_at: "2026-08-22T10:01:00Z",
      submitted_at: submitted ? "2026-08-22T10:02:00Z" : null,
    },
    intake_revision: intakeRevision,
    task: task(),
    task_revision: taskRevision,
    navigation: {
      schema_version: "1.0",
      task_id: "task_e2e_intake",
      pinned_at: null,
      archived_at: null,
      display_order: 0,
      revision: 1,
      updated_at: "2026-08-22T10:00:00Z",
    },
    presentation_revision: 1,
    assets: uploaded ? [asset] : [],
  });
  const masterResponse = () => ({
    schema_version: "1.0",
    thread: {
      schema_version: "1.0",
      task_id: "task_e2e_intake",
      latest_sequence: 1,
      latest_proposal_revision: 0,
      active_run: {
        run_id: "run_e2e_intake",
        message_id: "message_e2e_intake",
        status: "RUNNING",
        started_at: "2026-08-22T10:02:00Z",
        updated_at: "2026-08-22T10:02:00Z",
      },
      last_error: null,
      revision: 3,
      created_at: "2026-08-22T10:00:00Z",
      updated_at: "2026-08-22T10:02:00Z",
    },
    thread_revision: 3,
    messages: [{
      schema_version: "1.0",
      message_id: "message_e2e_intake",
      task_id: "task_e2e_intake",
      sequence: 1,
      role: "user",
      kind: "text",
      content: "秋季发布会三套主视觉方向",
      asset_refs: uploaded ? [{ asset_id: asset.asset_id, manifest_relpath: `inputs/manifests/${asset.asset_id}.json` }] : [],
      created_at: "2026-08-22T10:02:00Z",
    }],
    latest_proposal: null,
    proposals: [],
    task: task(),
    task_revision: taskRevision,
    gateway_available: true,
    assets: uploaded ? [{ asset_id: asset.asset_id, filename: asset.filename, description: asset.description, manifest_relpath: `inputs/manifests/${asset.asset_id}.json` }] : [],
  });

  await page.route("**/readyz", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ready" }) });
  });
  await page.route("**/api/v1/tasks", async (route) => {
    const items = [{
      task_id: "task_history",
      status: "SUCCEEDED",
      title: historyTitle,
      updated_at: "2026-08-21T08:00:00Z",
      revision: 2,
      pinned_at: historyPinned,
      archived_at: historyArchived,
      presentation_revision: historyPresentationRevision,
    }];
    if (created) items.unshift({
      task_id: "task_e2e_intake",
      status: "DRAFT",
      title: task().title,
      updated_at: "2026-08-22T10:01:00Z",
      revision: taskRevision,
      pinned_at: null,
      archived_at: null,
      presentation_revision: 1,
    });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", items }) });
  });
  await page.route("**/api/v1/task-intakes", async (route) => {
    const body = route.request().postDataJSON() as { start_policy?: "manual" | "auto" };
    startPolicy = body.start_policy ?? "manual";
    created = true;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(response()) });
  });
  await page.route("**/api/v1/task-intakes/task_e2e_intake/assets", async (route) => {
    expect(route.request().headers()["content-type"]).toContain("multipart/form-data; boundary=");
    expect(route.request().postDataBuffer()?.toString()).toContain("brief.md");
    uploaded = true;
    intakeRevision += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        intake: response().intake,
        intake_revision: intakeRevision,
        asset,
      }),
    });
  });
  await page.route("**/api/v1/task-intakes/task_e2e_intake/submit", async (route) => {
    submitted = true;
    intakeRevision += 1;
    taskRevision += 1;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(response()) });
  });
  await page.route("**/api/v1/task-intakes/task_e2e_intake", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(response()) });
  });
  await page.route("**/api/v1/tasks/task_e2e_intake/master/messages", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(masterResponse()) });
  });
  await page.route("**/api/v1/task-intakes/task_history", async (route) => {
    await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: { message: "No intake" } }) });
  });
  await page.route("**/api/v1/tasks/task_history/presentation", async (route) => {
    const body = route.request().postDataJSON() as { title?: string; pinned?: boolean; archived?: boolean };
    if (body.title !== undefined) historyTitle = body.title;
    if (body.pinned !== undefined) historyPinned = body.pinned ? "2026-08-22T10:03:00Z" : null;
    if (body.archived !== undefined) {
      historyArchived = body.archived ? "2026-08-22T10:04:00Z" : null;
      if (body.archived) historyPinned = null;
    }
    historyPresentationRevision += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        task: { ...task(), task_id: "task_history", title: historyTitle, status: "SUCCEEDED" },
        task_revision: 3,
        navigation: {
          schema_version: "1.0",
          task_id: "task_history",
          pinned_at: historyPinned,
          archived_at: historyArchived,
          display_order: 0,
          revision: historyPresentationRevision,
          updated_at: "2026-08-22T10:04:00Z",
        },
        presentation_revision: historyPresentationRevision,
      }),
    });
  });
});

test("creates a task from the chat-style page and lands in the Master conversation", async ({ page }) => {
  await page.goto("/tasks/new");
  const send = page.getByRole("button", { name: "发送并创建任务" });
  await expect(send).toBeDisabled();
  await page.getByLabel("发送给 Master 的首条消息").fill("秋季发布会三套主视觉方向");
  await page.locator('input[type="file"]').setInputFiles({
    name: "brief.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# 秋季发布会需求\n"),
  });
  await expect(page.getByText("brief.md", { exact: true })).toBeVisible();
  await send.click();

  await expect(page).toHaveURL(/\/tasks\/task_e2e_intake\/master$/);
  await expect(page.getByRole("log", { name: "Master 消息记录" })).toContainText("秋季发布会三套主视觉方向");
  await expect(page.getByText("Master 正在分析与思考")).toBeVisible();
  await expect(page.getByText("引用已有资源（创建提交后不可追加上传）")).toBeVisible();
  await expect(page.locator('input[type="file"]')).toHaveCount(0);
  await expect(page.getByRole("link", { name: /秋季发布会三套主视觉方向/ })).toBeVisible();
});

test("keeps the create thread flush with the shell and the Master composer at the bottom", async ({ page }) => {
  await page.goto("/tasks/new");

  const expand = page.getByRole("button", { name: "展开消息输入框" });
  await expect(expand).toBeVisible();
  await expect(expand).toHaveText("");
  const textarea = page.getByLabel("发送给 Master 的首条消息");
  const normalHeight = await textarea.evaluate((element) => element.getBoundingClientRect().height);
  await expand.click();
  await expect(page.getByRole("button", { name: "收起消息输入框" })).toHaveAttribute("aria-expanded", "true");
  const expandedHeight = await textarea.evaluate((element) => element.getBoundingClientRect().height);
  expect(expandedHeight).toBeGreaterThan(normalHeight * 2);
  await page.getByRole("button", { name: "收起消息输入框" }).click();
  await expect(textarea).toHaveJSProperty("rows", 4);

  const layout = await page.evaluate(() => {
    const rect = (selector: string): DOMRect => {
      const element = document.querySelector(selector);
      if (!(element instanceof HTMLElement)) throw new Error(`Missing ${selector}`);
      return element.getBoundingClientRect();
    };
    const sidebar = rect(".workbench-sidebar");
    const topbar = rect(".workbench-topbar");
    const thread = rect(".intake-chat__thread");
    const composer = rect(".intake-chat__composer");
    return {
      topGap: Math.round(thread.top - topbar.bottom),
      sideGap: Math.round(thread.left - sidebar.right),
      threadComposerGap: Math.round(composer.top - thread.bottom),
      bottomGap: Math.round(window.innerHeight - composer.bottom),
      widthDelta: Math.round(Math.abs(thread.width - composer.width)),
      threadHeight: Math.round(thread.height),
      composerHeight: Math.round(composer.height),
    };
  });

  expect(layout.topGap).toBeGreaterThanOrEqual(0);
  expect(layout.topGap).toBeLessThanOrEqual(3);
  expect(layout.sideGap).toBeGreaterThanOrEqual(0);
  expect(layout.sideGap).toBeLessThanOrEqual(3);
  expect(layout.threadComposerGap).toBeGreaterThanOrEqual(0);
  expect(layout.threadComposerGap).toBeLessThanOrEqual(3);
  expect(layout.bottomGap).toBeGreaterThanOrEqual(12);
  expect(layout.bottomGap).toBeLessThanOrEqual(20);
  expect(layout.widthDelta).toBeLessThanOrEqual(1);
  expect(layout.threadHeight).toBeGreaterThan(layout.composerHeight);

  await textarea.fill("秋季发布会三套主视觉方向");
  await page.getByRole("button", { name: "发送并创建任务" }).click();
  await expect(page).toHaveURL(/\/tasks\/task_e2e_intake\/master$/);
  await expect(page.locator(".master-composer")).toBeVisible();
  const continuedLayout = await page.evaluate(() => {
    const composer = document.querySelector(".master-composer");
    const messageInput = document.querySelector("#master-message");
    if (!(composer instanceof HTMLElement) || !(messageInput instanceof HTMLTextAreaElement)) {
      throw new Error("Missing continued Master composer");
    }
    return {
      bottomGap: Math.round(window.innerHeight - composer.getBoundingClientRect().bottom),
      textareaHeight: Math.round(messageInput.getBoundingClientRect().height),
    };
  });
  expect(continuedLayout.bottomGap).toBeGreaterThanOrEqual(12);
  expect(continuedLayout.bottomGap).toBeLessThanOrEqual(20);
  expect(Math.abs(continuedLayout.textareaHeight - normalHeight)).toBeLessThanOrEqual(2);
});

test("keeps the launch-mode choice out of the conversational create flow", async ({ page }) => {
  await page.goto("/tasks/new");
  await expect(page.getByText("启动方式")).toHaveCount(0);
  await expect(page.getByRole("radio")).toHaveCount(0);
  await page.getByLabel("发送给 Master 的首条消息").fill("秋季发布会三套主视觉方向");
  await page.getByRole("button", { name: "发送并创建任务" }).click();

  await expect(page).toHaveURL(/\/tasks\/task_e2e_intake\/master$/);
  await expect(page.getByRole("log", { name: "Master 消息记录" })).toContainText("秋季发布会三套主视觉方向");
});

test("recovers a server-side draft and submits it manually", async ({ page }) => {
  await page.goto("/tasks/task_e2e_intake/master");
  await expect(page.getByText("服务端草稿已恢复")).toBeVisible();
  await expect(page.getByText("尚未添加附件；Prompt 可单独提交。")).toBeVisible();
  await page.getByRole("button", { name: "提交任务材料" }).click();
  await expect(page.getByText("Master 正在分析与思考")).toBeVisible();

  await page.reload();
  await expect(page.getByRole("log", { name: "Master 消息记录" })).toContainText("秋季发布会三套主视觉方向");
  await expect(page.getByRole("button", { name: "提交任务材料" })).toHaveCount(0);
});

test("validates files locally and manages rename, pin and archive presentation state", async ({ page }) => {
  await page.goto("/tasks/new");
  await page.locator('input[type="file"]').setInputFiles({
    name: "slides.pptx",
    mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    buffer: Buffer.from("not accepted"),
  });
  await expect(page.getByRole("alert")).toContainText("仅支持图片、PDF、TXT 和 MD");

  await page.getByLabel("打开 品牌手册更新 的任务操作").click();
  await page.getByRole("menuitem", { name: "重命名" }).click();
  await page.getByLabel("任务标题").fill("品牌规范 2026");
  await page.getByRole("button", { name: "保存标题" }).click();
  await expect(page.getByRole("link", { name: /品牌规范 2026/ })).toBeVisible();

  await page.getByLabel("打开 品牌规范 2026 的任务操作").click();
  await page.getByRole("menuitem", { name: "置顶" }).click();
  await expect(page.getByRole("region", { name: "置顶" })).toContainText("品牌规范 2026");

  await page.getByLabel("打开 品牌规范 2026 的任务操作").click();
  await page.getByRole("menuitem", { name: "归档" }).click();
  await expect(page.getByRole("link", { name: /品牌规范 2026/ })).toHaveCount(0);
  await page.getByRole("searchbox", { name: "搜索主任务" }).fill("品牌规范");
  await expect(page.getByRole("region", { name: "归档搜索结果" })).toContainText("品牌规范 2026");
  await page.getByLabel("打开 品牌规范 2026 的任务操作").click();
  await page.getByRole("menuitem", { name: "恢复" }).click();
  await expect(page.getByRole("link", { name: /品牌规范 2026/ })).toBeVisible();
});
