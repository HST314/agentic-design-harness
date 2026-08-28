import { describe, expect, test } from "vitest";
import { pptAutoLaunchAction } from "./AgentWorkbenchPage";

describe("pptAutoLaunchAction", () => {
  test("starts a ready PPT instance entered from the pending board card", () => {
    expect(pptAutoLaunchAction({
      requested: true,
      canStart: true,
      linkStatus: "NO_UI_URL",
      retryAllowed: false,
    })).toBe("start");
  });

  test("recovers a retryable prior startup without a second user click", () => {
    expect(pptAutoLaunchAction({
      requested: true,
      canStart: false,
      linkStatus: "START_FAILED",
      retryAllowed: true,
    })).toBe("recover");
  });

  test("does not launch without the board startup intent", () => {
    expect(pptAutoLaunchAction({
      requested: false,
      canStart: true,
      linkStatus: "NO_UI_URL",
      retryAllowed: false,
    })).toBeNull();
  });
});
