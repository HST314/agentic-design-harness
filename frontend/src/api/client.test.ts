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
