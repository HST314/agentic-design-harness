import { expect, test } from "@playwright/test";

const harnessSettings = {
  schema_version: "1.0",
  server: { host: "127.0.0.1", port: 18080, log_level: "INFO" },
  models: {
    master: "ark-text-primary",
    text_reasoning: "ark-text-primary",
    vision_understanding: "ark-vlm-primary",
    image_generation: "ark-image-primary",
  },
  master: {
    model_timeout_seconds: 180,
    max_tool_rounds: 8,
    max_clarification_questions: 3,
    require_plan_confirmation: true,
    default_start_policy: "manual",
  },
  document_processing: {
    max_files_per_task: 20,
    max_total_bytes: 209715200,
    max_pdf_pages: 100,
    text_chunk_chars: 6000,
    visual_analysis: "auto",
    require_source_citations: true,
  },
  supervisor: {
    port_range_start: 18100,
    port_range_end: 18199,
    startup_timeout_seconds: 30,
    shutdown_grace_seconds: 5,
  },
};

const imageSettings = {
  schema_version: "1.0",
  question_preference: "proactive",
  max_auto_questions: 3,
  clarification_total_budget: 10,
  category_constraint: { release: "off" },
  style_direction: { release: "off" },
  candidate_concurrency: 5,
  default_output_size: "2560x1440",
  response_format: "url",
  watermark: false,
  self_check: {
    termination: "solo",
    fixed_rounds: 2,
    max_rounds: 4,
    stop_early_on_pass: false,
  },
  advanced_model_overrides: {
    intake_clarify: null,
    confirmation_build: null,
    initial_candidate_generation: null,
    self_check_inspection: null,
    self_check_rework: null,
    human_prompt_rework: null,
  },
};

test("global settings previews and publishes secret-free Harness and Image defaults", async ({ page }) => {
  const forbiddenConfigurationRequests: string[] = [];
  const settingsRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (/\/(?:api\/v1\/config|api\/v1\/key-pool)(?:\/|$)/.test(path)) {
      forbiddenConfigurationRequests.push(request.url());
    }
    if (path.startsWith("/api/v1/system-settings")) {
      settingsRequests.push(`${request.method()} ${path}`);
    }
  });
  await page.route("**/readyz", async (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ status: "ready" }),
  }));
  await page.route("**/api/v1/tasks", async (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ schema_version: "1.0", items: [] }),
  }));
  await page.route("**/api/v1/system-settings{,/**}", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/preview")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "1.0",
          preview_id: `settings-preview-${"b".repeat(24)}`,
          base_revision: `cfg_${"a".repeat(24)}`,
          candidate_revision: `cfg_${"b".repeat(24)}`,
          changes: [{
            field: "image_agent_settings.candidate_concurrency",
            before: 5,
            after: 4,
          }],
          harness_settings: harnessSettings,
          image_agent_settings: { ...imageSettings, candidate_concurrency: 4 },
        }),
      });
      return;
    }
    if (path.endsWith("/publish")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "1.0",
          status: "PUBLISHED",
          revision: `cfg_${"b".repeat(24)}`,
          changes: [],
          distribution: {
            updated: 2,
            waiting_safe_point: 0,
            failed: 0,
            completed_history_unchanged: 1,
            items: [],
          },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1.0",
        revision: `cfg_${"a".repeat(24)}`,
        harness_settings: harnessSettings,
        image_agent_settings: imageSettings,
        editable_schema: {},
        model_options: {
          text_models: [{ id: "ark-text-primary", label: "文本模型" }],
          vlm_models: [{ id: "ark-vlm-primary", label: "视觉模型" }],
          image_models: [{ id: "ark-image-primary", label: "图像模型" }],
        },
        last_publication: null,
      }),
    });
  });

  await page.goto("/settings");

  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("heading", { name: "全局设置" })).toBeVisible();
  await expect(page.getByRole("link", { name: "全局设置" })).toBeVisible();
  await page.getByRole("tab", { name: /专业助手设置/ }).click();
  await expect(page.getByLabel("品类约束库")).toHaveValue("off");
  await expect(page.getByLabel("风格方向库")).toHaveValue("off");
  await page.getByLabel("候选并发数").fill("4");
  await page.getByRole("button", { name: "预览更改" }).click();
  await expect(page.getByRole("heading", { name: "发布差异" })).toBeVisible();
  await page.getByRole("button", { name: "发布并同步" }).click();
  await expect(page.getByText("全局设置已发布并完成同步。")).toBeVisible();

  expect(settingsRequests).toContain("GET /api/v1/system-settings");
  expect(settingsRequests).toContain("POST /api/v1/system-settings/preview");
  expect(settingsRequests).toContain("POST /api/v1/system-settings/publish");
  expect(forbiddenConfigurationRequests).toEqual([]);
  await expect(page.locator("body")).not.toContainText(/API Key|Provider endpoint|付费 smoke/);
});
