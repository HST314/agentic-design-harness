import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import type { ContractPlanProposal } from "../../api/generated-contracts";
import { ConfirmDialog } from "./MasterThreadPage";

function proposalWithCards(titles: string[]): ContractPlanProposal {
  const stages = titles.map((_, index) => ({
    stage_id: `stage_${index + 1}`,
    type: "image" as const,
    position: index + 1,
    depends_on: [],
    required: true,
  }));
  return {
    schema_version: "1.0",
    proposal_id: "proposal_batch",
    task_id: "task_batch",
    revision: 1,
    status: "PENDING_CONFIRMATION",
    stages,
    work_items: titles.map((title, index) => ({
      schema_version: "1.0",
      work_item_id: `work_${index + 1}`,
      task_id: "task_batch",
      stage_id: `stage_${index + 1}`,
      title,
      agent_type: "image" as const,
      required: true,
      depends_on: [],
      current_instance_id: `instance_${index + 1}`,
      instance_ids: [`instance_${index + 1}`],
      task_card_ids: [`card_${index + 1}`],
    })),
    execution_cards: titles.map((_, index) => ({
      schema_version: "1.1",
      card_id: `card_${index + 1}`,
      revision: 1,
      task_id: "task_batch",
      stage_id: `stage_${index + 1}`,
      instance_id: `instance_${index + 1}`,
      agent_type: "image" as const,
      objective: `Create ${titles[index]}.`,
      instructions: ["Use the written requirements."],
      input_assets: [],
      expected_deliveries: [
        {
          kind: "image" as const,
          role: "key_visual",
          required: true,
          accepted_mime_types: ["image/png"],
        },
      ],
      parameters: { variants: 1 },
      created_at: "2026-08-27T00:00:00Z",
    })),
    created_at: "2026-08-27T00:00:00Z",
    updated_at: "2026-08-27T00:00:00Z",
    confirmed_at: null,
  };
}

function renderDialog(proposal: ContractPlanProposal): string {
  return renderToStaticMarkup(
    <ConfirmDialog
      proposal={proposal}
      taskRevision={2}
      open={false}
      pending={false}
      error={null}
      onCancel={() => undefined}
      onConfirm={() => undefined}
    />,
  );
}

describe("ConfirmDialog", () => {
  test("lists N cards as an all-checked checkbox list with one batch start button", () => {
    const markup = renderDialog(proposalWithCards(["北工大 A 海报", "北工大 B 文化墙"]));

    expect(markup.match(/type="checkbox"/g)).toHaveLength(2);
    expect(markup.match(/checked=""/g)).toHaveLength(2);
    expect(markup).toContain("北工大 A 海报");
    expect(markup).toContain("北工大 B 文化墙");
    expect(markup).toContain("批量启动");
    expect(markup).not.toContain("批量启动需勾选全部任务卡");
  });

  test("keeps the single-card confirm label for one-card plans", () => {
    const markup = renderDialog(proposalWithCards(["北工大 A 海报"]));

    expect(markup.match(/type="checkbox"/g)).toHaveLength(1);
    expect(markup).toContain("确认并启动");
    expect(markup).not.toContain("批量启动");
  });
});
