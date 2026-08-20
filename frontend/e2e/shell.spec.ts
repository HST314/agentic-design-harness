import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
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
  await page.route("**/api/v1/instances/i_ui", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        task_id: "t_ui",
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
  await page.getByRole("link", { name: /Image Agent/ }).click();
  await expect(page).toHaveURL(/\/instances\/i_ui$/);
  await expect(page.getByText("等待下一步决议")).toBeVisible();
  const workbench = page.getByRole("link", { name: "打开工作台" });
  await expect(workbench).toHaveAttribute("href", "http://127.0.0.1:18123/");
  await expect(workbench).toHaveAttribute("target", "_blank");
});
