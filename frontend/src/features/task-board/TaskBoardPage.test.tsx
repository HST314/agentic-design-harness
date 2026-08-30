import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, test } from "vitest";
import type { ContractWorkItemProjection } from "../../api/generated-contracts";
import {
  BoardView,
  canEnterWorkbench,
  PlanView,
  WorkbenchGateDialog,
  workbenchGateMessage,
  workbenchPath,
} from "./TaskBoardPage";

function runningWorkItem(index: number, title: string): ContractWorkItemProjection {
  return {
    schema_version: "1.0",
    work_item_id: `work_${index}`,
    task_id: "task_board",
    title,
    agent_type: "image",
    required: true,
    depends_on: [],
    stage: {
      stage_id: `stage_${index}`,
      position: index,
      type: "image",
      status: "RUNNING",
      depends_on: [],
      available: true,
    },
    business_status: "RUNNING",
    raw_status: "RUNNING",
    current_instance: {
      instance_id: `instance_${index}`,
      status: "RUNNING",
      approval_mode: "human",
      manual_finished: false,
      process_state: "RUNNING",
      restart_required: false,
      created_at: "2026-08-27T00:00:00Z",
    },
    instance_ids: [`instance_${index}`],
    attempts: [],
    pending_approvals: [],
    delivery_count: 0,
    alerts: [],
    updated_at: "2026-08-27T00:00:00Z",
  };
}

function pendingPptWorkItem(): ContractWorkItemProjection {
  return {
    ...runningWorkItem(2, "学院介绍 PPT"),
    agent_type: "ppt",
    business_status: "TODO",
    raw_status: "READY",
    stage: {
      stage_id: "stage_ppt",
      position: 2,
      type: "ppt",
      status: "READY",
      depends_on: ["stage_image"],
      available: true,
    },
    current_instance: {
      ...runningWorkItem(2, "学院介绍 PPT").current_instance!,
      status: "READY",
      process_state: null,
    },
  };
}

function renderBoard(items: ContractWorkItemProjection[]): string {
  return renderToStaticMarkup(
    <MemoryRouter>
      <BoardView items={items} />
    </MemoryRouter>,
  );
}

describe("BoardView", () => {
  test("renders N stacked cards for N running work items, one instance each", () => {
    const markup = renderBoard([
      runningWorkItem(1, "北工大 A 海报"),
      runningWorkItem(2, "北工大 B 文化墙"),
      runningWorkItem(3, "北工大 C 易拉宝"),
    ]);

    expect(markup.match(/task-card__entry/g)).toHaveLength(3);
    expect(markup).toContain("北工大 A 海报");
    expect(markup).toContain("北工大 B 文化墙");
    expect(markup).toContain("北工大 C 易拉宝");
    // 运行中 column count badge shows all N cards.
    expect(markup).toContain('id="board-column-running">运行中</h2><span>3</span>');
    // Every card keeps the single-execution data shape.
    expect(markup.match(/<dt>执行<\/dt><dd>1<\/dd>/g)).toHaveLength(3);
    // Cards deep-link into their own work item workbench.
    expect(markup).toContain("/tasks/task_board/work-items/work_1");
    expect(markup).toContain("/tasks/task_board/work-items/work_3");
    // Empty columns keep their hint instead of collapsing.
    expect(markup.match(/当前没有子任务/g)).toHaveLength(3);
  });

  test("keeps single-card and multi-card columns equally truthful", () => {
    const markup = renderBoard([runningWorkItem(1, "唯一海报")]);

    expect(markup.match(/task-card__entry/g)).toHaveLength(1);
    expect(markup).toContain('id="board-column-running">运行中</h2><span>1</span>');
    expect(markup.match(/当前没有子任务/g)).toHaveLength(3);
  });

  test("gates an unstarted PPT card behind the not-ready dialog instead of a link", () => {
    const item = pendingPptWorkItem();
    const markup = renderBoard([item]);

    expect(workbenchPath(item)).toBe("/tasks/task_board/work-items/work_2");
    expect(canEnterWorkbench(item)).toBe(false);
    expect(markup).not.toContain('href="/tasks/task_board/work-items/work_2"');
    // The entry renders exactly like an enterable card — same title and facts —
    // but as a button that opens the gate dialog, with no differential hint.
    expect(markup).not.toContain("请从 Master 启动");
    expect(markup).not.toContain("task-card__entry--disabled");
    expect(markup).not.toContain("task-card__entry-hint");
    expect(markup).toContain("学院介绍 PPT");
    expect(markup.match(/<button[^>]*class="task-card__entry"/g)).toHaveLength(1);
    expect(markup).toContain("PPT工作台未就绪");
  });

  test("gates unstarted image and general cards the same way as PPT", () => {
    const image = { ...runningWorkItem(4, "未启动海报") };
    image.current_instance = { ...image.current_instance!, status: "READY" as const, process_state: null };
    const general = {
      ...runningWorkItem(5, "未启动方案"),
      agent_type: "general" as const,
      current_instance: null,
      instance_ids: [],
    };
    const markup = renderBoard([image, general]);

    expect(canEnterWorkbench(image)).toBe(false);
    expect(canEnterWorkbench(general)).toBe(false);
    expect(markup).not.toContain('href="/tasks/task_board/work-items/work_4"');
    expect(markup).not.toContain('href="/tasks/task_board/work-items/work_5"');
    expect(markup).toContain("未启动海报");
    expect(markup).toContain("未启动方案");
    expect(markup.match(/<button[^>]*class="task-card__entry"/g)).toHaveLength(2);
  });

  test("blocks entry while the instance is still starting", () => {
    const item = runningWorkItem(6, "启动中海报");
    item.current_instance!.status = "STARTING";

    expect(canEnterWorkbench(item)).toBe(false);
    expect(renderBoard([item])).not.toContain('href="/tasks/task_board/work-items/work_6"');
  });

  test("opens a PPT workbench after its instance has started", () => {
    const item = pendingPptWorkItem();
    item.business_status = "RUNNING";
    item.current_instance!.status = "RUNNING";
    const markup = renderBoard([item]);

    expect(canEnterWorkbench(item)).toBe(true);
    expect(markup).toContain('href="/tasks/task_board/work-items/work_2"');
  });
});

describe("WorkbenchGateDialog", () => {
  function renderGate(item: ContractWorkItemProjection, open: boolean): string {
    return renderToStaticMarkup(
      <MemoryRouter>
        <WorkbenchGateDialog item={item} open={open} onClose={() => undefined} />
      </MemoryRouter>,
    );
  }

  test("renders nothing while closed", () => {
    expect(renderGate(pendingPptWorkItem(), false)).toBe("");
  });

  test("points unstarted tasks to Master with a launch hint", () => {
    const markup = renderGate(pendingPptWorkItem(), true);

    expect(markup).toContain("工作台未就绪");
    expect(markup).toContain("尚未启动");
    expect(markup).toContain("学院介绍 PPT");
    expect(markup).toContain('href="/tasks/task_board/master"');
    expect(markup).toContain("前往 Master");
    expect(markup).toContain("知道了");
  });

  test("asks for patience while the workbench is starting", () => {
    const item = pendingPptWorkItem();
    item.current_instance!.status = "STARTING";

    expect(workbenchGateMessage(item)).toContain("正在启动");
  });

  test("offers a retry hint after a failed start", () => {
    const item = pendingPptWorkItem();
    item.current_instance!.status = "FAILED_TO_START";

    expect(workbenchGateMessage(item)).toContain("重试启动");
  });
});

function planWorkItem(
  id: string,
  agentType: "general" | "image" | "ppt",
  businessStatus: ContractWorkItemProjection["business_status"],
  updatedAt: string,
): ContractWorkItemProjection {
  return {
    ...runningWorkItem(1, `${agentType} 任务 ${id}`),
    work_item_id: id,
    agent_type: agentType,
    business_status: businessStatus,
    updated_at: updatedAt,
  };
}

function renderPlan(items: ContractWorkItemProjection[], allItems?: ContractWorkItemProjection[]): string {
  return renderToStaticMarkup(
    <MemoryRouter>
      <PlanView items={items} allItems={allItems} />
    </MemoryRouter>,
  );
}

function currentColumnLabel(markup: string): string | null {
  const match = markup.match(/task-board__column--current" aria-labelledby="plan-column-(\w+)"/);
  return match?.[1] ?? null;
}

describe("PlanView", () => {
  test("groups every work item into the Image / PPT / 方案 columns in plan order", () => {
    const markup = renderPlan([
      planWorkItem("img_new", "image", "TODO", "2026-08-28T00:00:00Z"),
      planWorkItem("ppt_1", "ppt", "RUNNING", "2026-08-27T00:00:00Z"),
      planWorkItem("img_old", "image", "COMPLETED", "2026-08-26T00:00:00Z"),
      planWorkItem("gen_1", "general", "TODO", "2026-08-29T00:00:00Z"),
    ]);

    expect(markup).toContain('id="plan-column-image">Image</h2><span>2</span>');
    expect(markup).toContain('id="plan-column-ppt">PPT</h2><span>1</span>');
    expect(markup).toContain('id="plan-column-general">方案</h2><span>1</span>');
    // Cards keep the projection's plan order even when updated_at disagrees,
    // so a status change never reshuffles a column.
    expect(markup.indexOf("image 任务 img_new")).toBeLessThan(markup.indexOf("image 任务 img_old"));
  });

  test("highlights the Image column while any image task is incomplete", () => {
    const markup = renderPlan([
      planWorkItem("img_1", "image", "RUNNING", "2026-08-26T00:00:00Z"),
      planWorkItem("ppt_1", "ppt", "TODO", "2026-08-27T00:00:00Z"),
      planWorkItem("gen_1", "general", "TODO", "2026-08-28T00:00:00Z"),
    ]);

    expect(currentColumnLabel(markup)).toBe("image");
    expect(markup.match(/当前阶段/g)).toHaveLength(1);
  });

  test("highlights the PPT column once every image task is completed", () => {
    const markup = renderPlan([
      planWorkItem("img_1", "image", "COMPLETED", "2026-08-26T00:00:00Z"),
      planWorkItem("ppt_1", "ppt", "RUNNING", "2026-08-27T00:00:00Z"),
      planWorkItem("gen_1", "general", "TODO", "2026-08-28T00:00:00Z"),
    ]);

    expect(currentColumnLabel(markup)).toBe("ppt");
  });

  test("highlights the 方案 column when image and PPT tasks are all completed", () => {
    const markup = renderPlan([
      planWorkItem("img_1", "image", "COMPLETED", "2026-08-26T00:00:00Z"),
      planWorkItem("ppt_1", "ppt", "COMPLETED", "2026-08-27T00:00:00Z"),
      planWorkItem("gen_1", "general", "RUNNING", "2026-08-28T00:00:00Z"),
    ]);

    expect(currentColumnLabel(markup)).toBe("general");
  });

  test("highlights the only populated column even when its tasks are completed", () => {
    const markup = renderPlan([
      planWorkItem("ppt_1", "ppt", "COMPLETED", "2026-08-26T00:00:00Z"),
      planWorkItem("ppt_2", "ppt", "COMPLETED", "2026-08-27T00:00:00Z"),
    ]);

    expect(currentColumnLabel(markup)).toBe("ppt");
    expect(markup.match(/当前没有子任务/g)).toHaveLength(2);
  });

  test("keeps the highlight on the last populated column when everything is completed", () => {
    const markup = renderPlan([
      planWorkItem("img_1", "image", "COMPLETED", "2026-08-26T00:00:00Z"),
      planWorkItem("ppt_1", "ppt", "COMPLETED", "2026-08-27T00:00:00Z"),
      planWorkItem("gen_1", "general", "COMPLETED", "2026-08-28T00:00:00Z"),
    ]);

    expect(currentColumnLabel(markup)).toBe("general");
  });

  test("highlights nothing when the plan has no work items", () => {
    const markup = renderPlan([]);

    expect(currentColumnLabel(markup)).toBeNull();
    expect(markup.match(/当前没有子任务/g)).toHaveLength(3);
  });

  test("derives the current stage from the unfiltered plan, not the visible subset", () => {
    const allItems = [
      planWorkItem("img_1", "image", "RUNNING", "2026-08-26T00:00:00Z"),
      planWorkItem("ppt_1", "ppt", "TODO", "2026-08-27T00:00:00Z"),
      planWorkItem("gen_1", "general", "TODO", "2026-08-28T00:00:00Z"),
    ];
    // Filtering down to only PPT cards must not move the badge to the PPT column.
    const markup = renderPlan(allItems.filter((item) => item.agent_type === "ppt"), allItems);

    expect(currentColumnLabel(markup)).toBe("image");
    expect(markup).toContain('id="plan-column-image">Image</h2><span>0</span>');
    expect(markup).toContain('id="plan-column-ppt">PPT</h2><span>1</span>');
  });
});
