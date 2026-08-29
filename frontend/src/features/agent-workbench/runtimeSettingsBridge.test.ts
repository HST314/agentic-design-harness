import { describe, expect, test } from "vitest";
import {
  bridgeIdempotencyKey,
  isBridgeHello,
  parseBridgeRequest,
  RUNTIME_SETTINGS_BRIDGE_PROTOCOL,
  RUNTIME_SETTINGS_BRIDGE_VERSION,
} from "./runtimeSettingsBridge";

const base = {
  protocol: RUNTIME_SETTINGS_BRIDGE_PROTOCOL,
  version: RUNTIME_SETTINGS_BRIDGE_VERSION,
  type: "bridge.request",
  instance_id: "instance_image",
  request_id: "bridge_request_12345678",
  nonce: "nonce_current_123456789",
};

describe("runtime settings bridge protocol", () => {
  test("accepts only the current instance, protocol, nonce and safe proposal shape", () => {
    const request = {
      ...base,
      action: "runtime_settings.propose",
      payload: {
        base_revision: 3,
        overrides: {
          category_constraint: { release: "manual" },
          style_direction: { release: "off" },
          watermark: true,
        },
        sync_unstarted_image_work_items: true,
        expected_sync_instance_ids: ["instance_other"],
      },
    };
    expect(parseBridgeRequest(request, "instance_image", base.nonce)).toEqual(request);
    expect(parseBridgeRequest({ ...request, nonce: "replayed_nonce" }, "instance_image", base.nonce)).toBeNull();
    expect(parseBridgeRequest({ ...request, instance_id: "instance_other" }, "instance_image", base.nonce)).toBeNull();
    expect(parseBridgeRequest({ ...request, payload: { ...request.payload, adapter_key: "secret" } }, "instance_image", base.nonce)).toBeNull();
    expect(parseBridgeRequest({
      ...request,
      payload: { ...request.payload, overrides: { adapter_key: "secret" } },
    }, "instance_image", base.nonce)).toBeNull();
    expect(parseBridgeRequest({
      ...request,
      payload: {
        ...request.payload,
        overrides: { category_constraint: { release: "sometimes" } },
      },
    }, "instance_image", base.nonce)).toBeNull();
    expect(parseBridgeRequest({
      ...request,
      payload: { ...request.payload, overrides: { candidate_concurrency: 99 } },
    }, "instance_image", base.nonce)).toBeNull();
  });

  test("allows a nonce-free hello only for the exact current instance", () => {
    const hello = {
      protocol: RUNTIME_SETTINGS_BRIDGE_PROTOCOL,
      version: RUNTIME_SETTINGS_BRIDGE_VERSION,
      type: "bridge.hello",
      instance_id: "instance_image",
    };
    expect(isBridgeHello(hello, "instance_image")).toBe(true);
    expect(isBridgeHello({ ...hello, instance_id: "instance_other" }, "instance_image")).toBe(false);
  });

  test("derives bounded stable idempotency keys from one bridge request", () => {
    const key = bridgeIdempotencyKey("runtime_settings.confirm", "bridge_request_12345678");
    expect(key).toBe("workbench_confirm_bridge_request_12345678");
    expect(key.length).toBeLessThanOrEqual(128);
  });

  test("accepts a strict sync toggle payload and rejects malformed ones", () => {
    const request = {
      ...base,
      action: "runtime_settings.sync_toggle",
      payload: { sync_to_peers: true },
    };
    expect(parseBridgeRequest(request, "instance_image", base.nonce)).toEqual(request);
    expect(parseBridgeRequest({
      ...request,
      payload: { sync_to_peers: false },
    }, "instance_image", base.nonce)).toEqual({ ...request, payload: { sync_to_peers: false } });
    // Non-boolean flags, extra keys and empty payloads are all refused.
    expect(parseBridgeRequest({
      ...request,
      payload: { sync_to_peers: "yes" },
    }, "instance_image", base.nonce)).toBeNull();
    expect(parseBridgeRequest({
      ...request,
      payload: { sync_to_peers: true, adapter_key: "secret" },
    }, "instance_image", base.nonce)).toBeNull();
    expect(parseBridgeRequest({ ...request, payload: {} }, "instance_image", base.nonce)).toBeNull();
  });

  test("accepts only an exact delivery completion bundle identifier", () => {
    const request = {
      ...base,
      action: "delivery.complete",
      payload: { bundle_id: "bundle_0123456789abcdef" },
    };
    expect(parseBridgeRequest(request, "instance_image", base.nonce)).toEqual(request);
    expect(parseBridgeRequest({
      ...request,
      payload: { bundle_id: "../shared", semantic_hint: "publish" },
    }, "instance_image", base.nonce)).toBeNull();
    expect(bridgeIdempotencyKey("delivery.complete", base.request_id)).toBe(
      "workbench_complete_bridge_request_12345678",
    );
  });
});
