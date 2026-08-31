import AxeBuilder from "@axe-core/playwright";
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
  await page.route("**/api/v1/tasks/task_launch_campaign/work-items", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        task: {
          schema_version: "1.0",
          task_id: "task_launch_campaign",
          title: "秋季发布会主视觉",
          goal: "完成主视觉",
          master_owner: "master_default",
          start_policy: "manual",
          status: "RUNNING",
          created_at: "2026-08-22T09:00:00Z",
          updated_at: "2026-08-22T09:00:00Z",
          input_manifest: "inputs/manifests/input.json",
          plan_revision: 1,
        },
        stages: [],
        items: [],
        summary: { TODO: 0, RUNNING: 0, WAITING_APPROVAL: 0, COMPLETED: 0, EXCEPTION: 0 },
        refresh_after_ms: 5000,
        projection_revision: "shell-empty",
      }),
    });
  });
  await page.route("**/api/v1/tasks/task_launch_campaign/master/messages", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        thread: {
          schema_version: "1.0",
          task_id: "task_launch_campaign",
          latest_sequence: 0,
          latest_proposal_revision: 0,
          active_run: null,
          last_error: null,
          revision: 1,
          created_at: "2026-08-22T09:00:00Z",
          updated_at: "2026-08-22T09:00:00Z",
        },
        thread_revision: 1,
        messages: [],
        latest_proposal: null,
        proposals: [],
        task: {
          schema_version: "1.0",
          task_id: "task_launch_campaign",
          title: "秋季发布会主视觉",
          goal: "完成主视觉",
          master_owner: "master_default",
          start_policy: "manual",
          status: "RUNNING",
          created_at: "2026-08-22T09:00:00Z",
          updated_at: "2026-08-22T09:00:00Z",
          input_manifest: "inputs/manifests/input.json",
          plan_revision: 1,
        },
        task_revision: 3,
        gateway_available: true,
        assets: [],
      }),
    });
  });
});

test("root enters the accessible task-intake application shell", async ({ page }) => {
  await page.goto("/");

  await expect(page).toHaveURL(/\/tasks\/new$/);
  await expect(page).toHaveTitle("面向广告全案交付的企业级多智能体设计操作系统");
  const brand = page.getByRole("link", {
    name: "面向广告全案交付的企业级多智能体设计操作系统，返回新任务页",
  });
  await expect(brand).toHaveAttribute("title", "面向广告全案交付的企业级多智能体设计操作系统");
  await expect(brand).toContainText("DH");
  await expect(brand).toContainText("DesignHarness");
  await expect(brand).toContainText("企业级多智能体设计操作系统");
  await expect(page.locator('link[rel~="icon"]')).toHaveAttribute("href", "/favicon.svg");
  const faviconResponse = await page.request.get("/favicon.svg");
  expect(faviconResponse.ok()).toBe(true);
  expect(await faviconResponse.text()).toContain("DesignHarness (DH)");
  await expect(page.getByRole("heading", { name: "创建新的设计任务" })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "主任务历史" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "任务历史" })).toContainText("秋季发布会主视觉");
  await expect(page.getByRole("region", { name: "已归档" })).toContainText("品牌资料归档");
  await expect(page.getByRole("link", { name: /品牌资料归档/ })).toContainText("已归档");
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
    await expect(page.locator("#workbench-main")).toBeVisible();
    await expect(page.locator(".workbench-topbar")).toContainText("秋季发布会主视觉");
    if (route === "board") {
      await expect(page.locator(".task-projection--board")).toBeVisible();
    } else if (route === "plan") {
      await expect(page.locator(".task-projection--plan")).toBeVisible();
    }
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

test("core workbench routes pass the WCAG 2.1 A/AA automated audit", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });

  for (const route of [
    "/tasks/new",
    "/tasks/task_launch_campaign/master",
    "/tasks/task_launch_campaign/board",
    "/tasks/task_launch_campaign/plan",
  ]) {
    await page.goto(route);
    await expect(page.locator("#workbench-main")).toBeVisible();
    const audit = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();

    expect(
      audit.violations,
      `${route}: ${audit.violations.map((violation) => violation.id).join(", ")}`,
    ).toEqual([]);
  }
});

test("reduced-motion keeps feedback effectively instant without removing focus", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tasks/new");
  const action = page.getByRole("link", { name: "新任务", exact: true });
  await action.focus();
  await expect(action).toBeFocused();
  await expect.poll(async () => action.evaluate((element) => (
    Number.parseFloat(getComputedStyle(element).transitionDuration) <= 0.001
  ))).toBe(true);
});
