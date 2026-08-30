import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../../api/queries";
import { executeBridgeRequest } from "./AgentWorkbenchPage";
import {
  RUNTIME_SETTINGS_BRIDGE_PROTOCOL,
  RUNTIME_SETTINGS_BRIDGE_VERSION,
  type RuntimeSettingsBridgeRequest,
} from "./runtimeSettingsBridge";

vi.mock("../../api/queries", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/queries")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      deliveryBundles: vi.fn(),
      resolveApproval: vi.fn(),
    },
  };
});

function bridgeRequest(
  action: RuntimeSettingsBridgeRequest["action"],
  payload: Record<string, unknown>,
): RuntimeSettingsBridgeRequest {
  return {
    protocol: RUNTIME_SETTINGS_BRIDGE_PROTOCOL,
    version: RUNTIME_SETTINGS_BRIDGE_VERSION,
    type: "bridge.request",
    instance_id: "instance_image",
    request_id: "bridge_request_status01",
    nonce: "nonce",
    action,
    payload,
  };
}

const SCOPE = { taskId: "task_1", workItemId: "work_1" };

describe("executeBridgeRequest delivery.status", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test("returns the publish state of the candidate owned by the instance", async () => {
    vi.mocked(api.deliveryBundles).mockResolvedValue({
      candidates: [
        { bundle_id: "bundle_a", instance_id: "instance_image", status: "PUBLISHED" },
        { bundle_id: "bundle_a", instance_id: "instance_other", status: "PENDING_CONFIRMATION" },
      ],
      reviews: [],
    } as never);

    const result = await executeBridgeRequest(
      bridgeRequest("delivery.status", { bundle_id: "bundle_a" }),
      "instance_image",
      undefined,
      SCOPE,
    );

    expect(api.deliveryBundles).toHaveBeenCalledWith("task_1");
    expect(result).toEqual({ bundle_id: "bundle_a", status: "PUBLISHED" });
  });

  test("reports UNKNOWN when the candidate has not been reconciled yet", async () => {
    vi.mocked(api.deliveryBundles).mockResolvedValue({
      candidates: [],
      reviews: [],
    } as never);

    const result = await executeBridgeRequest(
      bridgeRequest("delivery.status", { bundle_id: "bundle_a" }),
      "instance_image",
      undefined,
      SCOPE,
    );

    expect(result).toEqual({ bundle_id: "bundle_a", status: "UNKNOWN" });
  });
});

describe("executeBridgeRequest delivery.complete", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  test("returns immediately when the bundle is already published", async () => {
    vi.mocked(api.deliveryBundles).mockResolvedValue({
      candidates: [
        { bundle_id: "bundle_a", instance_id: "instance_image", status: "PUBLISHED" },
      ],
      reviews: [],
    } as never);

    const result = await executeBridgeRequest(
      bridgeRequest("delivery.complete", { bundle_id: "bundle_a" }),
      "instance_image",
      undefined,
      SCOPE,
    );

    expect(result).toEqual({ bundle_id: "bundle_a", status: "PUBLISHED" });
    expect(api.resolveApproval).not.toHaveBeenCalled();
  });

  test("stops polling at the deadline instead of outrunning the bridge timeout", async () => {
    vi.stubGlobal("window", globalThis);
    vi.useFakeTimers();
    vi.mocked(api.deliveryBundles).mockResolvedValue({
      candidates: [],
      reviews: [],
    } as never);

    const pending = executeBridgeRequest(
      bridgeRequest("delivery.complete", { bundle_id: "bundle_a" }),
      "instance_image",
      undefined,
      SCOPE,
    );
    const assertion = expect(pending).rejects.toThrow("主系统尚未完成交付候选对账");
    await vi.advanceTimersByTimeAsync(26_000);
    await assertion;
  });

  test("aborts a slow in-flight read at the deadline instead of hanging past the bridge timeout", async () => {
    vi.stubGlobal("window", globalThis);
    vi.useFakeTimers();
    let aborted = false;
    // Every read hangs until its abort signal fires: without deadline-bound
    // cancellation the loop would stall here well past the 30s bridge timeout.
    vi.mocked(api.deliveryBundles).mockImplementation(
      (_taskId: string, signal?: AbortSignal) => new Promise((_, reject) => {
        signal?.addEventListener("abort", () => {
          aborted = true;
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      }) as never,
    );

    const pending = executeBridgeRequest(
      bridgeRequest("delivery.complete", { bundle_id: "bundle_a" }),
      "instance_image",
      undefined,
      SCOPE,
    );
    const assertion = expect(pending).rejects.toThrow("主系统尚未完成交付候选对账");
    await vi.advanceTimersByTimeAsync(26_000);
    await assertion;
    expect(aborted).toBe(true);
  });
});
