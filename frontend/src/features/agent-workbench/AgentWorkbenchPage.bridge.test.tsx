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
      deliveryBundleStatus: vi.fn(),
      completeDeliveryBundle: vi.fn(),
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
    vi.mocked(api.deliveryBundleStatus).mockResolvedValue({
      bundle_id: "bundle_a",
      instance_id: "instance_image",
      status: "PUBLISHED",
    } as never);

    const result = await executeBridgeRequest(
      bridgeRequest("delivery.status", { bundle_id: "bundle_a" }),
      "instance_image",
      undefined,
      SCOPE,
    );

    expect(api.deliveryBundleStatus).toHaveBeenCalledWith(
      "task_1",
      "instance_image",
      "bundle_a",
    );
    expect(result).toEqual({ bundle_id: "bundle_a", status: "PUBLISHED" });
  });

  test("reports UNKNOWN when the candidate has not been reconciled yet", async () => {
    vi.mocked(api.deliveryBundleStatus).mockResolvedValue({
      bundle_id: "bundle_a",
      instance_id: "instance_image",
      status: "UNKNOWN",
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

  test("completes the bundle with one idempotent command", async () => {
    vi.mocked(api.completeDeliveryBundle).mockResolvedValue({
      bundle_id: "bundle_a",
      instance_id: "instance_image",
      status: "PUBLISHED",
    } as never);

    const result = await executeBridgeRequest(
      bridgeRequest("delivery.complete", { bundle_id: "bundle_a" }),
      "instance_image",
      7,
      SCOPE,
    );

    expect(result).toEqual({ bundle_id: "bundle_a", status: "PUBLISHED" });
    expect(api.completeDeliveryBundle).toHaveBeenCalledTimes(1);
    expect(api.completeDeliveryBundle).toHaveBeenCalledWith(
      "task_1",
      "bundle_a",
      {
        instance_id: "instance_image",
        operation_id: "workbench_complete_cbf88e5f8ca79dd3",
        envelope: {
          idempotency_key: "workbench_complete_cbf88e5f8ca79dd3",
          actor_type: "human",
          actor_id: "human_operator",
          expected_revision: 7,
        },
      },
      expect.any(AbortSignal),
    );
  });

  test("reuses one completion operation across bridge retries for the same bundle", async () => {
    vi.mocked(api.completeDeliveryBundle).mockResolvedValue({
      bundle_id: "bundle_a",
      instance_id: "instance_image",
      status: "PUBLISHED",
    } as never);

    await executeBridgeRequest(
      bridgeRequest("delivery.complete", { bundle_id: "bundle_a" }),
      "instance_image",
      7,
      SCOPE,
    );
    await executeBridgeRequest(
      {
        ...bridgeRequest("delivery.complete", { bundle_id: "bundle_a" }),
        request_id: "bridge_request_retry02",
      },
      "instance_image",
      8,
      SCOPE,
    );

    const operations = vi.mocked(api.completeDeliveryBundle).mock.calls.map(
      (call) => call[2].operation_id,
    );
    expect(operations).toEqual([
      "workbench_complete_cbf88e5f8ca79dd3",
      "workbench_complete_cbf88e5f8ca79dd3",
    ]);
  });

  test("aborts a slow completion command before the child bridge timeout", async () => {
    vi.stubGlobal("window", globalThis);
    vi.useFakeTimers();
    let aborted = false;
    vi.mocked(api.completeDeliveryBundle).mockImplementation(
      (_taskId, _bundleId, _body, signal?: AbortSignal) => new Promise((_, reject) => {
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
    const assertion = expect(pending).rejects.toThrow("交付确认超时");
    await vi.advanceTimersByTimeAsync(26_000);
    await assertion;
    expect(aborted).toBe(true);
  });
});
