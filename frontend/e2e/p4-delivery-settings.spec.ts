import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

test.beforeEach(async ({ page }) => {
  await page.route("**/readyz", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ready" }) }));
  await page.route("**/api/v1/tasks", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", items: [{ task_id: "task_delivery", status: "WAITING_APPROVAL", title: "品牌分支交付", updated_at: "2026-08-22T17:00:00Z", revision: 5 }] }) }));
});

test.describe("P4 branch delivery page", () => {
  test.beforeEach(async ({ page }) => {
    const status = new Map<string, "PENDING_CONFIRMATION" | "PUBLISHED" | "REJECTED">([
      ["bundle_branch_a", "PENDING_CONFIRMATION"],
      ["bundle_branch_b", "PENDING_CONFIRMATION"],
    ]);
    const candidate = (bundleId: string, branchId: string) => ({
      schema_version: "1.0",
      bundle_id: bundleId,
      task_id: "task_delivery",
      work_item_id: "work_key_visual",
      instance_id: "instance_image",
      task_card_revision: 3,
      branch_id: branchId,
      checkpoint_id: `checkpoint_${branchId}`,
      image: { private_relative_path: `instances/instance_image/outputs/${bundleId}.png`, mime_type: "image/png", size_bytes: png.length, sha256: "a".repeat(64), width: 1920, height: 1080 },
      design_note: { private_relative_path: `instances/instance_image/outputs/${bundleId}.md`, mime_type: "text/markdown", size_bytes: 84, sha256: "b".repeat(64) },
      status: status.get(bundleId),
      created_at: "2026-08-22T17:00:00Z",
      decided_at: status.get(bundleId) === "PENDING_CONFIRMATION" ? null : "2026-08-22T17:05:00Z",
      actor: status.get(bundleId) === "PENDING_CONFIRMATION" ? null : { type: "human", id: "human_operator" },
      publication_batch_id: status.get(bundleId) === "PUBLISHED" ? `batch_${bundleId}` : null,
    });
    const review = (bundleId: string, approvalId: string) => ({
      bundle_id: bundleId,
      approval: { schema_version: "1.0", approval_id: approvalId, task_id: "task_delivery", instance_id: "instance_image", step_id: `delivery_${bundleId}`, kind: "DELIVERY_REVIEW", owner: "human", status: status.get(bundleId) === "PENDING_CONFIRMATION" ? "PENDING" : status.get(bundleId) === "PUBLISHED" ? "APPROVED" : "REJECTED", payload_ref: `approvals/${approvalId}/request.json`, created_at: "2026-08-22T17:00:00Z", sequence: approvalId.endsWith("a") ? 1 : 2, revision: status.get(bundleId) === "PENDING_CONFIRMATION" ? 1 : 2 },
      approval_revision: status.get(bundleId) === "PENDING_CONFIRMATION" ? 1 : 2,
    });
    await page.route("**/api/v1/tasks/task_delivery/delivery-bundles", async (route) => {
      const manifests = [...status].filter(([, value]) => value === "PUBLISHED").map(([bundleId]) => ({ schema_version: "1.0", bundle_id: bundleId, task_id: "task_delivery", work_item_id: "work_key_visual", instance_id: "instance_image", task_card_revision: 3, branch_id: bundleId.endsWith("a") ? "branch_a" : "branch_b", checkpoint_id: `checkpoint_${bundleId}`, publication_batch_id: `batch_${bundleId}`, image_asset: { asset_id: `asset_${bundleId}_image`, manifest_relpath: `resources/manifests/asset_${bundleId}_image.json` }, design_note_asset: { asset_id: `asset_${bundleId}_note`, manifest_relpath: `resources/manifests/asset_${bundleId}_note.json` }, actor: { type: "human", id: "human_operator" }, created_at: "2026-08-22T17:00:00Z", published_at: "2026-08-22T17:05:00Z" }));
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", candidates: [candidate("bundle_branch_a", "branch_a"), candidate("bundle_branch_b", "branch_b")], manifests, reviews: [review("bundle_branch_a", "approval_a"), review("bundle_branch_b", "approval_b")] }) });
    });
    await page.route("**/api/v1/tasks/task_delivery/delivery-bundles/*/preview?*", async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.get("asset") === "image") {
        await route.fulfill({ status: 200, contentType: "image/png", body: png });
      } else {
        await route.fulfill({ status: 200, contentType: "text/markdown; charset=utf-8", body: "# 设计说明\n\n- 保留品牌安全区\n- 使用自然光层次" });
      }
    });
    await page.route("**/api/v1/approvals/*/resolve", async (route) => {
      const approvalId = route.request().url().includes("approval_a") ? "approval_a" : "approval_b";
      const bundleId = approvalId === "approval_a" ? "bundle_branch_a" : "bundle_branch_b";
      const body = route.request().postDataJSON() as { decision: "APPROVED" | "REJECTED"; action: string | null; payload: object; envelope: { expected_revision: number } };
      expect(body.envelope.expected_revision).toBe(1);
      expect(body.payload).toEqual({});
      if (body.decision === "APPROVED") expect(body.action).toBe("publish_bundle");
      else expect(body.action).toBeNull();
      status.set(bundleId, body.decision === "APPROVED" ? "PUBLISHED" : "REJECTED");
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", approval: { approval_id: approvalId, status: body.decision }, candidate: candidate(bundleId, bundleId.endsWith("a") ? "branch_a" : "branch_b") }) });
    });
  });

  test("previews paired assets, restores focus, confirms and rejects branches", async ({ page }) => {
    await page.goto("/tasks/task_delivery/deliveries?bundle_id=bundle_branch_a");
    await expect(page.getByRole("heading", { name: "交付包" })).toBeVisible();
    await expect(page.getByRole("article").filter({ hasText: "bundle_branch_a" })).toBeFocused();
    await expect(page.getByRole("img", { name: "分支 branch_a 最终图片预览" })).toBeVisible();
    await expect(page.getByLabel("渲染后的 Markdown 设计说明").first()).toContainText("保留品牌安全区");

    const approve = page.getByRole("article").filter({ hasText: "bundle_branch_a" }).getByRole("button", { name: "确认图片与说明并入库" });
    await approve.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog", { name: "确认双资产入库" })).toContainText("resources/shared（不可覆盖）");
    await page.keyboard.press("Escape");
    await expect(approve).toBeFocused();
    await approve.press("Enter");
    await page.getByRole("dialog", { name: "确认双资产入库" }).getByRole("button", { name: "确认图片与说明并入库" }).click();
    await expect(page.getByText("分支 branch_a 的双资产已原子入库。")).toBeVisible();
    const published = page.getByRole("article").filter({ hasText: "bundle_branch_a" });
    await expect(published.getByRole("button", { name: "已入库" })).toBeDisabled();
    await expect(published.getByRole("link", { name: "打开共享图片" })).toHaveAttribute("href", /resources\?asset_id=asset_bundle_branch_a_image/);
    await published.getByRole("button", { name: "验证入库结果" }).click();
    await expect(page.getByText("bundle_branch_a 的入库结果已复验。")).toBeVisible();

    const rejected = page.getByRole("article").filter({ hasText: "bundle_branch_b" });
    await rejected.getByRole("button", { name: "退回修改" }).click();
    await page.getByRole("dialog", { name: "退回分支修改" }).getByRole("button", { name: "确认退回修改" }).click();
    await expect(page.getByText("分支 branch_b 已退回，候选和记录均已保留。")).toBeVisible();
    await expect(rejected.getByText("已退回", { exact: true })).toBeVisible();
  });

  test("has no serious accessibility violations in light and dark delivery views", async ({ page }) => {
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      await page.goto("/tasks/task_delivery/deliveries");
      await expect(page.getByRole("heading", { name: "交付包" })).toBeVisible();
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      expect(results.violations).toEqual([]);
    }
    for (const width of [1280, 1440, 1920]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/tasks/task_delivery/deliveries");
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    }
  });
});

test.describe("P4 React settings page", () => {
  test.beforeEach(async ({ page }) => {
    let revision = 3;
    let paidRequests = 0;
    const roles: Record<string, string> = { intake_clarify: "reasoning_llm", confirmation_build: "reasoning_llm", initial_candidate_generation: "text_to_image_model", self_check_inspection: "vision_language_model", self_check_rework: "text_to_image_model", human_prompt_rework: "text_to_image_model" };
    const config = () => ({ schema_version: "1.0", revision, image_provider: "ark", image_model_config: { model_config_id: "ark_routes", state_bindings: Object.entries(roles).map(([state, model_role]) => ({ state, model_role, provider: "ark", model: `ark-${state}`, parameters: {}, fallback_model: null })) }, image_runtime_policy: { max_auto_questions: 3, stream_model_output: false, clarification_total_budget: 10, question_preference: "proactive", category_constraint: { release: "auto" }, style_direction: { release: "auto" }, skill_invocation: { release: "auto" }, self_check: { termination: "solo", fixed_rounds: 2, max_rounds: 4, stop_early_on_pass: false, release: "auto" }, max_render_retries: 0, candidate_concurrency: 5, model_timeout_seconds: 180, default_output_size: "1024x1024", response_format: "url", watermark: false, offline_mode: false, allow_skill_degradation: false, style_library_root: "agent-library" }, supervisor: { port_range_start: 18100, port_range_end: 18199, startup_timeout_seconds: 15, health_interval_seconds: 1, shutdown_grace_seconds: 5 } });
    let credentials = [{ credential_pair_id: "ark_primary", provider: "ark", key_id: "key_primary", key_tail: "-1234", base_url_hint: "https://ark.example/…", revision: 1, enabled: true }];
    await page.route("**/api/v1/config/global", async (route) => {
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON() as { config: ReturnType<typeof config>; envelope: { expected_revision: number } };
        expect(body.envelope.expected_revision).toBe(revision);
        expect(body.config.image_provider).toBe("ark");
        expect(body.config.image_model_config.state_bindings).toHaveLength(6);
        expect(new Set(body.config.image_model_config.state_bindings.map((item) => item.state))).toEqual(new Set(Object.keys(roles)));
        revision += 1;
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", config: { ...body.config, revision } }) });
        return;
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", config: config() }) });
    });
    await page.route("**/api/v1/key-pool", async (route) => {
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON() as { pairs: Array<{ api_key: string; provider: string; base_url: string }> };
        expect(body.pairs[0]?.provider).toBe("ark");
        expect(body.pairs[0]?.api_key).toBe("browser-only-ark-secret");
        credentials = [{ credential_pair_id: "ark_primary", provider: "ark", key_id: "ark_key_primary", key_tail: "cret", base_url_hint: "https://ark.example/…", revision: 2, enabled: true }];
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", pairs: credentials, count: 1 }) });
        return;
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", items: credentials }) });
    });
    await page.route("**/api/v1/config/diagnostics/preflight", async (route) => {
      expect((route.request().postDataJSON() as { expected_config_revision: number }).expected_config_revision).toBe(revision);
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", status: "READY", config_revision: revision, provider: "ark", model_config_id: "ark_routes", credential_pairs: credentials, checks: [{ check_id: "provider", status: "PASS", message: "Provider 已设置为 Ark。", recovery: null }, { check_id: "six_state_routes", status: "PASS", message: "六个状态完整。", recovery: null }, { check_id: "cost_safety", status: "PASS", message: "本次预检不会产生图片费用。", recovery: null }], paid_request_performed: false, checked_at: "2026-08-22T17:10:00Z" }) });
    });
    await page.route("**/api/v1/config/diagnostics/paid-smoke", async (route) => {
      paidRequests += 1;
      const body = route.request().postDataJSON() as { cost_confirmation: boolean; envelope: { expected_revision: number } };
      expect(body.cost_confirmation).toBe(true);
      expect(body.envelope.expected_revision).toBe(revision);
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", status: "PASSED", config_revision: revision, provider: "ark", model: "ark-initial_candidate_generation", credential_pair: credentials[0], generated_count: 1, duration_ms: 842, paid_request_performed: true, completed_at: "2026-08-22T17:11:00Z" }) });
    });
    await page.exposeFunction("p4PaidRequestCount", () => paidRequests);
  });

  test("saves redacted Ark credentials, all six routes, and separates free from paid diagnostics", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Ark 与 Image Agent 设置" })).toBeVisible();
    await expect(page.getByText("-1234", { exact: true })).toBeVisible();
    await expect(page.getByRole("group", { name: "六状态模型路由" }).locator("fieldset")).toHaveCount(6);

    const secret = page.getByLabel("Ark API Key");
    await secret.fill("browser-only-ark-secret");
    await page.getByRole("button", { name: "安全保存 Ark 凭据" }).click();
    await expect(secret).toHaveValue("");
    await expect(page.locator("body")).not.toContainText("browser-only-ark-secret");
    await expect(page.getByText("凭据已保存", { exact: false })).toBeVisible();

    const generation = page.getByRole("group", { name: "首轮候选生成" });
    await generation.getByLabel("Ark 模型 ID").fill("doubao-seedream-p4");
    await page.getByRole("button", { name: "保存六状态模型路由" }).click();
    await expect(page.getByText(/全局配置已保存为 r4/)).toBeVisible();

    await page.getByRole("button", { name: "运行配置预检（不生图）" }).click();
    await expect(page.getByText("预检通过", { exact: true })).toBeVisible();
    expect(await page.evaluate(async () => (window as unknown as { p4PaidRequestCount: () => Promise<number> }).p4PaidRequestCount())).toBe(0);
    const paidButton = page.getByRole("button", { name: "打开付费 smoke 确认" });
    await expect(paidButton).toBeDisabled();
    await page.getByRole("checkbox", { name: "我确认下一步会产生一次真实图片生成费用" }).check();
    await expect(paidButton).toBeEnabled();
    await paidButton.focus();
    await paidButton.press("Enter");
    const dialog = page.getByRole("dialog", { name: "确认一次付费生图" });
    await expect(dialog).toContainText("普通保存和配置预检永远不会隐式触发");
    await page.keyboard.press("Escape");
    await expect(paidButton).toBeFocused();
    await paidButton.press("Enter");
    await page.getByRole("dialog", { name: "确认一次付费生图" }).getByRole("button", { name: "确认费用并生成 1 张" }).click();
    await expect(page.getByText(/Ark 真实 smoke 已通过/)).toBeVisible();
    expect(await page.evaluate(async () => (window as unknown as { p4PaidRequestCount: () => Promise<number> }).p4PaidRequestCount())).toBe(1);
  });

  test("has no serious accessibility violations in light and dark settings views", async ({ page }) => {
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      await page.goto("/settings");
      await expect(page.getByRole("heading", { name: "Ark 与 Image Agent 设置" })).toBeVisible();
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      expect(results.violations).toEqual([]);
    }
  });
});
