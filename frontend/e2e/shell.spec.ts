import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  let approvalResolved = false;
  await page.route("**/readyz", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ready" }),
    });
  });
  await page.route("**/api/v1/tasks", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        items: [
          {
            task_id: "t_ui",
            status: "RUNNING",
            title: "秋季发布主视觉",
            updated_at: "2026-08-20T12:00:00Z",
            revision: 2,
          },
        ],
      }),
    });
  });
  await page.route("**/api/v1/tasks/t_ui", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        task_revision: 2,
        task: {
          task_id: "t_ui",
          status: "RUNNING",
          title: "秋季发布主视觉",
          goal: "为内部评审制作一张发布主视觉。",
          start_policy: "manual",
          master_owner: "master_default",
          updated_at: "2026-08-20T12:00:00Z",
          revision: 2,
        },
        plan: {
          stages: [
            {
              stage_id: "s_image",
              type: "image",
              position: 1,
              status: "RUNNING",
              required: true,
              instance_ids: ["i_ui"],
            },
          ],
          instances: [
            {
              instance_id: "i_ui",
              task_id: "t_ui",
              agent_type: "image",
              status: "RUNNING",
              required: true,
              approval_mode: "human",
              config_revision: 1,
              ui_url: "http://127.0.0.1:18123/",
              process: {
                pid: 1234,
                port: 18123,
                state: "RUNNING",
                started_at: "2026-08-20T12:00:00Z",
              },
            },
          ],
        },
      }),
    });
  });
  await page.route("**/api/v1/tasks/t_ui/events?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        items: [
          {
            event_id: "evt_ui",
            event_type: "OBJECT_COMMITTED",
            object_type: "plan",
            object_id: "t_ui",
            revision: 2,
            actor: { actor_type: "master", actor_id: "master_default" },
            command: "save_plan",
            result: "COMMITTED",
            occurred_at: "2026-08-20T12:00:00Z",
          },
        ],
      }),
    });
  });
  await page.route("**/api/v1/tasks/t_ui/approvals?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        items: [
          {
            approval_id: "ap_ui",
            task_id: "t_ui",
            instance_id: "i_ui",
            step_id: "approve_taskbook",
            kind: "WORKFLOW",
            owner: "human",
            status: approvalResolved ? "APPROVED" : "PENDING",
            payload_ref: "approvals/ap_ui/request.json",
            created_at: "2026-08-20T12:01:00Z",
            sequence: 1,
            revision: approvalResolved ? 2 : 1,
          },
        ],
      }),
    });
  });
  await page.route("**/api/v1/tasks/t_ui/files?group=all", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        items: [
          {
            relative_path: "resources/shared/a_final/final.png",
            filename: "final.png",
            mime_type: "image/png",
            size_bytes: 2048,
            sha256: "a".repeat(64),
            previewable: true,
          },
          {
            relative_path: "inputs/brief.md",
            filename: "brief.md",
            mime_type: "text/markdown",
            size_bytes: 96,
            sha256: "b".repeat(64),
            previewable: true,
          },
        ],
        assets: [
          {
            integrity_status: "VERIFIED",
            manifest: {
              asset_id: "a_final",
              producer_instance_id: "i_ui",
              role: "final_image",
              relative_path: "resources/shared/a_final/final.png",
              description: "评审通过的最终主视觉",
              created_at: "2026-08-20T12:05:00Z",
            },
          },
        ],
      }),
    });
  });
  await page.route("**/api/v1/tasks/t_ui/files/preview?path=*", async (route) => {
    await route.fulfill({ status: 200, contentType: "image/png", body: "preview" });
  });
  const usage = {
    schema_version: "1.0",
    task_id: "t_ui",
    instance_id: null,
    completeness: "COMPLETE",
    event_count: 1,
    tokens: {
      input_tokens: 1000,
      output_tokens: 250,
      cached_input_tokens: 100,
      reasoning_tokens: 50,
      total_tokens: 1250,
    },
    cost: {
      completeness: "COMPLETE",
      known_micros: 3000,
      priced_event_count: 1,
      unpriced_event_count: 0,
      price_catalog_revisions: ["price_v1"],
    },
    instances: [
      {
        instance_id: "i_ui",
        agent_type: "image",
        completeness: "COMPLETE",
        event_count: 1,
        tokens: {
          input_tokens: 1000,
          output_tokens: 250,
          cached_input_tokens: 100,
          reasoning_tokens: 50,
          total_tokens: 1250,
        },
        cost: {
          completeness: "COMPLETE",
          known_micros: 3000,
          priced_event_count: 1,
          unpriced_event_count: 0,
          price_catalog_revisions: ["price_v1"],
        },
        last_checked_at: "2026-08-20T12:06:00Z",
      },
    ],
    models: [
      {
        model: "image-model",
        event_count: 1,
        tokens: {
          input_tokens: 1000,
          output_tokens: 250,
          cached_input_tokens: 100,
          reasoning_tokens: 50,
          total_tokens: 1250,
        },
        cost: {
          completeness: "COMPLETE",
          known_micros: 3000,
          priced_event_count: 1,
          unpriced_event_count: 0,
          price_catalog_revisions: ["price_v1"],
        },
      },
    ],
    time_buckets: [],
    events: [
      {
        event_id: "usage_ui",
        instance_id: "i_ui",
        request_id: "request_ui",
        model: "image-model",
        total_tokens: 1250,
        occurred_at: "2026-08-20T12:06:00Z",
      },
    ],
  };
  await page.route("**/api/v1/tasks/t_ui/usage", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(usage),
    });
  });
  await page.route("**/api/v1/instances/i_ui/usage", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...usage, instance_id: "i_ui" }),
    });
  });
  await page.route("**/api/v1/tasks/t_ui/retry-budget", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        budget: {
          revision: 2,
          retry_policy: {
            max_auto_retries_per_retry_group: 2,
            max_auto_retry_tokens_task: 5000,
            retry_token_reservation_by_agent: { image: 1000 },
            max_auto_retry_cost_micros: null,
            price_catalog_revision: null,
          },
          retry_budget_ledger: {
            retry_tokens_reserved: 1000,
            retry_tokens_settled: 1250,
            retry_cost_micros_reserved: 0,
            retry_cost_micros_settled: 3000,
            frozen: false,
            frozen_reason: null,
          },
          attempts: [{ attempt_id: "attempt_ui", status: "RESERVED" }],
        },
      }),
    });
  });
  await page.route("**/api/v1/instances/i_ui", async (route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "1.0", instance: { approval_mode: "master" } }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        task_id: "t_ui",
        task_revision: 2,
        pending_approval: approvalResolved
          ? null
          : {
              approval_id: "ap_ui",
              task_id: "t_ui",
              instance_id: "i_ui",
              step_id: "approve_taskbook",
              kind: "WORKFLOW",
              owner: "human",
              status: "PENDING",
              payload_ref: "approvals/ap_ui/request.json",
              created_at: "2026-08-20T12:01:00Z",
              sequence: 1,
              revision: 1,
            },
        instance: {
          instance_id: "i_ui",
          task_id: "t_ui",
          agent_type: "image",
          status: "WAITING_APPROVAL",
          required: true,
          approval_mode: "human",
          config_revision: 1,
          ui_url: "http://127.0.0.1:18123/",
          process: {
            pid: 1234,
            port: 18123,
            state: "RUNNING",
            started_at: "2026-08-20T12:00:00Z",
          },
        },
        observation: {
          status: "WAITING_APPROVAL",
          step_id: "waiting_clarification",
          capabilities: ["answer_clarification"],
          details: {
            job_status: "succeeded",
            timeline_cursor: 7,
            compatibility_error: "runtime.yaml endpoint does not match the Adapter version",
          },
        },
      }),
    });
  });
  const workItem = {
    schema_version: "1.0",
    work_item_id: "work_ui",
    task_id: "t_ui",
    title: "主视觉设计",
    agent_type: "image",
    required: true,
    depends_on: [],
    stage: { stage_id: "stage_ui", position: 1, type: "image", status: "RUNNING", depends_on: [], available: true },
    business_status: "WAITING_APPROVAL",
    raw_status: "WAITING_APPROVAL",
    current_instance: { instance_id: "i_ui", status: "WAITING_APPROVAL", approval_mode: "human", manual_finished: false, process_state: "RUNNING", restart_required: false, created_at: "2026-08-20T12:00:00Z" },
    instance_ids: ["i_ui"],
    attempts: [],
    pending_approvals: [],
    delivery_count: 0,
    alerts: [],
    updated_at: "2026-08-20T12:01:00Z",
  };
  await page.route("**/api/v1/tasks/t_ui/work-items", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ schema_version: "1.0", task_id: "t_ui", task_revision: 2, projection_revision: "ui", refresh_after_ms: 5000, stages: [workItem.stage], summary: { TODO: 0, RUNNING: 0, WAITING_APPROVAL: 1, COMPLETED: 0, EXCEPTION: 0 }, items: [workItem] }),
    });
  });
  await page.route("**/api/v1/tasks/t_ui/work-items/work_ui", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ schema_version: "1.0", task_id: "t_ui", task_revision: 2, projection_revision: "ui", refresh_after_ms: 5000, item: workItem }),
    });
  });
  await page.route("**/api/v1/instances/i_ui/ui-link?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ schema_version: "1.0", task_id: "t_ui", work_item_id: "work_ui", instance_id: "i_ui", agent_type: "image", instance_status: "WAITING_APPROVAL", ui_url: null, link_status: "NO_UI_URL", embeddable: false, frame_policy: "NOT_CHECKED", diagnostic: "Preparing." }),
    });
  });
  await page.route("**/api/v1/inbox?owner=human", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        items: [
          {
            inbox_id: "in_ui",
            task_id: "t_ui",
            instance_id: "i_ui",
            approval_id: "ap_ui",
            kind: "APPROVAL_REQUIRED",
            owner: "human",
            status: approvalResolved ? "HANDLED" : "UNREAD",
            title: "工作流等待决议",
            message: "任务书等待批准。",
            deep_link: "inbox?approval_id=ap_ui",
            created_at: "2026-08-20T12:01:00Z",
            sequence: 1,
            revision: approvalResolved ? 2 : 1,
            store_revision: approvalResolved ? 2 : 1,
          },
        ],
      }),
    });
  });
  await page.route("**/api/v1/approvals/ap_ui", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        approval: {
          approval_id: "ap_ui",
          task_id: "t_ui",
          instance_id: "i_ui",
          step_id: "approve_taskbook",
          kind: "WORKFLOW",
          owner: "human",
          status: approvalResolved ? "APPROVED" : "PENDING",
          payload_ref: "approvals/ap_ui/request.json",
          created_at: "2026-08-20T12:01:00Z",
          sequence: 1,
          revision: approvalResolved ? 2 : 1,
        },
        approval_revision: approvalResolved ? 2 : 1,
        payload: { available_actions: ["approve_taskbook"], context: { phase: "taskbook_review" } },
      }),
    });
  });
  await page.route("**/api/v1/approvals/ap_ui/resolve", async (route) => {
    approvalResolved = true;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ schema_version: "1.0", status: "RUNNING" }),
    });
  });
  await page.route("**/api/v1/inbox/in_ui/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ schema_version: "1.0", item: { status: "READ" } }),
    });
  });
});

test("production shell loads its favicon without browser errors", async ({ page }) => {
  const failedRequests: string[] = [];
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const httpErrorResponses: string[] = [];
  page.on("requestfailed", (request) => failedRequests.push(request.url()));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) httpErrorResponses.push(response.url());
  });

  await page.goto("/tasks");
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
  await expect(page.getByRole("heading", { name: "主任务", exact: true })).toBeVisible();
  expect({
    failed_requests: failedRequests.length,
    console_errors: consoleErrors.length,
    page_errors: pageErrors.length,
    http_error_responses: httpErrorResponses.length,
  }).toEqual({
    failed_requests: 0,
    console_errors: 0,
    page_errors: 0,
    http_error_responses: 0,
  });
});

test("shell navigation is keyboard reachable and deep-linkable", async ({ page }) => {
  await page.goto("/tasks");
  await expect(page.getByRole("heading", { name: "主任务", exact: true })).toBeVisible();
  const inboxLink = page.getByRole("link", { name: "收件箱" });
  await inboxLink.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByRole("heading", { name: "收件箱", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "收件箱", exact: true })).toBeVisible();
});

test("shell has no horizontal overflow on phone and landscape", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  for (const viewport of [
    { width: 375, height: 667 },
    { width: 667, height: 375 },
  ]) {
    await page.setViewportSize(viewport);
    await page.goto("/tasks");
    const dimensions = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(dimensions.scrollWidth).toBe(dimensions.clientWidth);
    await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
    await page.goto("/tasks/new");
    const intakeDimensions = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(intakeDimensions.scrollWidth).toBe(intakeDimensions.clientWidth);
    await expect(page.getByRole("heading", { name: "创建新的设计任务" })).toBeVisible();
  }
});

test("legacy instance links open the corresponding professional workbench", async ({ page }) => {
  await page.goto("/tasks");
  await page.getByRole("link", { name: "查看任务" }).click();
  await expect(page).toHaveURL(/\/tasks\/t_ui$/);
  await expect(page.getByRole("heading", { name: "阶段与实例" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "最近编排记录" })).toBeVisible();
  await page.getByRole("link", { name: "资源", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/t_ui\/resources$/);
  await expect(page.getByRole("heading", { name: "任务文件" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "公共交付" })).toBeVisible();
  await page.getByRole("link", { name: "Token", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/t_ui\/usage$/);
  await expect(page.getByRole("heading", { name: "用量观测" })).toBeVisible();
  await expect(page.getByText("实例占比", { exact: true })).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "i_ui Token 占比" })).toHaveAttribute("aria-valuenow", "100");
  await expect(page.getByText("1,250", { exact: true }).first()).toBeVisible();
  await page.getByRole("link", { name: "资源", exact: true }).click();
  await expect(page.getByRole("heading", { name: "final.png" })).toBeVisible();
  await page.getByRole("link", { name: "概览", exact: true }).click();
  await page.getByRole("link", { name: /Image Agent/ }).click();
  await expect(page).toHaveURL(/\/tasks\/t_ui\/work-items\/work_ui$/);
  await expect(page.locator("body")).not.toContainText(/PID|端口|Timeline 游标|runtime\.yaml|endpoint|Adapter|配置修订|凭据|instance_|work_/);
});

test("inbox notifications link straight to the corresponding professional workbench", async ({ page }) => {
  await page.goto("/inbox");
  const workbenchLink = page.getByRole("link", { name: "打开专业工作台" });
  await workbenchLink.click();
  await expect(page).toHaveURL(/\/tasks\/t_ui\/work-items\/work_ui$/);
  await expect(page.locator("body")).not.toContainText(/i_ui|t_ui|work_ui|队列序号|JSON/);
});

test("resources provide an explicit safe preview and a browser download", async ({ page, context }) => {
  const previewedPaths: string[] = [];
  const downloadedPaths: string[] = [];
  await page.route("**/api/v1/tasks/t_ui/files/preview?path=*", async (route) => {
    const path = new URL(route.request().url()).searchParams.get("path") ?? "";
    previewedPaths.push(path);
    await route.fulfill({
      status: 200,
      contentType: "text/markdown; charset=utf-8",
      body: "# 已校验任务书\n\n仅显示受控工作区中的已提交内容。",
    });
  });
  await context.route("**/api/v1/tasks/t_ui/files/download**", async (route) => {
    const path = new URL(route.request().url()).searchParams.get("path") ?? "";
    downloadedPaths.push(path);
    await route.fulfill({
      status: 200,
      contentType: "text/markdown; charset=utf-8",
      headers: { "Content-Disposition": 'attachment; filename="brief.md"' },
      body: "# 已校验任务书",
    });
  });

  await page.goto("/tasks/t_ui/resources");
  const briefCard = page.locator("article.resource-card").filter({ hasText: "brief.md" });
  await briefCard.getByRole("button", { name: "安全预览" }).click();
  await expect(briefCard.getByText("仅显示受控工作区中的已提交内容。")).toBeVisible();
  await expect(briefCard.locator("pre.resource-text-preview")).toBeFocused();
  await expect(briefCard.getByRole("button", { name: "已安全预览" })).toBeDisabled();
  expect(previewedPaths).toContain("inputs/brief.md");

  const downloadPromise = page.waitForEvent("download");
  const downloadLink = briefCard.getByRole("link", { name: "下载文件" });
  await expect(downloadLink).toHaveAttribute("download", "");
  // Chromium bypasses request routing for download-attribute navigations. After
  // asserting that UI contract, let the mocked Content-Disposition drive the download.
  await downloadLink.evaluate((link) => link.removeAttribute("download"));
  await downloadLink.click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("brief.md");
  expect(await download.failure()).toBeNull();
  expect(downloadedPaths).toEqual(["inputs/brief.md"]);
});

test("task detail exposes approvals, Token and read-only events as deep links", async ({ page }) => {
  await page.goto("/tasks/t_ui/approvals");
  await expect(page.getByRole("heading", { name: "审批记录" })).toBeVisible();
  await expect(page.getByText("批准任务书")).toBeVisible();
  await page.getByRole("link", { name: "事件", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/t_ui\/events$/);
  await expect(page.getByRole("heading", { name: "任务事件" })).toBeVisible();
  await expect(page.getByText("保存执行计划")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "任务事件" })).toBeVisible();
});

test("human approval resolves once and the inbox records it as handled", async ({ page }) => {
  const browserErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));

  await page.goto("/inbox?approval_id=ap_ui");
  await expect(page.getByRole("heading", { name: "待处理通知" })).toBeVisible();
  await expect(page.getByText("工作流等待决议")).toBeVisible();
  await expect(page.getByLabel("操作人 ID")).toHaveCount(0);
  await expect(page.getByText("approve_taskbook")).toHaveCount(0);
  await expect(page.getByText("taskbook_review")).toHaveCount(0);
  await page.getByRole("button", { name: "批准并推进" }).click();
  await expect(page.getByText("该审批已完成处理。")).toBeVisible();
  await expect(page.getByText("已处理", { exact: true })).toBeVisible();
  expect(browserErrors).toEqual([]);
});
