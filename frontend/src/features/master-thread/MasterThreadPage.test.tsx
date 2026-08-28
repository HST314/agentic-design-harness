import { describe, expect, test } from "vitest";
import type { MasterSessionProposal } from "../../api/client";
import type {
  ContractMasterMessage,
  ContractPlanProposal,
} from "../../api/generated-contracts";
import {
  buildMasterTimeline,
  designerFacingMessage,
  isPptLaunchBlocked,
  startStageLabel,
} from "./MasterThreadPage";

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

describe("designerFacingMessage", () => {
  test("removes revisions and internal identifiers from system copy", () => {
    const input = message("m4", 4);
    input.role = "system";
    input.content = "PlanProposal · r2：任务卡 card_direction_a 已保存为 r3，计划已更新为 r2，实例 instance_abc123 正在运行。";

    const value = designerFacingMessage(input);

    expect(value).toContain("执行计划");
    expect(value).toContain("该子任务");
    expect(value).not.toMatch(/PlanProposal|card_|instance_|\br\d+\b/);
  });

  test("keeps the designer's own message unchanged", () => {
    const input = message("m5", 5);
    input.role = "user";
    input.kind = "text";
    input.content = "请保留画面里的 r2 标记。";
    expect(designerFacingMessage(input)).toBe(input.content);
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

describe("isPptLaunchBlocked", () => {
  const card = (agentType: "image" | "ppt"): ContractPlanProposal["execution_cards"][number] => ({
    schema_version: "1.1",
    card_id: `card_${agentType}`,
    revision: 1,
    task_id: "task_batch",
    stage_id: `stage_${agentType}`,
    instance_id: `instance_${agentType}`,
    agent_type: agentType,
    objective: "完成设计任务。",
    instructions: ["遵循已确认要求。"],
    input_assets: [],
    expected_deliveries: [{
      kind: agentType === "ppt" ? "presentation" : "image",
      role: "primary",
      required: true,
      accepted_mime_types: [agentType === "ppt" ? "application/vnd.openxmlformats-officedocument.presentationml.presentation" : "image/png"],
    }],
    parameters: {},
    created_at: "2026-08-28T00:00:00Z",
  });

  test("blocks PPT while any planned image instance is not manually finished", () => {
    expect(isPptLaunchBlocked(card("ppt"), ["instance_image"])).toBe(true);
  });

  test("unblocks PPT after all image instances are manually finished", () => {
    expect(isPptLaunchBlocked(card("ppt"), [])).toBe(false);
  });

  test("does not block image launches on the PPT gate", () => {
    expect(isPptLaunchBlocked(card("image"), ["instance_image"])).toBe(false);
  });
});
