import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import { FocusTabLink, workbenchFocusPath } from "./AgentWorkbenchPage";

describe("workbenchFocusPath", () => {
  test("builds the fullscreen focus route for a work item", () => {
    expect(workbenchFocusPath("task_1", "work_1")).toBe("/tasks/task_1/work-items/work_1/focus");
  });

  test("encodes identifiers that contain route-unsafe characters", () => {
    expect(workbenchFocusPath("task/1", "work 1")).toBe("/tasks/task%2F1/work-items/work%201/focus");
  });
});

describe("FocusTabLink", () => {
  test("opens the focus workbench in a new browser tab", () => {
    const markup = renderToStaticMarkup(<FocusTabLink taskId="task_1" workItemId="work_1" />);

    expect(markup).toContain('class="workbench-task-tabs__focus"');
    expect(markup).toContain('href="/tasks/task_1/work-items/work_1/focus"');
    expect(markup).toContain('target="_blank"');
    expect(markup).toContain('rel="noopener noreferrer"');
    expect(markup).toContain('aria-label="在新标签页打开全屏工作台"');
    expect(markup).toContain("新标签页");
  });
});
