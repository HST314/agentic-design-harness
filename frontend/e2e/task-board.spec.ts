import { expect, test } from "@playwright/test";

const task = {
  schema_version: "1.0",
  task_id: "task_board_e2e",
  title: "秋季发布会主视觉",
  goal: "完成主视觉与演示文稿。",
  master_owner: "master_default",
  start_policy: "manual",
  status: "RUNNING",
  created_at: "2026-08-22T10:00:00Z",
  updated_at: "2026-08-22T10:05:00Z",
  input_manifest: "inputs/manifests/input.json",
  plan_revision: 2,
};

function item(
  id: string,
  title: string,
  status: "TODO" | "RUNNING" | "WAITING_APPROVAL" | "COMPLETED" | "EXCEPTION",
  rawStatus: string,
  stage: 1 | 2,
) {
  const ppt = stage === 2;
  return {
    schema_version: "1.0",
    work_item_id: id,
    task_id: task.task_id,
    title,
    agent_type: ppt ? "ppt" : "image",
    required: true,
    depends_on: ppt ? ["work_kv_a"] : [],
    stage: {
      stage_id: ppt ? "stage_ppt" : "stage_image",
      position: stage,
      type: ppt ? "ppt" : "image",
      status: rawStatus,
      depends_on: ppt ? ["stage_image"] : [],
      available: !ppt,
    },
    business_status: status,
    raw_status: rawStatus,
    current_instance: {
      instance_id: `instance_${id}`,
      status: rawStatus,
      approval_mode: "human",
      manual_finished: status === "COMPLETED" && !ppt,
      process_state: status === "RUNNING" ? "RUNNING" : null,
      restart_required: false,
      created_at: "2026-08-22T10:00:00Z",
    },
    instance_ids: id === "work_kv_a" ? ["instance_kv_a_01", "instance_kv_a_02"] : [`instance_${id}`],
    attempts: id === "work_kv_a" ? [{ attempt_id: "attempt_kv_a_retry", instance_id: "instance_kv_a_02", status: "RESERVED", automatic: true, created_at: "2026-08-22T10:03:00Z", settled_at: null }] : [],
    pending_approvals: status === "WAITING_APPROVAL" ? [{ approval_id: `approval_${id}`, kind: "WORKFLOW", owner: "human", created_at: "2026-08-22T10:04:00Z" }] : [],
    delivery_count: status === "COMPLETED" ? 3 : 0,
    alerts: ppt ? [{ code: "ADAPTER_UNAVAILABLE", severity: "error", message: "PPT 能力尚未接入。" }] : [],
    updated_at: "2026-08-22T10:05:00Z",
  };
}

const items = [
  item("work_todo", "KV 方向 B", "TODO", "READY", 1),
  item("work_kv_a", "KV 方向 A", "RUNNING", "RUNNING", 1),
  item("work_approval", "KV 方向 C", "WAITING_APPROVAL", "WAITING_APPROVAL", 1),
  item("work_complete", "KV 方向 D", "COMPLETED", "SUCCEEDED", 1),
  item("work_ppt", "整合演示文稿", "EXCEPTION", "UNAVAILABLE", 2),
];

const projection = {
  schema_version: "1.0",
  task,
  task_revision: 6,
  stages: [
    { stage_id: "stage_image", position: 1, type: "image", required: true, depends_on: [], status: "RUNNING", available: true, work_item_ids: ["work_todo", "work_kv_a", "work_approval", "work_complete"] },
    { stage_id: "stage_ppt", position: 2, type: "ppt", required: true, depends_on: ["stage_image"], status: "UNAVAILABLE", available: false, work_item_ids: ["work_ppt"] },
  ],
  items,
  summary: { TODO: 1, RUNNING: 1, WAITING_APPROVAL: 1, COMPLETED: 1, EXCEPTION: 1 },
  refresh_after_ms: 3000,
  projection_revision: "abc123",
};

test.beforeEach(async ({ page }) => {
  await page.route("**/readyz", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ready" }) }));
  await page.route("**/api/v1/tasks", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "1.0", items: [{ task_id: task.task_id, title: task.title, status: task.status, updated_at: task.updated_at, revision: 6 }] }) }));
  await page.route("**/api/v1/tasks/task_board_e2e/work-items/*", async (route) => {
    const workItemId = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1) ?? "");
    const selected = items.find((candidate) => candidate.work_item_id === workItemId);
    await route.fulfill({ status: selected ? 200 : 404, contentType: "application/json", body: JSON.stringify(selected ? { schema_version: "1.0", task, item: selected, refresh_after_ms: 3000, projection_revision: "abc123" } : { error: { message: "WorkItem not found" } }) });
  });
  await page.route("**/api/v1/tasks/task_board_e2e/work-items", async (route) => route.fulfill({ status: 200, contentType: "application/json", headers: { ETag: '"abc123"' }, body: JSON.stringify(projection) }));
  await page.route("**/api/v1/tasks/task_board_e2e/work-items/*/status", async (route) => {
    const segments = new URL(route.request().url()).pathname.split("/");
    const workItemId = decodeURIComponent(segments.at(-2) ?? "");
    const body = route.request().postDataJSON() as { business_status: typeof items[number]["business_status"] };
    const updatedItems = items.map((candidate) => candidate.work_item_id === workItemId
      ? { ...candidate, business_status: body.business_status }
      : candidate);
    const summary = {
      TODO: updatedItems.filter((candidate) => candidate.business_status === "TODO").length,
      RUNNING: updatedItems.filter((candidate) => candidate.business_status === "RUNNING").length,
      WAITING_APPROVAL: updatedItems.filter((candidate) => candidate.business_status === "WAITING_APPROVAL").length,
      COMPLETED: updatedItems.filter((candidate) => candidate.business_status === "COMPLETED").length,
      EXCEPTION: updatedItems.filter((candidate) => candidate.business_status === "EXCEPTION").length,
    };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...projection, task_revision: 7, items: updatedItems, summary, projection_revision: "status-updated" }) });
  });
});

test("shows one stable logical card per WorkItem in four business columns", async ({ page }) => {
  await page.goto("/tasks/task_board_e2e/board");
  await expect(page.getByRole("navigation", { name: "任务工作区" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "待办" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "运行中" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "待审批" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "已完成" })).toBeVisible();

  const runningCard = page.getByRole("link", { name: "KV 方向 A，运行中，进入 图片工作台" });
  await expect(runningCard).toHaveCount(1);
  await expect(runningCard).toContainText("执行2");
  await expect(runningCard).toContainText("重试1");

  await page.getByLabel("创作类型").selectOption("ppt");
  await expect(page.getByRole("heading", { name: "整合演示文稿" })).toBeVisible();
  await expect(page.getByText("KV 方向 A")).toHaveCount(0);
  await page.getByLabel("创作类型").selectOption("all");
  await expect(page.getByRole("heading", { name: "整合演示文稿" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "KV 方向 D" })).toBeVisible();
});

test("moves a card to the selected status column", async ({ page }) => {
  await page.goto("/tasks/task_board_e2e/board");
  await page.getByLabel("设置 KV 方向 B 状态").selectOption("COMPLETED");

  const completedColumn = page.getByRole("region", { name: "已完成" });
  await expect(completedColumn.getByRole("heading", { name: "KV 方向 B" })).toBeVisible();
});

test("opens a refresh-safe task drawer with designer-facing status and attempt history", async ({ page }) => {
  await page.goto("/tasks/task_board_e2e/board");
  await page.locator("article.task-card").filter({ hasText: "KV 方向 A" }).getByRole("link", { name: "详情" }).click();
  await expect(page).toHaveURL(/drawer=work-item&target=work_kv_a/);
  const drawer = page.getByRole("dialog", { name: "工作台详情抽屉" });
  await expect(drawer.getByRole("heading", { name: "子任务详情" })).toBeVisible();
  await expect(drawer).toContainText("当前进度运行中");
  await expect(drawer).toContainText("2 次执行 · 1 次自动重试");
  await expect(drawer).not.toContainText(/work_|instance_|RUNNING/);
  await page.reload();
  await expect(page.getByRole("heading", { name: "子任务详情" })).toBeVisible();
  await page.getByRole("button", { name: "关闭详情抽屉" }).click();
  await expect(page).not.toHaveURL(/drawer=/);
});

test("renders ordered Stage dependencies and a truthful PPT boundary", async ({ page }) => {
  await page.goto("/tasks/task_board_e2e/plan");
  await expect(page.getByRole("heading", { name: "图片 设计阶段" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "PPT 设计阶段" })).toBeVisible();
  await expect(page.getByText("依赖 第 1 阶段 · 图片")).toBeVisible();
  await expect(page.getByText("PPT 能力未接入", { exact: true })).toBeVisible();
  await expect(page.getByText(/不会进入伪工作台/)).toBeVisible();

  for (const width of [1280, 1440, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth === document.documentElement.clientWidth)).toBe(true);
  }
});
