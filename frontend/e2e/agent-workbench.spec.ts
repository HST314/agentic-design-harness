import { expect, test } from "@playwright/test";

const task = {
  schema_version: "1.0",
  task_id: "task_workbench_e2e",
  title: "秋季发布会主视觉",
  goal: "验证内嵌专业工作台。",
  master_owner: "master_default",
  start_policy: "manual",
  status: "RUNNING",
  created_at: "2026-08-22T10:00:00Z",
  updated_at: "2026-08-22T10:05:00Z",
  input_manifest: "inputs/manifests/input.json",
  plan_revision: 2,
};

function workItem(agentType: "image" | "ppt") {
  const ppt = agentType === "ppt";
  return {
    schema_version: "1.0",
    work_item_id: ppt ? "work_ppt" : "work_image",
    task_id: task.task_id,
    title: ppt ? "发布会演示文稿" : "KV 方向 A",
    agent_type: agentType,
    required: true,
    depends_on: [],
    stage: {
      stage_id: ppt ? "stage_ppt" : "stage_image",
      position: ppt ? 2 : 1,
      type: agentType,
      status: ppt ? "UNAVAILABLE" : "RUNNING",
      depends_on: [],
      available: !ppt,
    },
    business_status: ppt ? "EXCEPTION" : "RUNNING",
    raw_status: ppt ? "UNAVAILABLE" : "RUNNING",
    current_instance: {
      instance_id: ppt ? "instance_ppt" : "instance_image",
      status: ppt ? "UNAVAILABLE" : "RUNNING",
      approval_mode: "human",
      process_state: ppt ? null : "RUNNING",
      restart_required: false,
      created_at: "2026-08-22T10:00:00Z",
    },
    instance_ids: [ppt ? "instance_ppt" : "instance_image"],
    attempts: [],
    pending_approvals: [],
    delivery_count: 0,
    alerts: ppt ? [{ code: "ADAPTER_UNAVAILABLE", severity: "error", message: "PPT 能力尚未接入。" }] : [],
    updated_at: "2026-08-22T10:05:00Z",
  };
}

async function mockShell(page) {
  await page.route("**/readyz", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ready" }) }));
  await page.route("**/api/v1/tasks", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", items: [{ task_id: task.task_id, title: task.title, status: task.status, updated_at: task.updated_at, revision: 6 }] }) }));
}

test.beforeEach(async ({ page }) => {
  await mockShell(page);
});

test("embeds only the server-approved current Image instance with keyboard exits", async ({ page }) => {
  const item = workItem("image");
  await page.route("**/api/v1/tasks/task_workbench_e2e/work-items/work_image", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", task, item, refresh_after_ms: 3000, projection_revision: "image-ready" }) }));
  await page.route("**/api/v1/instances/instance_image/ui-link?*", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", task_id: task.task_id, work_item_id: item.work_item_id, instance_id: "instance_image", agent_type: "image", instance_status: "RUNNING", ui_url: "http://127.0.0.1:19091/", link_status: "READY", embeddable: true, frame_policy: "FRAME_ANCESTORS_ALLOWED", diagnostic: "Allowed." }) }));
  await page.route("http://127.0.0.1:19091/", async (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html><html><body><main><h1>Image Agent Studio</h1><a download href='data:text/plain,asset'>下载交付物</a></main></body></html>" }));

  await page.goto("/tasks/task_workbench_e2e/work-items/work_image");
  await expect(page.getByRole("heading", { name: "KV 方向 A" })).toBeVisible();
  await expect(page.getByText("专业工作台连接已验证")).toBeVisible();
  await expect(page.locator("body")).not.toContainText(/Adapter|frame 策略|iframe|sandbox/);
  const iframe = page.locator("iframe[title='Image Agent 工作台：KV 方向 A']");
  await expect(iframe).toHaveAttribute("src", "http://127.0.0.1:19091/");
  await expect(iframe).toHaveAttribute("sandbox", /allow-downloads/);
  await expect(iframe).toHaveAttribute("sandbox", /allow-scripts/);
  await expect(iframe).not.toHaveAttribute("sandbox", /allow-top-navigation/);
  await expect(iframe).toHaveAttribute("referrerpolicy", "origin");
  await expect(page.frameLocator("iframe").getByRole("heading", { name: "Image Agent Studio" })).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveCount(0);
  const focusLink = page.getByRole("link", { name: "新标签页" });
  await expect(focusLink).toHaveAttribute(
    "href",
    "/tasks/task_workbench_e2e/work-items/work_image/focus",
  );
  await expect(focusLink).toHaveAttribute("rel", "noopener noreferrer");

  await page.getByRole("button", { name: "跳到 Image Agent 工作台" }).click();
  await expect.poll(() => page.evaluate(() => document.activeElement?.tagName)).toBe("IFRAME");
  await page.getByRole("button", { name: "返回工作台操作栏" }).click();
  await expect.poll(() => page.evaluate(() => document.activeElement?.classList.contains("agent-workbench__actions"))).toBe(true);

  for (const width of [1280, 1440, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth === document.documentElement.clientWidth)).toBe(true);
  }
});

test("bridges managed settings through the verified parent with a rotating nonce", async ({ page }) => {
  const item = workItem("image");
  let proposalBody: Record<string, unknown> | null = null;
  await page.route("**/api/v1/tasks/task_workbench_e2e/work-items/work_image", async (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ schema_version: "1.0", task, item, refresh_after_ms: 3000, projection_revision: "image-settings" }),
  }));
  await page.route("**/api/v1/instances/instance_image/ui-link?*", async (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      schema_version: "1.0",
      task_id: task.task_id,
      work_item_id: item.work_item_id,
      instance_id: "instance_image",
      agent_type: "image",
      instance_status: "RUNNING",
      task_revision: 9,
      ui_url: "http://127.0.0.1:19093/",
      link_status: "READY",
      embeddable: true,
      frame_policy: "FRAME_ANCESTORS_ALLOWED",
      diagnostic: "Allowed.",
    }),
  }));
  await page.route("**/api/v1/instances/instance_image/runtime-settings", async (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ schema_version: "2.0", revision: { current: 3 }, values: {} }),
  }));
  await page.route("**/api/v1/instances/instance_image/runtime-setting-proposals", async (route) => {
    proposalBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ schema_version: "2.0", proposal_id: "proposal_e2e", diff: [] }),
    });
  });
  await page.route("http://127.0.0.1:19093/", async (route) => route.fulfill({
    status: 200,
    contentType: "text/html",
    body: `<!doctype html><html><body><main><h1>Image Agent Studio</h1><output id="bridge-result">等待桥接</output></main><script>
      const parentOrigin = new URL(document.referrer).origin;
      const base = { protocol: 'image-agent-runtime-settings', version: '1.0', instance_id: 'instance_image' };
      let step = 'get';
      addEventListener('message', (event) => {
        if (event.source !== parent || event.origin !== parentOrigin || event.data.protocol !== base.protocol) return;
        if (event.data.type === 'bridge.init') {
          parent.postMessage({ ...base, type: 'bridge.request', request_id: 'bridge_settings_get_123', nonce: event.data.nonce, action: 'runtime_settings.get', payload: {} }, parentOrigin);
        } else if (event.data.type === 'bridge.response' && event.data.ok && step === 'get') {
          step = 'propose';
          parent.postMessage({ ...base, type: 'bridge.request', request_id: 'bridge_settings_propose_456', nonce: event.data.next_nonce, action: 'runtime_settings.propose', payload: { base_revision: 3, overrides: { watermark: true }, sync_unstarted_image_work_items: false, expected_sync_instance_ids: [] } }, parentOrigin);
        } else if (event.data.type === 'bridge.response' && event.data.ok && step === 'propose') {
          document.querySelector('#bridge-result').textContent = event.data.payload.proposal_id;
        }
      });
      parent.postMessage({ ...base, type: 'bridge.hello' }, parentOrigin);
    </script></html>`,
  }));

  await page.goto("/tasks/task_workbench_e2e/work-items/work_image/focus");
  await expect(page.locator(".workbench-sidebar")).toHaveCount(0);
  await expect(page.locator(".agent-workbench--focus")).toBeVisible();
  await expect(page.frameLocator("iframe").locator("#bridge-result")).toHaveText("proposal_e2e");
  expect(proposalBody).toMatchObject({
    base_revision: 3,
    overrides: { watermark: true },
    sync_unstarted_image_work_items: false,
    expected_sync_instance_ids: [],
    envelope: {
      actor_type: "human",
      actor_id: "human_operator",
      expected_revision: 9,
    },
  });
  expect(JSON.stringify(proposalBody)).not.toContain("Adapter");
  expect(JSON.stringify(proposalBody)).not.toContain("request_key");
});

test("shows frame-policy failure with a controlled external fallback", async ({ page }) => {
  const item = workItem("image");
  await page.route("**/api/v1/tasks/task_workbench_e2e/work-items/work_image", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", task, item, refresh_after_ms: 3000, projection_revision: "image-blocked" }) }));
  await page.route("**/api/v1/instances/instance_image/ui-link?*", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", task_id: task.task_id, work_item_id: item.work_item_id, instance_id: "instance_image", agent_type: "image", instance_status: "RUNNING", ui_url: "http://127.0.0.1:19092/", link_status: "FRAME_BLOCKED", embeddable: false, frame_policy: "X_FRAME_OPTIONS_BLOCKED", diagnostic: "Image Agent only permits same-origin framing." }) }));

  await page.goto("/tasks/task_workbench_e2e/work-items/work_image");
  await expect(page.getByRole("heading", { name: "Image Agent 无法安全内嵌" })).toBeVisible();
  await expect(page.locator("iframe")).toHaveCount(0);
  const fallback = page.getByRole("link", { name: "直接打开原始工作台" });
  await expect(fallback).toHaveAttribute("href", "http://127.0.0.1:19092/");
  await expect(fallback).toHaveAttribute("rel", "noopener noreferrer");
  await expect(page.getByRole("button", { name: "重新检查" })).toBeVisible();
});

test("keeps PPT as a truthful unavailable boundary without requesting a UI link", async ({ page }) => {
  const item = workItem("ppt");
  let linkRequests = 0;
  await page.route("**/api/v1/tasks/task_workbench_e2e/work-items/work_ppt", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", task, item, refresh_after_ms: 5000, projection_revision: "ppt-unavailable" }) }));
  await page.route("**/api/v1/instances/*/ui-link?*", async (route) => { linkRequests += 1; await route.abort(); });

  await page.goto("/tasks/task_workbench_e2e/work-items/work_ppt");
  await expect(page.getByRole("heading", { name: "PPT 工作台暂不可用" })).toBeVisible();
  await expect(page.getByText(/返回看板调整计划/)).toBeVisible();
  expect(linkRequests).toBe(0);
});
