"""Cross-object validation and execution projection for Master plans."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, NoReturn

from ..contracts import ContractRegistry
from ..core.errors import HarnessError


def validate_plan_proposal(
    contracts: ContractRegistry,
    proposal: dict[str, Any],
    *,
    task_id: str,
    expected_revision: int,
) -> None:
    """Validate orchestrator output before it becomes a durable planning fact."""

    contracts.validate("plan-proposal", proposal)
    if proposal["task_id"] != task_id:
        _invalid("The Master plan belongs to another task.")
    if proposal["revision"] != expected_revision:
        _invalid("The Master plan revision is not the next durable revision.")
    if proposal["status"] != "PENDING_CONFIRMATION" or proposal["confirmed_at"] is not None:
        _invalid("A new Master plan must be pending confirmation.")

    stages = sorted(proposal["stages"], key=lambda item: item["position"])
    stage_ids = [item["stage_id"] for item in stages]
    if len(stage_ids) != len(set(stage_ids)) or [item["position"] for item in stages] != list(
        range(1, len(stages) + 1)
    ):
        _invalid("Master plan stages require unique ids and contiguous positions.")
    topology = tuple(item["type"] for item in stages)
    if topology not in {("image",), ("ppt",), ("image", "ppt")}:
        _invalid("Only Image-only, PPT-only and Image-to-PPT plans are supported.")
    stage_by_id = {item["stage_id"]: item for item in stages}
    for stage in stages:
        earlier = set(stage_ids[: stage["position"] - 1])
        if not set(stage["depends_on"]).issubset(earlier):
            _invalid("A Master stage dependency must reference an earlier stage.")
    if topology == ("image", "ppt") and stages[1]["depends_on"] != [stages[0]["stage_id"]]:
        _invalid("An Image-to-PPT plan requires the PPT stage to depend on Image.")

    items = proposal["work_items"]
    item_by_id = {item["work_item_id"]: item for item in items}
    if len(item_by_id) != len(items):
        _invalid("Master work item ids must be unique.")
    for item in items:
        stage = stage_by_id.get(item["stage_id"])
        if item["task_id"] != task_id or stage is None or item["agent_type"] != stage["type"]:
            _invalid("Every Master work item must match its task and stage.")
        if item["current_instance_id"] is None:
            _invalid("A new Master work item requires one current instance.")
        if set(item["instance_ids"]) != {item["current_instance_id"]}:
            _invalid("A new Master plan cannot introduce historical instances.")
        if item["work_item_id"] in item["depends_on"]:
            _invalid("A Master work item cannot depend on itself.")
        if not set(item["depends_on"]).issubset(item_by_id):
            _invalid("A Master work item references an unknown dependency.")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            _invalid("Master work item dependencies contain a cycle.")
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in item_by_id[item_id]["depends_on"]:
            visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in item_by_id:
        visit(item_id)

    cards = proposal["execution_cards"]
    card_by_id = {item["card_id"]: item for item in cards}
    if len(card_by_id) != len(cards):
        _invalid("Master execution card ids must be unique.")
    referenced_cards = [card_id for item in items for card_id in item["task_card_ids"]]
    if len(referenced_cards) != len(set(referenced_cards)) or set(referenced_cards) != set(
        card_by_id
    ):
        _invalid("Every Master execution card must belong to exactly one work item.")
    instance_ids: set[str] = set()
    for item in items:
        if len(item["task_card_ids"]) != 1:
            _invalid("Each new work item requires exactly one active execution card.")
        card = card_by_id[item["task_card_ids"][0]]
        instance_id = item["current_instance_id"]
        if (
            card["task_id"] != task_id
            or card["stage_id"] != item["stage_id"]
            or card["agent_type"] != item["agent_type"]
            or card["instance_id"] != instance_id
        ):
            _invalid("A Master execution card does not match its work item.")
        if instance_id in instance_ids:
            _invalid("A Master instance cannot belong to multiple work items.")
        instance_ids.add(instance_id)


def materialize_plan_proposal(
    proposal: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Translate a validated proposal to the existing frozen plan command shapes."""

    items = proposal["work_items"]
    stage_instances: dict[str, list[str]] = {item["stage_id"]: [] for item in proposal["stages"]}
    instances: list[dict[str, Any]] = []
    for item in items:
        instance_id = item["current_instance_id"]
        stage_instances[item["stage_id"]].append(instance_id)
        instances.append(
            {
                "instance_id": instance_id,
                "stage_id": item["stage_id"],
                "agent_type": item["agent_type"],
                "required": item["required"],
                "approval_mode": "human",
                "config_revision": 1,
                "workspace_relpath": f"instances/{instance_id}",
                "task_card_relpath": f"instances/{instance_id}/task-card.json",
            }
        )
    stages = [
        {**deepcopy(stage), "instance_ids": stage_instances[stage["stage_id"]]}
        for stage in sorted(proposal["stages"], key=lambda item: item["position"])
    ]
    return stages, instances, deepcopy(proposal["execution_cards"])


def _invalid(message: str) -> NoReturn:
    raise HarnessError("VALIDATION_ERROR", message)
