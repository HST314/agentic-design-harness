"""Cross-object validation and execution projection for Master plans."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, NoReturn

from ..contracts import ContractRegistry
from ..core.errors import HarnessError

_SOURCE_CITATION = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?P<asset_id>[A-Za-z][A-Za-z0-9_-]{0,127})/"
    r"(?:page/(?P<page>[1-9][0-9]*)|"
    r"block/(?P<block_id>[A-Za-z][A-Za-z0-9_-]{0,127}))"
    r"(?![A-Za-z0-9_.-])"
)


@dataclass(frozen=True, slots=True)
class AssetSourceIndex:
    """Persisted source locations that a PlanProposal may cite."""

    page_count: int | None
    block_ids: frozenset[str]


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
    stage_by_id = {item["stage_id"]: item for item in stages}
    for stage in stages:
        earlier = set(stage_ids[: stage["position"] - 1])
        if not set(stage["depends_on"]).issubset(earlier):
            _invalid("A Master stage dependency must reference an earlier stage.")
        for dependency_id in stage["depends_on"]:
            dependency = stage_by_id[dependency_id]
            if dependency["type"] != "image" or stage["type"] != "ppt":
                _invalid(
                    "Same-type stages must be parallel and only Image-to-PPT "
                    "dependencies are supported."
                )

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


def validate_source_citations(
    proposal: dict[str, Any], source_indexes: Mapping[str, AssetSourceIndex]
) -> None:
    """Require every cited page/block to exist in persisted asset understanding."""

    for card in proposal["execution_cards"]:
        instructions = "\n".join(card["instructions"])
        input_asset_ids = {source["asset_id"] for source in card["input_assets"]}
        citations_by_asset: dict[str, list[re.Match[str]]] = {
            asset_id: [] for asset_id in input_asset_ids
        }
        for citation in _SOURCE_CITATION.finditer(instructions):
            asset_id = citation.group("asset_id")
            if asset_id not in input_asset_ids:
                _invalid("A source citation references an undeclared input asset.")
            citations_by_asset[asset_id].append(citation)

        for asset_id, citations in citations_by_asset.items():
            if not citations:
                _invalid(
                    "Every input asset in a Master execution card requires an "
                    "asset_id/page or asset_id/block source citation."
                )
            source_index = source_indexes.get(asset_id)
            if source_index is None:
                _invalid("A cited input asset has no persisted source understanding.")
            for citation in citations:
                page = citation.group("page")
                if page is not None:
                    page_number = int(page)
                    if (
                        source_index.page_count is None
                        or page_number > source_index.page_count
                    ):
                        _invalid("A source citation references a page that does not exist.")
                    continue
                block_id = citation.group("block_id")
                if block_id not in source_index.block_ids:
                    _invalid("A source citation references a block that does not exist.")


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
