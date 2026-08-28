import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, test, vi } from "vitest";
import { api, approvalDetailQuery, inboxQuery, visiblePollInterval } from "./queries";

afterEach(() => vi.restoreAllMocks());

describe("visiblePollInterval", () => {
  test("uses the server projection cadence while the workbench is visible", () => {
    expect(visiblePollInterval(3_000, "visible")).toBe(3_000);
    expect(visiblePollInterval(5_000, "visible")).toBe(5_000);
    expect(visiblePollInterval(undefined, "visible")).toBe(5_000);
  });

  test("pauses high-frequency polling in a hidden tab", () => {
    expect(visiblePollInterval(3_000, "hidden")).toBe(false);
    expect(visiblePollInterval(5_000, "hidden")).toBe(false);
  });
});

describe("inbox queries", () => {
  test("polls the inbox with one list request and loads approval details only on demand", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const inbox = vi.spyOn(api, "inbox").mockResolvedValue({
      schema_version: "1.0",
      items: [{ inbox_id: "inbox_1", approval_id: "approval_1" }],
    } as unknown as Awaited<ReturnType<typeof api.inbox>>);
    const approval = vi.spyOn(api, "approval").mockResolvedValue({
      approval: { approval_id: "approval_1" },
    } as unknown as Awaited<ReturnType<typeof api.approval>>);

    await queryClient.fetchQuery(inboxQuery);
    expect(inbox).toHaveBeenCalledTimes(1);
    expect(approval).not.toHaveBeenCalled();

    await queryClient.fetchQuery(approvalDetailQuery("approval_1"));
    expect(approval).toHaveBeenCalledTimes(1);
  });
});
