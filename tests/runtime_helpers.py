"""Shared builders for Phase 1 runtime behavior tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.contracts import ContractRegistry
from harness.domain.commands import CommandEnvelope
from harness.domain.service import TaskCommandService
from harness.storage.store import FileStateStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = PROJECT_ROOT / "contracts" / "v1"


def build_store(root: Path) -> FileStateStore:
    contracts = ContractRegistry(CONTRACTS_ROOT)
    return FileStateStore(root / "control-data", root / "workspace", contracts, 0.25)


def build_service(root: Path) -> tuple[FileStateStore, TaskCommandService]:
    store = build_store(root)
    store.start()
    return store, TaskCommandService(store, store.contracts)


def envelope(key: str, expected: int, actor_type: str = "human") -> CommandEnvelope:
    return CommandEnvelope(
        idempotency_key=key,
        actor_type=actor_type,
        actor_id="tester",
        expected_revision=expected,
    )


def image_plan(task_id: str, count: int = 1) -> dict[str, list[dict[str, Any]]]:
    instance_ids = [f"i_image_{index}" for index in range(1, count + 1)]
    return {
        "stages": [stage(task_id, "s_image", "image", 1, [], True, instance_ids)],
        "instances": [instance(task_id, item, "s_image", "image", True) for item in instance_ids],
        "task_cards": [card(task_id, item, "s_image", "image") for item in instance_ids],
    }


def ppt_plan(task_id: str, required: bool = True) -> dict[str, list[dict[str, Any]]]:
    return {
        "stages": [stage(task_id, "s_ppt", "ppt", 1, [], required, ["i_ppt_1"])],
        "instances": [instance(task_id, "i_ppt_1", "s_ppt", "ppt", required)],
        "task_cards": [card(task_id, "i_ppt_1", "s_ppt", "ppt")],
    }


def image_to_ppt_plan(
    task_id: str, ppt_required: bool = True
) -> dict[str, list[dict[str, Any]]]:
    return {
        "stages": [
            stage(task_id, "s_image", "image", 1, [], True, ["i_image_1"]),
            stage(task_id, "s_ppt", "ppt", 2, ["s_image"], ppt_required, ["i_ppt_1"]),
        ],
        "instances": [
            instance(task_id, "i_image_1", "s_image", "image", True),
            instance(task_id, "i_ppt_1", "s_ppt", "ppt", ppt_required),
        ],
        "task_cards": [
            card(task_id, "i_image_1", "s_image", "image"),
            card(task_id, "i_ppt_1", "s_ppt", "ppt"),
        ],
    }


def stage(
    task_id: str,
    stage_id: str,
    agent_type: str,
    position: int,
    depends_on: list[str],
    required: bool,
    instance_ids: list[str],
) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "task_id": task_id,
        "type": agent_type,
        "position": position,
        "depends_on": depends_on,
        "required": required,
        "instance_ids": instance_ids,
    }


def instance(
    task_id: str,
    instance_id: str,
    stage_id: str,
    agent_type: str,
    required: bool,
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "task_id": task_id,
        "stage_id": stage_id,
        "agent_type": agent_type,
        "required": required,
        "approval_mode": "human",
        "config_revision": 1,
        "credential_pair_ref": "cred_test_01",
        "credential_pair_revision": 1,
        "workspace_relpath": f"instances/{instance_id}",
        "task_card_relpath": f"instances/{instance_id}/task-card.json",
    }


def card(task_id: str, instance_id: str, stage_id: str, agent_type: str) -> dict[str, Any]:
    delivery = (
        {
            "kind": "image",
            "role": "final_artwork",
            "required": True,
            "accepted_mime_types": ["image/png"],
        }
        if agent_type == "image"
        else {
            "kind": "presentation",
            "role": "final_presentation",
            "required": True,
            "accepted_mime_types": [
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ],
        }
    )
    return {
        "card_id": f"card_{instance_id}",
        "revision": 1,
        "task_id": task_id,
        "stage_id": stage_id,
        "instance_id": instance_id,
        "agent_type": agent_type,
        "objective": f"Complete {instance_id}",
        "instructions": ["Use only registered inputs."],
        "input_assets": [],
        "expected_deliveries": [delivery],
        "parameters": {"variants": 1} if agent_type == "image" else {"slide_count": 8},
    }


def create_task(
    service: TaskCommandService,
    task_id: str,
    start_policy: str = "manual",
) -> dict[str, Any]:
    return service.create_task(
        task_id=task_id,
        title=f"Task {task_id}",
        goal="Verify the Phase 1 control-plane behavior.",
        master_owner="master_default",
        start_policy=start_policy,
        input_manifest="inputs/manifests/input.json",
        envelope=envelope(f"create-{task_id}", 0),
    )
