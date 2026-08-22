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
            task_id: "task_launch_campaign",
            status: "RUNNING",
            title: "秋季发布会主视觉",
            updated_at: "2026-08-22T09:00:00Z",
            revision: 3,
            pinned_at: "2026-08-22T09:30:00Z",
            archived_at: null,
            presentation_revision: 2,
          },
          {
            task_id: "task_archive_review",
            status: "SUCCEEDED",
            title: "品牌资料归档",
            updated_at: "2026-08-21T09:00:00Z",
            revision: 2,
            pinned_at: null,
            archived_at: "2026-08-22T08:00:00Z",
            presentation_revision: 3,
          },
        ],
      }),
    });
  });
  await page.route("**/api/v1/task-intakes/*", async (route) => {
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ error: { message: "The requested task intake does not exist." } }),
    });
  });
});

test("root enters the accessible task-intake application shell", async ({ page }) => {
  await page.goto("/");

  await expect(page).toHaveURL(/\/tasks\/new$/);
  await expect(page.getByRole("heading", { name: "创建新的设计任务" })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "主任务历史" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "任务历史" })).toContainText("秋季发布会主视觉");
  await expect(page.getByRole("status").filter({ hasText: "服务就绪" })).toBeVisible();

  const skipLink = page.getByRole("link", { name: "跳到主要内容" });
  await skipLink.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#workbench-main")).toBeFocused();
});

test("task history search and the status drawer preserve route state", async ({ page }) => {
  await page.goto("/tasks/new");
  const search = page.getByRole("searchbox", { name: "搜索主任务" });
  await search.fill("归档");
  await expect(page.getByRole("link", { name: /品牌资料归档/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /秋季发布会主视觉/ })).toHaveCount(0);

  await page.getByRole("button", { name: "打开状态抽屉" }).click();
  await expect(page).toHaveURL(/drawer=status/);
  await expect(page.getByRole("dialog", { name: "工作台详情抽屉" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "运行状态" })).toBeVisible();
  await page.getByRole("button", { name: "关闭详情抽屉" }).click();
  await expect(page).not.toHaveURL(/drawer=status/);
});

test("master, board and plan deep links share one stable shell", async ({ page }) => {
  for (const route of ["master", "board", "plan"]) {
    await page.goto(`/tasks/task_launch_campaign/${route}`);
    await expect(page.getByRole("complementary", { name: "主任务历史" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "任务工作区" })).toBeVisible();
    await expect(page.getByText("任务 task_launch_campaign", { exact: true }).first()).toBeVisible();
  }
});

test("desktop target widths have no page-level horizontal overflow", async ({ page }) => {
  for (const width of [1280, 1440, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/tasks/new");
    const dimensions = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(dimensions.scrollWidth).toBe(dimensions.clientWidth);
  }
});
