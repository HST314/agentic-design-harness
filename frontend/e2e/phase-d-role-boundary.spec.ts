import { expect, test } from "@playwright/test";

test("designer shell has no settings surface or configuration API traffic", async ({ page }) => {
  const configurationRequests: string[] = [];
  page.on("request", (request) => {
    if (/\/(?:api\/v1\/config|api\/v1\/key-pool)(?:\/|$)/.test(new URL(request.url()).pathname)) {
      configurationRequests.push(request.url());
    }
  });
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
      body: JSON.stringify({ schema_version: "1.0", items: [] }),
    });
  });

  await page.goto("/settings");

  await expect(page).toHaveURL(/\/tasks\/new$/);
  await expect(page.getByRole("heading", { name: "创建新的设计任务" })).toBeVisible();
  await expect(page.getByRole("link", { name: "设置" })).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(/API Key|Provider|endpoint|离线模式|付费 smoke|模型路由/);
  expect(configurationRequests).toEqual([]);
});
