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
          details: { job_status: "succeeded", timeline_cursor: 7 },
        },
      }),
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
  }
});

test("task and instance pages preserve the Image workbench boundary", async ({ page }) => {
  await page.goto("/tasks");
  await page.getByRole("link", { name: "查看任务" }).click();
  await expect(page).toHaveURL(/\/tasks\/t_ui$/);
  await expect(page.getByRole("heading", { name: "阶段与实例" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "任务文件" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "final.png" })).toBeVisible();
  await page.getByRole("link", { name: /Image Agent/ }).click();
  await expect(page).toHaveURL(/\/instances\/i_ui$/);
  await expect(page.getByText("等待下一步决议")).toBeVisible();
  const workbench = page.getByRole("link", { name: "打开工作台" });
  await expect(workbench).toHaveAttribute("href", "http://127.0.0.1:18123/");
  await expect(workbench).toHaveAttribute("target", "_blank");
});

test("human approval resolves once and the inbox records it as handled", async ({ page }) => {
  await page.goto("/inbox?approval_id=ap_ui");
  await expect(page.getByRole("heading", { name: "按到达顺序处理" })).toBeVisible();
  await expect(page.getByText("工作流等待决议")).toBeVisible();
  await page.getByRole("button", { name: "批准并推进" }).click();
  await expect(page.getByText("该审批已完成处理。")).toBeVisible();
  await expect(page.getByText("已处理", { exact: true })).toBeVisible();
});
