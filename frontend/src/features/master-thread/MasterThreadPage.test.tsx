import { describe, expect, test } from "vitest";
import type { MasterSessionProposal } from "../../api/client";
import type {
  ContractMasterMessage,
  ContractPlanProposal,
} from "../../api/generated-contracts";
import { buildMasterTimeline, startStageLabel } from "./MasterThreadPage";

function message(messageId: string, sequence: number): ContractMasterMessage {
  return {
    schema_version: "1.0",
    message_id: messageId,
    task_id: "task_batch",
    sequence,
    role: "master",
    kind: "plan_proposal",
    content: `内容 ${messageId}`,
    asset_refs: [],
    created_at: "2026-08-27T00:00:00Z",
  };
}

function proposal(
  proposalId: string,
  revision: number,
  messageId: string | null,
): MasterSessionProposal {
  const base: ContractPlanProposal = {
    schema_version: "1.0",
    proposal_id: proposalId,
    task_id: "task_batch",
    revision,
    status: "PENDING_CONFIRMATION",
    stages: [],
    work_items: [],
    execution_cards: [],
    created_at: "2026-08-27T00:00:00Z",
    updated_at: "2026-08-27T00:00:00Z",
    confirmed_at: null,
  };
  return { ...base, message_id: messageId };
}

describe("buildMasterTimeline", () => {
  test("inserts each proposal card group directly after its own message", () => {
    const items = buildMasterTimeline(
      [message("m1", 1), message("m2", 2), message("m3", 3)],
      [proposal("p2", 2, "m2"), proposal("p1", 1, "m1")],
    );

    expect(items.map((item) => (item.kind === "message" ? item.message.message_id : item.proposal.proposal_id))).toEqual([
      "m1",
      "p1",
      "m2",
      "p2",
      "m3",
    ]);
  });

  test("keeps every plan version in the flow and appends orphan proposals at the end", () => {
    const items = buildMasterTimeline(
      [message("m1", 1)],
      [proposal("p1", 1, "m1"), proposal("p2", 2, "missing_message"), proposal("p3", 3, null)],
    );

    expect(items.map((item) => (item.kind === "message" ? item.message.message_id : item.proposal.proposal_id))).toEqual([
      "m1",
      "p1",
      "p2",
      "p3",
    ]);
  });

  test("returns only proposals when the thread has no messages yet", () => {
    const items = buildMasterTimeline([], [proposal("p1", 1, null)]);

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "proposal" });
  });
});

describe("startStageLabel", () => {
  test("maps backend start states to the Chinese live status labels", () => {
    const base = {
      attempt: 1,
      launch_id: null,
      side_effect_stage: "NONE",
      last_error: null,
      updated_at: "2026-08-27T00:00:00Z",
    };
    expect(startStageLabel(null)).toBe("已受理");
    expect(startStageLabel({ ...base, state: "PENDING" })).toBe("已受理");
    expect(startStageLabel({ ...base, state: "PREPARING" })).toBe("准备运行环境");
    expect(startStageLabel({ ...base, state: "PROCESS_STARTING" })).toBe("启动进程");
    expect(startStageLabel({ ...base, state: "AGENT_STARTING" })).toBe("健康检查");
    expect(startStageLabel({ ...base, state: "RUNNING" })).toBe("已就绪");
  });
});
