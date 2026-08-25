import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const currentDir = path.dirname(fileURLToPath(import.meta.url));
const imageFrontend = path.resolve(currentDir, "../../agents/image_agent_mvp/frontend");
const imageOrigin = "http://127.0.0.1:19094";

const fixture = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="/static/css/main.css">
  <title>Image Agent 设置</title>
</head>
<body>
  <div class="app app--managed">
    <header class="topnav">
      <div class="topnav__brand"><div class="brand__mark" aria-hidden="true"></div><strong>Image Agent</strong></div>
      <nav class="topnav__tabs" aria-label="主导航">
        <button type="button" class="topnav__tab">工作区</button>
        <button type="button" class="topnav__tab">状态</button>
        <button type="button" class="topnav__tab" aria-current="page">设置</button>
      </nav>
      <div class="topnav__context"><button class="btn btn--secondary">刷新</button></div>
    </header>
    <div class="app-body"><main class="main"><div class="content" id="content"></div></main></div>
  </div>
  <script type="module">
    import { renderRuntimeSettingsPage } from '/static/js/settingspage.js';

    const selfCheck = {
      termination: 'solo', fixed_rounds: 2, max_rounds: 4, stop_early_on_pass: false,
    };
    const values = {
      question_preference: { inherited: 'proactive', effective: 'proactive', overridden: false },
      max_auto_questions: { inherited: 3, effective: 3, overridden: false },
      clarification_total_budget: { inherited: 10, effective: 10, overridden: false },
      category_constraint: {
        inherited: { release: 'auto' }, effective: { release: 'manual' },
        explicit: { release: 'manual' }, overridden: true,
      },
      style_direction: {
        inherited: { release: 'auto' }, effective: { release: 'off' },
        explicit: { release: 'off' }, overridden: true,
      },
      candidate_concurrency: { inherited: 5, effective: 3, explicit: 3, overridden: true },
      default_output_size: { inherited: '2560x1440', effective: '2560x1440', overridden: false },
      response_format: { inherited: 'url', effective: 'url', overridden: false },
      watermark: { inherited: false, effective: false, overridden: false },
      self_check: { inherited: selfCheck, effective: selfCheck, explicit: {}, overridden: false },
    };
    const modelBindings = Object.fromEntries([
      ['intake_clarify', 'text-primary'],
      ['confirmation_build', 'text-primary'],
      ['initial_candidate_generation', 'image-primary'],
      ['self_check_inspection', 'vision-primary'],
      ['self_check_rework', 'image-primary'],
      ['human_prompt_rework', 'image-primary'],
    ].map(([key, value]) => [key, { inherited: value, effective: value, overridden: false }]));
    const modelOptions = Object.fromEntries(
      Object.entries(modelBindings).map(([key, value]) => [key, [{ id: value.effective, label: value.effective }]]),
    );
    renderRuntimeSettingsPage(
      document.querySelector('#content'),
      { project_id: 'instance_image' },
      { managed: true, editable: true },
      {
        load: async () => ({
          revision: { current: 3, revision_id: 'cfg-inst-r000003' },
          values,
          model_bindings: modelBindings,
          model_options: modelOptions,
          sync_candidates: [{ instance_id: 'instance_image_2' }],
          editable: true,
        }),
        propose: async (payload) => {
          window.proposalPayload = payload;
          return {
            proposal_id: 'proposal_settings_ui',
            diff: [{ field: 'candidate_concurrency', before: 3, after: 4 }],
          };
        },
      },
    );
  </script>
</body>
</html>`;

async function routeImageFrontend(page: Page) {
  await page.route(`${imageOrigin}/**`, async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/") {
      await route.fulfill({ status: 200, contentType: "text/html", body: fixture });
      return;
    }
    const relative = pathname.replace(/^\//, "");
    const filePath = path.resolve(imageFrontend, relative);
    if (!filePath.startsWith(`${imageFrontend}${path.sep}`)) {
      await route.fulfill({ status: 404, body: "Not found" });
      return;
    }
    const contentType = pathname.endsWith(".css") ? "text/css" : "text/javascript";
    await route.fulfill({ status: 200, contentType, body: await readFile(filePath) });
  });
}

test("renders the task settings layout with six accessible tabs and a save scope", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await routeImageFrontend(page);
  await page.goto(`${imageOrigin}/`);

  await expect(page.getByRole("tab")).toHaveCount(6);
  expect(await page.getByRole("tab").allTextContents()).toEqual([
    "提问与澄清",
    "数据库与放行",
    "候选与出图",
    "质量自检",
    "系统与高级",
    "模型",
  ]);
  await expect(page.getByLabel("自动提问上限")).toBeEnabled();
  await expect(page.getByText("同步到本任务其他 Image Agent")).toBeVisible();

  await page.getByRole("tab", { name: "候选与出图" }).click();
  await expect(page.getByLabel("候选图并发数")).toHaveValue("3");
  await page.getByLabel("候选图并发数").fill("4");
  await page.getByLabel("同步到本任务其他 Image Agent").check();
  await page.getByRole("button", { name: "预览设置变更" }).click();
  await expect(page.getByRole("heading", { name: "变更预览" })).toBeVisible();
  expect(await page.evaluate(
    () => (window as typeof window & { proposalPayload?: unknown }).proposalPayload,
  )).toEqual({
    base_revision: 3,
    overrides: { candidate_concurrency: 4 },
    sync_unstarted_image_work_items: true,
    expected_sync_instance_ids: ["instance_image_2"],
  });
  await page.getByRole("tab", { name: "数据库与放行" }).click();
  await expect(page.getByLabel("品类约束放行方式")).toHaveValue("manual");
  await expect(page.getByLabel("艺术风格放行方式")).toHaveValue("off");
  await expect.poll(() => page.evaluate(
    () => document.documentElement.scrollWidth === document.documentElement.clientWidth,
  )).toBe(true);

  if (process.env.SETTINGS_UI_SCREENSHOT) {
    await page.screenshot({ path: process.env.SETTINGS_UI_SCREENSHOT, fullPage: true });
  }
});
