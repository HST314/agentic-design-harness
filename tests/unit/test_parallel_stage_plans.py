"""Same-type parallel stage topology: draft materialization and proposal gates."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from harness.contracts import ContractRegistry
from harness.core.errors import HarnessError
from harness.domain.master import materialize_plan_proposal, validate_plan_proposal
from harness.services.plan_drafts import (
    PlanDraftValidationError,
    master_response_schema,
    materialize_plan_draft,
    validate_master_response,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_ROOT = ROOT / "contracts" / "v1"
TASK_ID = "t_parallel"
CREATED_AT = "2026-08-27T00:00:00Z"


def image_stage_draft(title: str) -> dict[str, Any]:
    return {
        "type": "image",
        "title": title,
        "required": True,
        "objective": f"Create {title}.",
        "instructions": ["Use the written requirements."],
        "input_asset_ids": [],
        "expected_deliveries": [
            {
                "kind": "image",
                "role": "key_visual",
                "required": True,
                "accepted_mime_types": ["image/png"],
            }
        ],
        "parameters": {
            "aspect_ratio": None,
            "variants": 1,
            "usage_context": "Campaign",
            "category_id": None,
            "category_version": None,
        },
    }


def ppt_stage_draft(title: str) -> dict[str, Any]:
    return {
        "type": "ppt",
        "title": title,
        "required": True,
        "objective": f"Compose {title}.",
        "instructions": ["Use the generated key visual."],
        "input_asset_ids": [],
        "expected_deliveries": [
            {
                "kind": "presentation",
                "role": "final_presentation",
                "required": True,
                "accepted_mime_types": [
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                ],
            }
        ],
        "parameters": {"slide_count": 8, "planned_asset_role": None},
    }


def model_output(stages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "PLAN_READY",
        "message": "The plan is ready for review.",
        "task_title": "Parallel campaign",
        "plan_draft": {"stages": stages},
    }


class ParallelStageMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contracts = ContractRegistry(CONTRACTS_ROOT)

    def _materialize(self, stages: list[dict[str, Any]]) -> dict[str, Any]:
        output = model_output(stages)
        validate_master_response(master_response_schema([]), output)
        return materialize_plan_draft(
            TASK_ID,
            1,
            output["plan_draft"],
            created_at=CREATED_AT,
            asset_ids=set(),
        )

    def test_dual_parallel_image_stages_materialize_distinct_objects(self) -> None:
        proposal = self._materialize(
            [image_stage_draft("Poster"), image_stage_draft("Culture wall")]
        )

        self.assertEqual(len(proposal["stages"]), 2)
        self.assertEqual(len(proposal["work_items"]), 2)
        self.assertEqual(len(proposal["execution_cards"]), 2)
        for stage in proposal["stages"]:
            self.assertEqual(stage["depends_on"], [])
        for item in proposal["work_items"]:
            self.assertEqual(item["depends_on"], [])
        identifier_groups = [
            [stage["stage_id"] for stage in proposal["stages"]],
            [item["work_item_id"] for item in proposal["work_items"]],
            [item["current_instance_id"] for item in proposal["work_items"]],
            [card["card_id"] for card in proposal["execution_cards"]],
        ]
        for identifiers in identifier_groups:
            self.assertEqual(len(set(identifiers)), 2, identifiers)

        validate_plan_proposal(
            self.contracts, proposal, task_id=TASK_ID, expected_revision=1
        )

        stages, instances, cards = materialize_plan_proposal(proposal)
        self.assertEqual(len(instances), 2)
        self.assertEqual(len(cards), 2)
        self.assertEqual(len({item["instance_id"] for item in instances}), 2)
        for stage in stages:
            self.assertEqual(len(stage["instance_ids"]), 1)
            self.assertEqual(stage["depends_on"], [])

    def test_image_to_ppt_still_chains_across_types(self) -> None:
        proposal = self._materialize(
            [image_stage_draft("Poster"), ppt_stage_draft("Deck")]
        )

        image_stage, ppt_stage = proposal["stages"]
        self.assertEqual(image_stage["depends_on"], [])
        self.assertEqual(ppt_stage["depends_on"], [image_stage["stage_id"]])
        image_item, ppt_item = proposal["work_items"]
        self.assertEqual(image_item["depends_on"], [])
        self.assertEqual(ppt_item["depends_on"], [image_item["work_item_id"]])

        validate_plan_proposal(
            self.contracts, proposal, task_id=TASK_ID, expected_revision=1
        )

    def test_same_type_dependency_is_rejected_at_the_proposal_gate(self) -> None:
        proposal = self._materialize(
            [image_stage_draft("Poster"), image_stage_draft("Culture wall")]
        )
        first, second = proposal["stages"]
        second["depends_on"] = [first["stage_id"]]

        with self.assertRaises(HarnessError) as captured:
            validate_plan_proposal(
                self.contracts, proposal, task_id=TASK_ID, expected_revision=1
            )
        self.assertEqual(captured.exception.code, "VALIDATION_ERROR")

    def test_unknown_stage_type_is_rejected_by_the_builder(self) -> None:
        draft = {"stages": [{**image_stage_draft("Poster"), "type": "video"}]}

        with self.assertRaises(PlanDraftValidationError) as captured:
            materialize_plan_draft(
                TASK_ID,
                1,
                draft,
                created_at=CREATED_AT,
                asset_ids=set(),
            )
        self.assertEqual(captured.exception.reason, "unsupported_topology")


if __name__ == "__main__":
    unittest.main()
