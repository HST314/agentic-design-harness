import { describe, expect, test } from "vitest";
import { canResolveApprovalInInbox, designerFacingNotification } from "./InboxPage";

describe("designerFacingNotification", () => {
  test("rephrases assistant names and removes internal identifiers", () => {
    const value = designerFacingNotification(
      "Image Agent 的实例 instance_a1b2c3 正在 approve_taskbook，分支 branch_1234567890 等待处理。",
    );

    expect(value).toContain("图片助手");
    expect(value).toContain("该子任务");
    expect(value).toContain("设计分支");
    expect(value).not.toMatch(/Agent|instance_|approve_taskbook|branch_/);
  });
});

describe("canResolveApprovalInInbox", () => {
  test("keeps parameter-free decisions in the inbox", () => {
    expect(canResolveApprovalInInbox("approve_taskbook")).toBe(true);
    expect(canResolveApprovalInInbox("publish_bundle")).toBe(true);
  });

  test("routes decisions that need designer input to the professional workbench", () => {
    expect(canResolveApprovalInInbox("select_master")).toBe(false);
    expect(canResolveApprovalInInbox("answer_clarification")).toBe(false);
  });
});
