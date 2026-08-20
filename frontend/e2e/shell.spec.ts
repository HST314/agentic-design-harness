import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.route("**/readyz", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ready" }),
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
