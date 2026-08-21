"""Cross-object validation for the three frozen Phase 1 task topologies."""

from __future__ import annotations

from typing import Any, NoReturn

from ..contracts import ContractRegistry
from ..core.errors import HarnessError


def validate_plan(contracts: ContractRegistry, plan: dict[str, Any]) -> None:
    contracts.validate("task-plan", plan)
    task_id = plan["task"]["task_id"]
    stages = plan["stages"]
    instances = plan["instances"]
    cards = plan["task_cards"]
    stage_ids = [item["stage_id"] for item in stages]
    positions = [item["position"] for item in stages]
    if len(set(stage_ids)) != len(stage_ids) or positions != list(range(1, len(stages) + 1)):
        _invalid("Stage ids must be unique and positions must be contiguous from one.")
    stage_by_id = {item["stage_id"]: item for item in stages}
    for stage in stages:
        if stage["task_id"] != task_id:
            _invalid("Every stage must belong to the plan task.")
        earlier = set(stage_ids[: stage["position"] - 1])
        if not set(stage["depends_on"]).issubset(earlier):
            _invalid("A stage dependency must reference an earlier stage in the task.")
    topology = tuple(item["type"] for item in stages)
    if topology == ("image",) or topology == ("ppt",):
        valid_topology = stages[0]["depends_on"] == []
    elif topology == ("image", "ppt"):
        valid_topology = stages[0]["depends_on"] == [] and stages[1]["depends_on"] == [
            stages[0]["stage_id"]
        ]
    else:
        valid_topology = False
    if not valid_topology:
        _invalid("Only Image-only, PPT-only and Image-to-PPT plans are supported.")

    instance_by_id: dict[str, dict[str, Any]] = {}
    for instance in instances:
        if instance["instance_id"] in instance_by_id:
            _invalid("Instance ids must be unique within a plan.")
        instance_by_id[instance["instance_id"]] = instance
        stage = stage_by_id.get(instance["stage_id"])
        if stage is None or instance["task_id"] != task_id:
            _invalid("Every instance must belong to a stage in the plan task.")
        if instance["agent_type"] != stage["type"]:
            _invalid("An instance agent type must match its stage type.")
    referenced = [instance_id for stage in stages for instance_id in stage["instance_ids"]]
    if len(referenced) != len(set(referenced)) or set(referenced) != set(instance_by_id):
        _invalid("Every instance must be referenced by exactly one stage.")

    card_instances: set[str] = set()
    for card in cards:
        instance = instance_by_id.get(card["instance_id"])
        if instance is None or card["task_id"] != task_id:
            _invalid("Every task card must reference an instance in the plan task.")
        if card["stage_id"] != instance["stage_id"] or card["agent_type"] != instance["agent_type"]:
            _invalid("Task card stage and agent type must match its instance.")
        if card["instance_id"] in card_instances:
            _invalid("Every instance has exactly one active task card in a saved plan.")
        card_instances.add(card["instance_id"])
    if card_instances != set(instance_by_id):
        _invalid("Every instance requires one task card.")


def _invalid(message: str) -> NoReturn:
    raise HarnessError("VALIDATION_ERROR", message)
