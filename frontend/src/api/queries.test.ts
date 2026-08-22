import { describe, expect, test } from "vitest";
import { visiblePollInterval } from "./queries";

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
