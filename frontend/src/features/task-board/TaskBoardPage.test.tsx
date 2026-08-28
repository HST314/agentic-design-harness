import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, test } from "vitest";
import type { ContractWorkItemProjection } from "../../api/generated-contracts";
import { BoardView, workbenchPath } from "./TaskBoardPage";

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
    // Every card keeps the single-instance data shape.
    expect(markup.match(/<dt>实例<\/dt><dd>1<\/dd>/g)).toHaveLength(3);
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

  test("marks a pending PPT card link for one-click automatic startup", () => {
    const item = pendingPptWorkItem();
    const markup = renderBoard([item]);

    expect(workbenchPath(item)).toBe("/tasks/task_board/work-items/work_2?start=1");
    expect(markup).toContain("/tasks/task_board/work-items/work_2?start=1");
    expect(markup).toContain("进入 PPT 工作台");
  });
});
