import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import { canResolveApprovalInInbox, designerFacingNotification, InboxHeading } from "./InboxPage";

function heading(props: Partial<Parameters<typeof InboxHeading>[0]> = {}): string {
  return renderToStaticMarkup(
    <InboxHeading
      pendingCount={3}
      unreadCount={2}
      readCount={1}
      markingRead={false}
      clearingRead={false}
      onMarkAllRead={() => undefined}
      onClearRead={() => undefined}
      {...props}
    />,
  );
}

describe("InboxHeading", () => {
  test("always renders both bulk actions so they stay resident", () => {
    const markup = heading();

    expect(markup).toContain("一键已读");
    expect(markup).toContain("删除已读");
    expect(markup).toContain('aria-label="删除全部已读消息"');
    expect(markup).toContain("3 待处理");
  });

  test("disables each action only when it has no effect", () => {
    const noUnread = heading({ unreadCount: 0 });
    const markReadDisabled = noUnread.match(
      /<button[^>]*inbox-page__mark-read[^>]*>/,
    )?.[0];
    const clearReadEnabled = noUnread.match(
      /<button[^>]*inbox-page__clear-read[^>]*>/,
    )?.[0];
    expect(markReadDisabled).toContain("disabled");
    expect(clearReadEnabled).not.toContain("disabled");

    const noRead = heading({ readCount: 0 });
    const markReadEnabled = noRead.match(
      /<button[^>]*inbox-page__mark-read[^>]*>/,
    )?.[0];
    const clearReadDisabled = noRead.match(
      /<button[^>]*inbox-page__clear-read[^>]*>/,
    )?.[0];
    expect(markReadEnabled).not.toContain("disabled");
    expect(clearReadDisabled).toContain("disabled");
  });

  test("disables both actions while either mutation is running", () => {
    const marking = heading({ markingRead: true });
    expect(marking).toContain("正在标记…");
    expect(marking.match(/<button[^>]*disabled/g)?.length).toBe(2);

    const clearing = heading({ clearingRead: true });
    expect(clearing).toContain("正在删除…");
    expect(clearing.match(/<button[^>]*disabled/g)?.length).toBe(2);
  });
});

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
