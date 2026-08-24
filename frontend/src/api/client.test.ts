import { afterEach, describe, expect, test, vi } from "vitest";
import { ApiClient } from "./client";

function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ApiClient", () => {
  test("encodes every task and work-item path segment for the workbench link", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ schema_version: "1.0" }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient("https://control.example");

    await client.instanceUiLink("task/秋季", "work item/?", "instance/#1");

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe(
      "https://control.example/api/v1/instances/instance%2F%231/ui-link?"
      + "task_id=task%2F%E7%A7%8B%E5%AD%A3&work_item_id=work+item%2F%3F",
    );
    expect(init).toMatchObject({ headers: { Accept: "application/json" } });
  });

  test("preserves revision and actor envelopes on presentation writes", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ schema_version: "1.0" }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient();

    await client.updateTaskPresentation("task_1", {
      title: "已确认标题",
      envelope: {
        idempotency_key: "presentation_once",
        actor_type: "human",
        actor_id: "operator",
        expected_revision: 7,
      },
    });

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(init?.method).toBe("PATCH");
    expect(JSON.parse(String(init?.body))).toEqual({
      title: "已确认标题",
      envelope: {
        idempotency_key: "presentation_once",
        actor_type: "human",
        actor_id: "operator",
        expected_revision: 7,
      },
    });
  });

  test("exposes stable API error codes and conflict details to recovery UI", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      error: {
        code: "REVISION_CONFLICT",
        message: "状态已更新，请重新读取。",
        details: { current_revision: 8 },
      },
    }, 409));
    vi.stubGlobal("fetch", fetchMock);

    const request = new ApiClient().taskIntake("task_conflict");
    await expect(request).rejects.toEqual(expect.objectContaining({
      message: "状态已更新，请重新读取。",
      status: 409,
      code: "REVISION_CONFLICT",
      details: { current_revision: 8 },
    }));
  });

  test("renders adapter validation details as a designer-facing Chinese error", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      error: {
        code: "VALIDATION_ERROR",
        message: "The Agent adapter rejected its task card.",
        details: {
          errors: ["Image TaskCard 1.1 requires parameters.usage_context."],
        },
      },
    }, 422));
    vi.stubGlobal("fetch", fetchMock);

    await expect(new ApiClient().masterSession("task_invalid")).rejects.toMatchObject({
      message: "任务卡未通过智能体校验：图片任务卡缺少使用场景。",
      code: "VALIDATION_ERROR",
      details: {
        errors: ["Image TaskCard 1.1 requires parameters.usage_context."],
      },
    });
  });

  test("keeps deployment details out of designer-facing API errors", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      error: {
        code: "MODEL_PROVIDER_UNAVAILABLE",
        message: "Provider endpoint and API Key are not configured in runtime.yaml.",
        details: { request_id: "request_safe" },
      },
    }, 503));
    vi.stubGlobal("fetch", fetchMock);

    await expect(new ApiClient().masterSession("task_1")).rejects.toMatchObject({
      message: "智能创作服务暂时不可用，请稍后重试；当前任务内容已保留。如持续失败，请联系支持人员。",
      status: 503,
      code: "MODEL_PROVIDER_UNAVAILABLE",
      details: { request_id: "request_safe" },
    });
  });

  test("normalizes infrastructure error codes even when their message is generic", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      error: {
        code: "ADAPTER_UNAVAILABLE",
        message: "The requested workspace is unavailable.",
      },
    }, 503));
    vi.stubGlobal("fetch", fetchMock);

    await expect(new ApiClient().instance("instance_1")).rejects.toMatchObject({
      message: "智能创作服务暂时不可用，请稍后重试；当前任务内容已保留。如持续失败，请联系支持人员。",
      code: "ADAPTER_UNAVAILABLE",
    });
  });

  test("falls back to the HTTP status when an intermediary returns non-JSON", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response("upstream failed", {
      status: 502,
      headers: { "Content-Type": "text/plain" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(new ApiClient().readiness()).rejects.toMatchObject({
      message: "请求失败（502）。",
      status: 502,
    });
  });
});
