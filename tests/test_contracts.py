from __future__ import annotations

import copy
import json
import re
import unittest
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts" / "v1"
SCHEMAS = CONTRACTS / "schemas"
CATALOGS = CONTRACTS / "catalogs"
PLAN_EXAMPLES = CONTRACTS / "examples" / "plans"
OBJECT_EXAMPLES = CONTRACTS / "examples" / "objects"
GOLDEN = ROOT / "tests" / "golden"

ACTIVE_INSTANCE_STATUSES = {"READY", "STARTING", "RUNNING"}
EXECUTING_INSTANCE_STATUSES = {"STARTING", "RUNNING"}
WAITING_INSTANCE_STATUSES = {"WAITING_APPROVAL"}
FAILED_INSTANCE_STATUSES = {"FAILED_TO_START", "FAILED", "CRASHED"}
COMPLETED_INSTANCE_STATUSES = {"SUCCEEDED", "ARCHIVED"}
HEX_32_REGRESSION_VALUE = "abcdef0123456789abcdef0123456789"
HEX_64_REGRESSION_VALUE = HEX_32_REGRESSION_VALUE * 2


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


CREDENTIAL_POLICY = load_json(CATALOGS / "credential-detection-policy.json")
KNOWN_CREDENTIAL_RULES = tuple(
    (rule["id"], re.compile(rule["pattern"], re.IGNORECASE))
    for rule in CREDENTIAL_POLICY["known_format_rules"]
)
SENSITIVE_SOURCE_IDS = {
    source["id"] for source in CREDENTIAL_POLICY["sensitive_sources"]
}
SENSITIVE_VALUE_POLICY = CREDENTIAL_POLICY["sensitive_value_marker"]


@dataclass(frozen=True)
class SensitiveValue:
    """Internal marker that deliberately has no JSON representation."""

    value: str
    source: str
    locator: str

    def __post_init__(self) -> None:
        if self.source not in SENSITIVE_SOURCE_IDS:
            raise ValueError(f"unknown sensitive source: {self.source}")


SCHEMA_DOCUMENTS = {
    path.name: load_json(path) for path in sorted(SCHEMAS.glob("*.schema.json"))
}


def build_registry() -> Registry:
    registry = Registry()
    for schema in SCHEMA_DOCUMENTS.values():
        registry = registry.with_resource(
            schema["$id"], Resource.from_contents(schema)
        )
    return registry


REGISTRY = build_registry()
FORMAT_CHECKER = FormatChecker()


def is_rfc3339_utc_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
        r"(?:\.\d+)?Z",
        value,
    ) is None:
        return False
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def is_uri(value: object) -> bool:
    if not isinstance(value, str):
        return True
    return bool(urlsplit(value).scheme)


FORMAT_CHECKER.checks("date-time", raises=(TypeError, ValueError))(
    is_rfc3339_utc_datetime
)
FORMAT_CHECKER.checks("uri", raises=(TypeError, ValueError))(is_uri)


def validator(schema_name: str) -> Draft202012Validator:
    return Draft202012Validator(
        SCHEMA_DOCUMENTS[schema_name],
        registry=REGISTRY,
        format_checker=FORMAT_CHECKER,
    )


def validate(schema_name: str, value: Any) -> None:
    validator(schema_name).validate(value)


class SemanticContractError(ValueError):
    pass


def schema_version_is_supported(
    document_version: str, supported_versions: list[str], version_pattern: str
) -> bool:
    return (
        re.fullmatch(version_pattern, document_version) is not None
        and document_version in supported_versions
    )


def credential_rule_for_text(value: str) -> str | None:
    """Return the named rule that identifies a plaintext credential."""

    for rule_id, pattern in KNOWN_CREDENTIAL_RULES:
        if pattern.search(value):
            return rule_id
    return None


def marked_sensitive_value(value: str, source: str, locator: str) -> SensitiveValue:
    """Model an internal sensitive value before a public serialization boundary."""

    return SensitiveValue(value=value, source=source, locator=locator)


def validate_task_card_credentials(value: Any) -> None:
    """Reject marked or recognizably formatted secrets at the public boundary."""

    if isinstance(value, SensitiveValue):
        raise SemanticContractError(
            "public task card serialization rejected sensitive value "
            f"from {value.source}:{value.locator}"
        )
    if isinstance(value, str):
        rule_id = credential_rule_for_text(value)
        if rule_id is not None:
            raise SemanticContractError(
                f"public task card contains a plaintext credential ({rule_id})"
            )
    if isinstance(value, list):
        for item in value:
            validate_task_card_credentials(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_task_card_credentials(key)
            validate_task_card_credentials(item)


def serialize_public_task_card(card: dict[str, Any]) -> str:
    """Apply the secret boundary before producing canonical public JSON."""

    validate_task_card_credentials(card)
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def validate_requirement_lifecycle(
    item: dict[str, Any],
    plan_revision: int,
    task_created_at: datetime,
    task_updated_at: datetime,
    unavailable_activation_required: bool,
) -> bool:
    """Validate frozen original/activation facts and return downgrade evidence."""

    lifecycle = item["requirement_lifecycle"]
    original_required = lifecycle["original_required"]
    first_activated_at = lifecycle["first_activated_at"]
    downgrade = lifecycle["authorized_downgrade"]

    activated_statuses = (
        {"RUNNING", "WAITING_APPROVAL", "SUCCEEDED", "FAILED"}
        if "type" in item
        else {
            "STARTING",
            "RUNNING",
            "WAITING_APPROVAL",
            "FAILED_TO_START",
            "SUCCEEDED",
            "FAILED",
            "CRASHED",
        }
    )
    activation_fact_required = (
        item["status"] in activated_statuses
        or (item["status"] == "UNAVAILABLE" and unavailable_activation_required)
    )
    if activation_fact_required and first_activated_at is None:
        raise SemanticContractError("activated child lacks first activation fact")

    activated_at = None
    if first_activated_at is not None:
        activated_at = datetime.fromisoformat(
            first_activated_at.replace("Z", "+00:00")
        )
        if activated_at < task_created_at:
            raise SemanticContractError("activation predates task creation")
        if activated_at > task_updated_at:
            raise SemanticContractError("activation occurs after task snapshot")
        if "instance_id" in item:
            instance_created_at = datetime.fromisoformat(
                item["created_at"].replace("Z", "+00:00")
            )
            if activated_at < instance_created_at:
                raise SemanticContractError("activation predates instance creation")

    if original_required is False:
        if item["required"] is True:
            raise SemanticContractError("an originally optional child became required")
        if downgrade is not None:
            raise SemanticContractError(
                "an originally optional child cannot carry downgrade authorization"
            )
        return False

    if item["required"] is True:
        if downgrade is not None:
            raise SemanticContractError(
                "a currently required child cannot carry downgrade authorization"
            )
        return False

    if first_activated_at is None or downgrade is None:
        raise SemanticContractError(
            "an originally required child needs activation and authorized downgrade facts"
        )
    if downgrade["plan_revision"] > plan_revision:
        raise SemanticContractError("downgrade revision exceeds the task plan revision")

    authorized_at = datetime.fromisoformat(
        downgrade["authorized_at"].replace("Z", "+00:00")
    )
    assert activated_at is not None
    if authorized_at < activated_at:
        raise SemanticContractError("downgrade authorization predates activation")
    if authorized_at > task_updated_at:
        raise SemanticContractError(
            "downgrade authorization occurs after task snapshot"
        )
    return True


def expected_stage_status(
    stage: dict[str, Any],
    stage_by_id: dict[str, dict[str, Any]],
    instance_by_id: dict[str, dict[str, Any]],
) -> str:
    instances = [instance_by_id[item] for item in stage["instance_ids"]]
    instance_statuses = {item["status"] for item in instances}

    if stage["status"] == "CANCELLED":
        if not instance_statuses.issubset({"CANCELLED", "SUPERSEDED", "ARCHIVED"}):
            raise SemanticContractError("cancelled stage still has non-cancelled instances")
        return "CANCELLED"
    if stage["status"] == "SKIPPED":
        if instance_statuses & (ACTIVE_INSTANCE_STATUSES | WAITING_INSTANCE_STATUSES):
            raise SemanticContractError("skipped stage still has active instances")
        return "SKIPPED"

    dependencies_succeeded = all(
        stage_by_id[dependency]["status"] == "SUCCEEDED"
        for dependency in stage["depends_on"]
    )
    image_instances = [
        item for item in instance_by_id.values() if item["agent_type"] == "image"
    ]
    manual_ppt_gate = bool(
        stage["type"] == "ppt"
        and stage["depends_on"]
        and image_instances
        and all(item.get("manual_finished", False) for item in image_instances)
    )
    already_activated = (
        stage["requirement_lifecycle"]["first_activated_at"] is not None
    )
    if not dependencies_succeeded and not manual_ppt_gate and not already_activated:
        return "PENDING"
    if instance_statuses & EXECUTING_INSTANCE_STATUSES:
        return "RUNNING"
    if "READY" in instance_statuses:
        return "READY"

    required_instances = [item for item in instances if item["required"]]
    required_statuses = {item["status"] for item in required_instances}
    if required_statuses & WAITING_INSTANCE_STATUSES:
        return "WAITING_APPROVAL"
    if required_statuses & FAILED_INSTANCE_STATUSES:
        return "FAILED"
    if "UNAVAILABLE" in required_statuses:
        return "UNAVAILABLE"

    completion_set = required_instances or instances
    if completion_set and all(
        item["status"] in COMPLETED_INSTANCE_STATUSES for item in completion_set
    ):
        return "SUCCEEDED"
    return "PENDING"


def validate_task_aggregate(
    task: dict[str, Any],
    stages: list[dict[str, Any]],
    instances: list[dict[str, Any]],
    has_authorized_downgrade: bool,
) -> None:
    task_status = task["status"]
    instance_statuses = {item["status"] for item in instances}
    stage_by_id = {item["stage_id"]: item for item in stages}

    if task_status == "AWAITING_START_CONFIRMATION":
        allowed_instances = {"CREATED", "READY", "UNAVAILABLE"}
        allowed_stages = {"PENDING", "READY", "UNAVAILABLE"}
        if (
            task["start_policy"] != "manual"
            or not instance_statuses.issubset(allowed_instances)
            or not {item["status"] for item in stages}.issubset(allowed_stages)
        ):
            raise SemanticContractError(
                "awaiting-start task contains activated child state"
            )
        return

    if task_status in {"DRAFT", "PLANNED"}:
        activated = (
            EXECUTING_INSTANCE_STATUSES
            | WAITING_INSTANCE_STATUSES
            | FAILED_INSTANCE_STATUSES
            | COMPLETED_INSTANCE_STATUSES
        )
        if instance_statuses & activated:
            raise SemanticContractError("pre-activation task contains activated instances")
        return

    if task_status == "CANCELLED":
        if instance_statuses & (ACTIVE_INSTANCE_STATUSES | WAITING_INSTANCE_STATUSES):
            raise SemanticContractError("cancelled task still has active instances")
        return

    required_stages = [item for item in stages if item["required"]]
    required_instances = [
        item
        for item in instances
        if item["required"] and stage_by_id[item["stage_id"]]["required"]
    ]
    required_instance_statuses = {item["status"] for item in required_instances}

    if required_instance_statuses & WAITING_INSTANCE_STATUSES:
        expected = "WAITING_APPROVAL"
    elif instance_statuses & ACTIVE_INSTANCE_STATUSES or any(
        item["status"] in {"READY", "RUNNING"} for item in required_stages
    ):
        expected = "RUNNING"
    elif required_instance_statuses & FAILED_INSTANCE_STATUSES or any(
        item["status"] == "FAILED" for item in required_stages
    ):
        expected = "FAILED"
    elif "UNAVAILABLE" in {item["status"] for item in required_stages}:
        expected = "BLOCKED_UNAVAILABLE"
    elif any(item["status"] == "PENDING" for item in required_stages):
        expected = "RUNNING"
    elif all(item["status"] == "SUCCEEDED" for item in required_stages) and all(
        item["status"] in COMPLETED_INSTANCE_STATUSES for item in required_instances
    ):
        expected = "PARTIAL" if has_authorized_downgrade else "SUCCEEDED"
    else:
        raise SemanticContractError("task children do not form an aggregatable state")

    if task_status != expected:
        raise SemanticContractError(
            f"task status {task_status} does not match aggregate {expected}"
        )


def validate_usage_semantics(usage: dict[str, Any]) -> None:
    if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
        raise SemanticContractError("total tokens must equal input plus output")
    if usage["cached_input_tokens"] > usage["input_tokens"]:
        raise SemanticContractError("cached input tokens exceed input tokens")
    if usage["reasoning_tokens"] > usage["output_tokens"]:
        raise SemanticContractError("reasoning tokens exceed output tokens")


def validate_delivery_semantics(delivery: dict[str, Any]) -> None:
    output_prefix = f"instances/{delivery['instance_id']}/outputs/"
    for output in delivery["outputs"]:
        if not output["source_relative_path"].startswith(output_prefix):
            raise SemanticContractError(
                "delivery source must be inside the producing instance outputs"
            )


def validate_asset_semantics(asset: dict[str, Any]) -> None:
    producer = asset["producer_instance_id"]
    if producer is None:
        if not asset["relative_path"].startswith("inputs/"):
            raise SemanticContractError(
                "user asset must resolve inside the task inputs"
            )
        return

    bundle_id = asset.get("bundle_id")
    legacy_path = asset["relative_path"].startswith(
        f"resources/shared/{asset['asset_id']}/"
    )
    bundle_path = (
        isinstance(bundle_id, str)
        and Path(asset["relative_path"]).parent.as_posix() == "resources/shared"
        and Path(asset["relative_path"]).stem == bundle_id
    )
    if not (legacy_path or bundle_path):
        raise SemanticContractError(
            "published asset must use its asset directory or flat bundle stem"
        )
    if not asset["source_relative_path"].startswith(
        f"instances/{producer}/outputs/"
    ):
        raise SemanticContractError(
            "published asset source must resolve inside producer outputs"
        )


def validate_work_item_semantics(work_item: dict[str, Any]) -> None:
    current_instance_id = work_item["current_instance_id"]
    if current_instance_id is not None and current_instance_id not in work_item["instance_ids"]:
        raise SemanticContractError("current instance is absent from work item history")
    if work_item["work_item_id"] in work_item["depends_on"]:
        raise SemanticContractError("work item cannot depend on itself")


def validate_plan_proposal_semantics(proposal: dict[str, Any]) -> None:
    task_id = proposal["task_id"]
    stages = proposal["stages"]
    stage_by_id = {stage["stage_id"]: stage for stage in stages}
    if len(stage_by_id) != len(stages):
        raise SemanticContractError("plan proposal has duplicate stage ids")
    positions = sorted(stage["position"] for stage in stages)
    if positions != list(range(1, len(stages) + 1)):
        raise SemanticContractError("plan proposal stage positions must be consecutive")
    for stage in stages:
        for dependency in stage["depends_on"]:
            dependency_stage = stage_by_id.get(dependency)
            if dependency_stage is None:
                raise SemanticContractError("plan proposal references an unknown stage")
            if dependency_stage["position"] >= stage["position"]:
                raise SemanticContractError("plan proposal stage dependency is not earlier")

    work_items = proposal["work_items"]
    work_item_by_id = {item["work_item_id"]: item for item in work_items}
    if len(work_item_by_id) != len(work_items):
        raise SemanticContractError("plan proposal has duplicate work item ids")
    for item in work_items:
        validate_work_item_semantics(item)
        if item["task_id"] != task_id:
            raise SemanticContractError("work item belongs to another task")
        stage = stage_by_id.get(item["stage_id"])
        if stage is None:
            raise SemanticContractError("work item references an unknown stage")
        if item["agent_type"] != stage["type"]:
            raise SemanticContractError("work item type differs from its stage")
        for dependency in item["depends_on"]:
            dependency_item = work_item_by_id.get(dependency)
            if dependency_item is None:
                raise SemanticContractError("work item references an unknown dependency")
            dependency_stage = stage_by_id[dependency_item["stage_id"]]
            if dependency_stage["position"] > stage["position"]:
                raise SemanticContractError("work item depends on a later stage")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(work_item_id: str) -> None:
        if work_item_id in visiting:
            raise SemanticContractError("work item dependencies contain a cycle")
        if work_item_id in visited:
            return
        visiting.add(work_item_id)
        for dependency in work_item_by_id[work_item_id]["depends_on"]:
            visit(dependency)
        visiting.remove(work_item_id)
        visited.add(work_item_id)

    for work_item_id in work_item_by_id:
        visit(work_item_id)

    cards = proposal["execution_cards"]
    card_by_id = {card["card_id"]: card for card in cards}
    if len(card_by_id) != len(cards):
        raise SemanticContractError("plan proposal has duplicate execution card ids")
    referenced_card_ids = [
        card_id for item in work_items for card_id in item["task_card_ids"]
    ]
    if len(referenced_card_ids) != len(set(referenced_card_ids)):
        raise SemanticContractError("execution card belongs to multiple work items")
    if set(referenced_card_ids) != set(card_by_id):
        raise SemanticContractError("work items and execution cards do not match")
    for item in work_items:
        for card_id in item["task_card_ids"]:
            card = card_by_id[card_id]
            if card["task_id"] != task_id:
                raise SemanticContractError("execution card belongs to another task")
            if card["stage_id"] != item["stage_id"]:
                raise SemanticContractError("execution card stage differs from work item")
            if card["agent_type"] != item["agent_type"]:
                raise SemanticContractError("execution card type differs from work item")
            if card["instance_id"] not in item["instance_ids"]:
                raise SemanticContractError("execution card instance is absent from work item")
            serialize_public_task_card(card)

    created_at = datetime.fromisoformat(proposal["created_at"].replace("Z", "+00:00"))
    updated_at = datetime.fromisoformat(proposal["updated_at"].replace("Z", "+00:00"))
    if updated_at < created_at:
        raise SemanticContractError("plan proposal update predates creation")
    if proposal["confirmed_at"] is not None:
        confirmed_at = datetime.fromisoformat(
            proposal["confirmed_at"].replace("Z", "+00:00")
        )
        if not created_at <= confirmed_at <= updated_at:
            raise SemanticContractError("plan proposal confirmation is outside its timeline")


def validate_plan_semantics(plan: dict[str, Any]) -> None:
    """Validate invariants that span multiple JSON Schema objects."""

    task = plan["task"]
    task_id = task["task_id"]
    stages = sorted(plan["stages"], key=lambda item: item["position"])
    instances = plan["instances"]
    cards = plan["task_cards"]
    task_created_at = datetime.fromisoformat(
        task["created_at"].replace("Z", "+00:00")
    )
    task_updated_at = datetime.fromisoformat(
        task["updated_at"].replace("Z", "+00:00")
    )
    if task_updated_at < task_created_at:
        raise SemanticContractError("task update predates task creation")

    for instance in instances:
        instance_created_at = datetime.fromisoformat(
            instance["created_at"].replace("Z", "+00:00")
        )
        if instance_created_at < task_created_at:
            raise SemanticContractError("instance creation predates task creation")
        if instance_created_at > task_updated_at:
            raise SemanticContractError(
                "instance creation occurs after task snapshot"
            )

    if plan["schema_version"] != task["schema_version"]:
        raise SemanticContractError("plan and task schema versions differ")
    for item in [*plan["stages"], *plan["instances"], *plan["task_cards"]]:
        if item["schema_version"] != plan["schema_version"]:
            raise SemanticContractError("nested schema version differs from plan")

    stage_ids = [stage["stage_id"] for stage in stages]
    if len(stage_ids) != len(set(stage_ids)):
        raise SemanticContractError("stage ids must be unique")

    positions = [stage["position"] for stage in stages]
    if positions != list(range(1, len(stages) + 1)):
        raise SemanticContractError("stage positions must be unique and contiguous")

    topology = tuple(stage["type"] for stage in stages)
    if topology not in {("image",), ("ppt",), ("image", "ppt")}:
        raise SemanticContractError(f"unsupported task topology: {topology}")

    position_by_stage = {
        stage["stage_id"]: stage["position"] for stage in stages
    }
    stage_by_id = {stage["stage_id"]: stage for stage in stages}
    for stage in stages:
        if stage["task_id"] != task_id:
            raise SemanticContractError("stage belongs to another task")
        for dependency in stage["depends_on"]:
            if dependency not in position_by_stage:
                raise SemanticContractError("stage dependency does not exist")
            if position_by_stage[dependency] >= stage["position"]:
                raise SemanticContractError("stage dependency must point backward")

    if stages[0]["depends_on"]:
        raise SemanticContractError("first stage cannot have dependencies")
    if topology == ("image", "ppt") and stages[1]["depends_on"] != [
        stages[0]["stage_id"]
    ]:
        raise SemanticContractError("image-to-ppt must use the canonical dependency")

    instance_by_id = {item["instance_id"]: item for item in instances}
    if len(instance_by_id) != len(instances):
        raise SemanticContractError("instance ids must be unique")

    declared_instance_ids: set[str] = set()
    for stage in stages:
        for instance_id in stage["instance_ids"]:
            if instance_id in declared_instance_ids:
                raise SemanticContractError("instance belongs to multiple stages")
            declared_instance_ids.add(instance_id)
            instance = instance_by_id.get(instance_id)
            if instance is None:
                raise SemanticContractError("stage references a missing instance")
            if instance["task_id"] != task_id:
                raise SemanticContractError("instance belongs to another task")
            if instance["stage_id"] != stage["stage_id"]:
                raise SemanticContractError("instance stage reference is inconsistent")
            if instance["agent_type"] != stage["type"]:
                raise SemanticContractError("instance type differs from stage type")

    if declared_instance_ids != set(instance_by_id):
        raise SemanticContractError("plan contains an undeclared instance")

    dependencies_succeeded_by_stage = {
        stage["stage_id"]: all(
            stage_by_id[dependency]["status"] == "SUCCEEDED"
            for dependency in stage["depends_on"]
        )
        for stage in stages
    }
    downgrade_evidence = []
    for stage in stages:
        unavailable_activation_required = (
            task["status"] == "BLOCKED_UNAVAILABLE"
            and stage["required"]
            and stage["status"] == "UNAVAILABLE"
            and dependencies_succeeded_by_stage[stage["stage_id"]]
        )
        downgrade_evidence.append(
            validate_requirement_lifecycle(
                stage,
                task["plan_revision"],
                task_created_at,
                task_updated_at,
                unavailable_activation_required,
            )
        )
    for instance in instances:
        stage = stage_by_id[instance["stage_id"]]
        unavailable_activation_required = (
            task["status"] == "BLOCKED_UNAVAILABLE"
            and stage["required"]
            and instance["required"]
            and instance["status"] == "UNAVAILABLE"
            and dependencies_succeeded_by_stage[stage["stage_id"]]
            and stage["status"] not in {"SKIPPED", "CANCELLED"}
        )
        downgrade_evidence.append(
            validate_requirement_lifecycle(
                instance,
                task["plan_revision"],
                task_created_at,
                task_updated_at,
                unavailable_activation_required,
            )
        )
    has_authorized_downgrade = any(downgrade_evidence)

    for stage in stages:
        aggregate = expected_stage_status(stage, stage_by_id, instance_by_id)
        if stage["status"] != aggregate:
            raise SemanticContractError(
                f"stage {stage['stage_id']} status {stage['status']} "
                f"does not match aggregate {aggregate}"
            )
    validate_task_aggregate(
        task, stages, instances, has_authorized_downgrade
    )

    card_by_instance = {item["instance_id"]: item for item in cards}
    if len(card_by_instance) != len(cards):
        raise SemanticContractError("an instance has multiple task cards")
    if set(card_by_instance) != set(instance_by_id):
        raise SemanticContractError("every instance must have exactly one task card")

    for instance_id, card in card_by_instance.items():
        instance = instance_by_id[instance_id]
        if card["task_id"] != task_id:
            raise SemanticContractError("task card belongs to another task")
        if card["stage_id"] != instance["stage_id"]:
            raise SemanticContractError("task card stage reference is inconsistent")
        if card["agent_type"] != instance["agent_type"]:
            raise SemanticContractError("task card type differs from instance type")
        serialize_public_task_card(card)
        for asset_ref in card["input_assets"]:
            if not asset_ref["manifest_relpath"].startswith(
                "resources/manifests/"
            ):
                raise SemanticContractError(
                    "task card asset reference must use a registered manifest"
                )


class SchemaTests(unittest.TestCase):
    def test_every_schema_is_valid_draft_2020_12(self) -> None:
        ids: set[str] = set()
        for name, schema in SCHEMA_DOCUMENTS.items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)
                self.assertEqual(
                    schema["$schema"],
                    "https://json-schema.org/draft/2020-12/schema",
                )
                self.assertNotIn(schema["$id"], ids)
                ids.add(schema["$id"])

    def test_all_plan_examples_pass_schema_and_semantics(self) -> None:
        paths = sorted(PLAN_EXAMPLES.glob("*.json"))
        self.assertEqual(
            {path.name for path in paths},
            {"image-only.json", "ppt-only.json", "image-to-ppt.json"},
        )
        for path in paths:
            with self.subTest(example=path.name):
                plan = load_json(path)
                validate("task-plan.schema.json", plan)
                validate_plan_semantics(plan)

    def test_standalone_object_examples_pass(self) -> None:
        examples = {
            "bundle-manifest.json": "bundle-manifest.schema.json",
            "delivery-bundle-candidate.json": "delivery-bundle-candidate.schema.json",
            "imported-asset.json": "asset-manifest.schema.json",
            "master-message.json": "master-message.schema.json",
            "master-thread.json": "master-thread.schema.json",
            "published-asset.json": "asset-manifest.schema.json",
            "published-delivery.json": "delivery.schema.json",
            "pending-approval.json": "approval-request.schema.json",
            "plan-proposal.json": "plan-proposal.schema.json",
            "task-intake.json": "task-intake.schema.json",
            "task-navigation-metadata.json": "task-navigation-metadata.schema.json",
            "token-usage.json": "token-usage-event.schema.json",
            "work-item.json": "work-item.schema.json",
            "work-item-projection.json": "work-item-projection.schema.json",
        }
        self.assertEqual(
            {path.name for path in OBJECT_EXAMPLES.glob("*.json")},
            set(examples),
        )
        for example_name, schema_name in examples.items():
            with self.subTest(example=example_name):
                document = load_json(OBJECT_EXAMPLES / example_name)
                validate(schema_name, document)
                if example_name in {"imported-asset.json", "published-asset.json"}:
                    validate_asset_semantics(document)
                elif example_name == "published-delivery.json":
                    validate_delivery_semantics(document)
                elif example_name == "token-usage.json":
                    validate_usage_semantics(document)
                elif example_name == "work-item.json":
                    validate_work_item_semantics(document)
                elif example_name == "plan-proposal.json":
                    validate_plan_proposal_semantics(document)

    def test_delivery_bundle_candidate_requires_atomic_decision_fields(self) -> None:
        candidate = load_json(OBJECT_EXAMPLES / "delivery-bundle-candidate.json")
        validate("delivery-bundle-candidate.schema.json", candidate)

        candidate["status"] = "PUBLISHED"
        with self.assertRaises(ValidationError):
            validate("delivery-bundle-candidate.schema.json", candidate)

        candidate.update(
            {
                "decided_at": "2026-08-22T15:05:00Z",
                "actor": {"type": "human", "id": "reviewer_01"},
                "publication_batch_id": "batch_bundle_main_01",
            }
        )
        validate("delivery-bundle-candidate.schema.json", candidate)

    def test_root_contracts_use_current_schema_version(self) -> None:
        catalog = load_json(CATALOGS / "schema-versions.json")
        current = catalog["current_schema_version"]
        self.assertEqual(catalog["supported_schema_versions"], [current])
        for path in sorted((CONTRACTS / "examples").rglob("*.json")):
            with self.subTest(example=path.relative_to(CONTRACTS)):
                document = load_json(path)
                self.assertEqual(document["schema_version"], current)


class WorkbenchContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work_item = load_json(OBJECT_EXAMPLES / "work-item.json")
        self.proposal = load_json(OBJECT_EXAMPLES / "plan-proposal.json")
        self.intake = load_json(OBJECT_EXAMPLES / "task-intake.json")
        self.navigation = load_json(
            OBJECT_EXAMPLES / "task-navigation-metadata.json"
        )

    def test_current_instance_must_be_in_work_item_history(self) -> None:
        self.work_item["current_instance_id"] = "instance_unknown"
        with self.assertRaisesRegex(
            SemanticContractError, "absent from work item history"
        ):
            validate_work_item_semantics(self.work_item)

    def test_plan_proposal_references_are_closed_and_acyclic(self) -> None:
        validate_plan_proposal_semantics(self.proposal)
        self.proposal["work_items"][0]["depends_on"] = ["work_unknown"]
        with self.assertRaisesRegex(
            SemanticContractError, "unknown dependency"
        ):
            validate_plan_proposal_semantics(self.proposal)

    def test_plan_proposal_card_must_match_its_work_item(self) -> None:
        self.proposal["execution_cards"][0]["instance_id"] = "instance_unknown"
        with self.assertRaisesRegex(
            SemanticContractError, "absent from work item"
        ):
            validate_plan_proposal_semantics(self.proposal)

    def test_submitted_intake_locks_upload_session(self) -> None:
        self.intake["status"] = "SUBMITTED"
        self.intake["submitted_at"] = "2026-08-22T09:05:00Z"
        with self.assertRaises(ValidationError):
            validate("task-intake.schema.json", self.intake)
        self.intake["upload_session"]["status"] = "LOCKED"
        validate("task-intake.schema.json", self.intake)

    def test_archived_navigation_metadata_cannot_remain_pinned(self) -> None:
        self.navigation["archived_at"] = "2026-08-22T09:05:00Z"
        with self.assertRaises(ValidationError):
            validate("task-navigation-metadata.schema.json", self.navigation)
        self.navigation["pinned_at"] = None
        validate("task-navigation-metadata.schema.json", self.navigation)


class TopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.image_only = load_json(PLAN_EXAMPLES / "image-only.json")
        self.ppt_only = load_json(PLAN_EXAMPLES / "ppt-only.json")
        self.image_to_ppt = load_json(PLAN_EXAMPLES / "image-to-ppt.json")

    def test_image_only_topology(self) -> None:
        self.assertEqual(
            [stage["type"] for stage in self.image_only["stages"]], ["image"]
        )
        self.assertEqual(len(self.image_only["instances"]), 3)
        self.assertTrue(
            all(
                instance["agent_type"] == "image"
                for instance in self.image_only["instances"]
            )
        )

    def test_ppt_only_places_ppt_first_and_persists_unavailable(self) -> None:
        stage = self.ppt_only["stages"][0]
        self.assertEqual(stage["type"], "ppt")
        self.assertEqual(stage["position"], 1)
        self.assertEqual(stage["depends_on"], [])
        self.assertEqual(stage["status"], "UNAVAILABLE")
        self.assertEqual(
            self.ppt_only["task"]["status"], "BLOCKED_UNAVAILABLE"
        )

    def test_image_to_ppt_depends_on_image_without_premature_block(self) -> None:
        image_stage, ppt_stage = self.image_to_ppt["stages"]
        self.assertEqual((image_stage["type"], ppt_stage["type"]), ("image", "ppt"))
        self.assertEqual(ppt_stage["depends_on"], [image_stage["stage_id"]])
        self.assertEqual(ppt_stage["status"], "PENDING")
        self.assertEqual(self.image_to_ppt["task"]["status"], "RUNNING")

    def test_forward_or_missing_stage_dependency_is_rejected(self) -> None:
        plan = copy.deepcopy(self.image_to_ppt)
        plan["stages"][0]["depends_on"] = ["s_deck"]
        with self.assertRaisesRegex(SemanticContractError, "point backward"):
            validate_plan_semantics(plan)

        plan = copy.deepcopy(self.image_to_ppt)
        plan["stages"][1]["depends_on"] = ["s_missing"]
        with self.assertRaisesRegex(SemanticContractError, "does not exist"):
            validate_plan_semantics(plan)

    def test_ppt_to_image_and_duplicate_stage_topologies_are_rejected(self) -> None:
        for topology in [("ppt", "image"), ("image", "image")]:
            with self.subTest(topology=topology):
                plan = copy.deepcopy(self.image_to_ppt)
                for index, agent_type in enumerate(topology):
                    plan["stages"][index]["type"] = agent_type
                    plan["instances"][index]["agent_type"] = agent_type
                    plan["task_cards"][index]["agent_type"] = agent_type
                with self.assertRaisesRegex(
                    SemanticContractError, "unsupported task topology"
                ):
                    validate_plan_semantics(plan)

    def test_cross_task_instance_reference_is_rejected(self) -> None:
        plan = copy.deepcopy(self.image_only)
        plan["instances"][0]["task_id"] = "t_other"
        with self.assertRaisesRegex(SemanticContractError, "another task"):
            validate_plan_semantics(plan)


class AggregateStateTests(unittest.TestCase):
    def test_manual_image_gate_authorizes_pending_ppt_stage(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-to-ppt.json")
        image_instance, ppt_instance = plan["instances"]
        ppt_stage = plan["stages"][1]
        ppt_instance["status"] = "READY"
        image_instance["manual_finished"] = True
        stage_by_id = {item["stage_id"]: item for item in plan["stages"]}
        instance_by_id = {
            item["instance_id"]: item for item in plan["instances"]
        }

        self.assertEqual(
            expected_stage_status(ppt_stage, stage_by_id, instance_by_id),
            "READY",
        )
        image_instance["manual_finished"] = False
        self.assertEqual(
            expected_stage_status(ppt_stage, stage_by_id, instance_by_id),
            "PENDING",
        )

    def test_activated_child_requires_first_activation_fact(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-to-ppt.json")
        plan["instances"][0]["requirement_lifecycle"][
            "first_activated_at"
        ] = None
        validate("task-plan.schema.json", plan)
        with self.assertRaisesRegex(
            SemanticContractError, "activated child lacks first activation fact"
        ):
            validate_plan_semantics(plan)

    def test_succeeded_task_with_ready_children_is_rejected(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-only.json")
        plan["task"]["status"] = "SUCCEEDED"
        with self.assertRaisesRegex(SemanticContractError, "task status SUCCEEDED"):
            validate_plan_semantics(plan)

    def test_stage_status_must_match_instance_aggregate(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-to-ppt.json")
        plan["stages"][0]["status"] = "READY"
        with self.assertRaisesRegex(SemanticContractError, "stage s_visual status"):
            validate_plan_semantics(plan)

    def test_required_ppt_blocks_only_after_image_stage_finishes(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-to-ppt.json")
        plan["instances"][0]["status"] = "SUCCEEDED"
        plan["stages"][0]["status"] = "SUCCEEDED"
        plan["stages"][1]["status"] = "UNAVAILABLE"
        for item in (plan["stages"][1], plan["instances"][1]):
            item["requirement_lifecycle"]["first_activated_at"] = (
                plan["task"]["updated_at"]
            )
        plan["task"]["status"] = "BLOCKED_UNAVAILABLE"
        validate_plan_semantics(plan)

        plan["task"]["status"] = "SUCCEEDED"
        with self.assertRaisesRegex(
            SemanticContractError, "does not match aggregate BLOCKED_UNAVAILABLE"
        ):
            validate_plan_semantics(plan)

    def test_triggered_unavailable_requires_activation_facts(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "ppt-only.json")
        for collection_name in ("stages", "instances"):
            with self.subTest(collection=collection_name):
                missing_fact = copy.deepcopy(plan)
                missing_fact[collection_name][0]["requirement_lifecycle"][
                    "first_activated_at"
                ] = None
                validate("task-plan.schema.json", missing_fact)
                with self.assertRaisesRegex(
                    SemanticContractError,
                    "activated child lacks first activation fact",
                ):
                    validate_plan_semantics(missing_fact)

    def test_pre_confirmation_cancellation_preserves_unavailable_placeholder(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "ppt-only.json")
        plan["task"]["start_policy"] = "manual"
        plan["task"]["status"] = "CANCELLED"
        for item in (plan["stages"][0], plan["instances"][0]):
            item["requirement_lifecycle"]["first_activated_at"] = None

        validate("task-plan.schema.json", plan)
        validate_plan_semantics(plan)

    def test_waiting_dependency_does_not_count_as_triggered_unavailable(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-to-ppt.json")
        self.assertEqual(plan["instances"][1]["status"], "UNAVAILABLE")
        self.assertIsNone(
            plan["instances"][1]["requirement_lifecycle"][
                "first_activated_at"
            ]
        )
        validate_plan_semantics(plan)

    def test_activation_is_anchored_to_task_and_instance_timeline(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "ppt-only.json")

        activation_before_task = copy.deepcopy(plan)
        for item in (
            activation_before_task["stages"][0],
            activation_before_task["instances"][0],
        ):
            item["requirement_lifecycle"]["first_activated_at"] = (
                "2025-08-19T16:10:01Z"
            )
        with self.assertRaisesRegex(
            SemanticContractError, "activation predates task creation"
        ):
            validate_plan_semantics(activation_before_task)

        activation_before_instance = copy.deepcopy(plan)
        activation_before_instance["task"]["updated_at"] = (
            "2026-08-19T16:10:03Z"
        )
        activation_before_instance["instances"][0]["created_at"] = (
            "2026-08-19T16:10:02Z"
        )
        with self.assertRaisesRegex(
            SemanticContractError, "activation predates instance creation"
        ):
            validate_plan_semantics(activation_before_instance)

        activation_after_snapshot = copy.deepcopy(plan)
        activation_after_snapshot["task"]["updated_at"] = (
            "2026-08-19T16:10:00Z"
        )
        with self.assertRaisesRegex(
            SemanticContractError, "activation occurs after task snapshot"
        ):
            validate_plan_semantics(activation_after_snapshot)

    def test_instance_creation_is_anchored_to_task_snapshot_window(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "ppt-only.json")

        before_task = copy.deepcopy(plan)
        before_task["instances"][0]["created_at"] = "2025-08-19T16:10:00Z"
        with self.assertRaisesRegex(
            SemanticContractError, "instance creation predates task creation"
        ):
            validate_plan_semantics(before_task)

        after_snapshot = copy.deepcopy(plan)
        after_snapshot["instances"][0]["created_at"] = (
            "2026-08-19T16:10:02Z"
        )
        with self.assertRaisesRegex(
            SemanticContractError, "instance creation occurs after task snapshot"
        ):
            validate_plan_semantics(after_snapshot)

    def test_waiting_approval_has_priority_over_running_work(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-only.json")
        plan["task"]["status"] = "WAITING_APPROVAL"
        plan["task"]["updated_at"] = "2026-08-19T16:01:00Z"
        plan["stages"][0]["status"] = "RUNNING"
        plan["instances"][0]["status"] = "WAITING_APPROVAL"
        plan["instances"][1]["status"] = "RUNNING"
        plan["instances"][2]["status"] = "SUCCEEDED"
        for item in [*plan["stages"], *plan["instances"]]:
            item["requirement_lifecycle"]["first_activated_at"] = (
                "2026-08-19T16:01:00Z"
            )
        validate_plan_semantics(plan)

        plan["instances"][1]["status"] = "SUCCEEDED"
        plan["stages"][0]["status"] = "WAITING_APPROVAL"
        plan["task"]["status"] = "WAITING_APPROVAL"
        validate_plan_semantics(plan)

    def test_initial_optional_ppt_does_not_make_task_partial(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-to-ppt.json")
        plan["instances"][0]["status"] = "SUCCEEDED"
        plan["stages"][0]["status"] = "SUCCEEDED"

        ppt_stage = plan["stages"][1]
        ppt_stage["required"] = False
        ppt_stage["requirement_lifecycle"] = {
            "original_required": False,
            "first_activated_at": None,
            "authorized_downgrade": None,
        }
        ppt_stage["status"] = "SKIPPED"

        ppt_instance = plan["instances"][1]
        ppt_instance["required"] = False
        ppt_instance["requirement_lifecycle"] = copy.deepcopy(
            ppt_stage["requirement_lifecycle"]
        )

        plan["task"]["status"] = "PARTIAL"
        validate("task-plan.schema.json", plan)
        with self.assertRaisesRegex(
            SemanticContractError,
            "task status PARTIAL does not match aggregate SUCCEEDED",
        ):
            validate_plan_semantics(plan)

        plan["task"]["status"] = "SUCCEEDED"
        validate_plan_semantics(plan)

    def test_partial_requires_activated_authorized_downgrade(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-to-ppt.json")
        plan["task"]["status"] = "PARTIAL"
        plan["task"]["plan_revision"] = 2
        plan["task"]["updated_at"] = "2026-08-19T16:25:00Z"
        plan["instances"][0]["status"] = "SUCCEEDED"
        plan["stages"][0]["status"] = "SUCCEEDED"

        downgrade = {
            "authorization_id": "auth_ppt_downgrade",
            "authorized_at": "2026-08-19T16:25:00Z",
            "authorized_by_type": "master",
            "authorized_by_id": "master_default",
            "plan_revision": 2,
            "reason": "PPT adapter unavailable; retain completed image delivery.",
        }
        for item in (plan["stages"][1], plan["instances"][1]):
            item["required"] = False
            item["requirement_lifecycle"] = {
                "original_required": True,
                "first_activated_at": "2026-08-19T16:24:00Z",
                "authorized_downgrade": copy.deepcopy(downgrade),
            }
        plan["stages"][1]["status"] = "SKIPPED"

        validate("task-plan.schema.json", plan)
        validate_plan_semantics(plan)

        authorization_after_snapshot = copy.deepcopy(plan)
        authorization_after_snapshot["task"]["updated_at"] = (
            "2026-08-19T16:24:30Z"
        )
        with self.assertRaisesRegex(
            SemanticContractError,
            "downgrade authorization occurs after task snapshot",
        ):
            validate_plan_semantics(authorization_after_snapshot)

        plan["stages"][1]["requirement_lifecycle"][
            "authorized_downgrade"
        ] = None
        with self.assertRaisesRegex(
            SemanticContractError,
            "needs activation and authorized downgrade facts",
        ):
            validate_plan_semantics(plan)


class BoundaryTests(unittest.TestCase):
    def test_absolute_and_traversing_paths_are_rejected(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-only.json")
        instance = copy.deepcopy(plan["instances"][0])
        instance["workspace_relpath"] = "/tmp/outside"
        with self.assertRaises(ValidationError):
            validate("agent-instance.schema.json", instance)

        instance["workspace_relpath"] = "instances/../outside"
        with self.assertRaises(ValidationError):
            validate("agent-instance.schema.json", instance)

        instance["workspace_relpath"] = "instances\\outside"
        with self.assertRaises(ValidationError):
            validate("agent-instance.schema.json", instance)

    def test_api_key_cannot_be_added_to_public_instance_contract(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-only.json")
        instance = copy.deepcopy(plan["instances"][0])
        instance["api_key"] = "must-not-be-public"
        with self.assertRaises(ValidationError):
            validate("agent-instance.schema.json", instance)

    def test_task_card_uses_agent_parameter_whitelists(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-only.json")
        card = copy.deepcopy(plan["task_cards"][0])
        for forbidden_name in (
            "api_key",
            "openai_api_key",
            "subscription_key",
            "author_name",
            "slide_count",
        ):
            with self.subTest(forbidden_name=forbidden_name):
                rejected = copy.deepcopy(card)
                rejected["parameters"][forbidden_name] = "must-not-be-public"
                with self.assertRaises(ValidationError):
                    validate("task-card.schema.json", rejected)

        card = copy.deepcopy(plan["task_cards"][0])
        card["parameters"]["output_dir"] = "/etc"
        with self.assertRaises(ValidationError):
            validate("task-card.schema.json", card)

        validate("task-card.schema.json", plan["task_cards"][0])
        validate(
            "task-card.schema.json",
            load_json(PLAN_EXAMPLES / "ppt-only.json")["task_cards"][0],
        )

    def test_task_card_1_1_adds_only_declared_image_adapter_inputs(self) -> None:
        fixture = load_json(GOLDEN / "image-task-card-mapping-v1.1.json")
        card = fixture["harness_task_card"]
        validate("task-card-v1.1.schema.json", card)

        with self.assertRaises(ValidationError):
            validate("task-card.schema.json", card)

        old_card = copy.deepcopy(load_json(PLAN_EXAMPLES / "image-only.json")["task_cards"][0])
        validate("task-card.schema.json", old_card)
        old_card["schema_version"] = "1.1"
        validate("task-card-v1.1.schema.json", old_card)

        incomplete_category = copy.deepcopy(card)
        del incomplete_category["parameters"]["category_id"]
        with self.assertRaises(ValidationError):
            validate("task-card-v1.1.schema.json", incomplete_category)

        forbidden = copy.deepcopy(card)
        forbidden["parameters"]["output_dir"] = "/tmp/outside"
        with self.assertRaises(ValidationError):
            validate("task-card-v1.1.schema.json", forbidden)

    def test_task_card_rejects_credentials_outside_parameters(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-only.json")

        card = copy.deepcopy(plan["task_cards"][0])
        card["instructions"].append("Use sk-plaintext-secret for this request")
        with self.assertRaises(ValidationError):
            validate("task-card.schema.json", card)

        for hex_value in (
            HEX_32_REGRESSION_VALUE,
            HEX_64_REGRESSION_VALUE,
        ):
            with self.subTest(explicit_credential_assignment=len(hex_value)):
                card = copy.deepcopy(plan["task_cards"][0])
                card["objective"] = f"api_key={hex_value}"
                with self.assertRaises(ValidationError):
                    validate("task-card.schema.json", card)
                with self.assertRaisesRegex(
                    SemanticContractError, "plaintext credential"
                ):
                    serialize_public_task_card(card)

        secret_plan = copy.deepcopy(plan)
        secret_plan["task_cards"][0]["input_assets"][0][
            "asset_id"
        ] = "sk-plaintext-secret"
        validate("task-plan.schema.json", secret_plan)
        with self.assertRaisesRegex(SemanticContractError, "plaintext credential"):
            validate_plan_semantics(secret_plan)

    def test_task_card_rejects_known_provider_credentials(self) -> None:
        provider_tokens = {
            "github_fine_grained": "github_pat_"
            "11AA0aBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSs",
            "slack_bot": "xoxb-"
            "111111111111-222222222222-aBcDeFgHiJkLmNoPqRsTuVwX",
            "aws_access_key_id": "AKIA" "IOSFODNN7EXAMPLE",
            "aws_secret_access_key": "aws_secret_access_key="
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        }
        for provider, token in provider_tokens.items():
            with self.subTest(provider=provider):
                plan = load_json(PLAN_EXAMPLES / "image-only.json")
                plan["task_cards"][0]["objective"] = (
                    f"Use synthetic credential {token}"
                )
                with self.assertRaisesRegex(
                    SemanticContractError, "plaintext credential"
                ):
                    validate_plan_semantics(plan)

        for token in provider_tokens.values():
            with self.subTest(schema_fast_rejection=token.split("-")[0]):
                card = load_json(PLAN_EXAMPLES / "image-only.json")[
                    "task_cards"
                ][0]
                card["objective"] = f"Use synthetic credential {token}"
                with self.assertRaises(ValidationError):
                    validate("task-card.schema.json", card)

    def test_sensitive_source_marker_rejects_opaque_base36_key(self) -> None:
        base36_value = "q7m2v9k4x8c5n1b6d3f0h7j2l9p4r8t"
        card = copy.deepcopy(
            load_json(PLAN_EXAMPLES / "image-only.json")["task_cards"][0]
        )
        card["objective"] = marked_sensitive_value(
            base36_value,
            source="environment",
            locator="IMAGE_API_KEY",
        )

        with self.assertRaisesRegex(
            SemanticContractError,
            "serialization rejected sensitive value from environment:IMAGE_API_KEY",
        ):
            serialize_public_task_card(card)

        with self.assertRaises(TypeError):
            json.dumps(card)

        with self.assertRaisesRegex(ValueError, "unknown sensitive source"):
            marked_sensitive_value(base36_value, "unclassified", "unknown")

    def test_sensitive_source_marker_rejects_hex_values_allowed_as_asset_ids(
        self,
    ) -> None:
        hex_values = (
            HEX_32_REGRESSION_VALUE,
            HEX_64_REGRESSION_VALUE,
        )
        for hex_value in hex_values:
            with self.subTest(hex_characters=len(hex_value)):
                card = copy.deepcopy(
                    load_json(PLAN_EXAMPLES / "image-only.json")["task_cards"][0]
                )
                card["input_assets"][0]["asset_id"] = marked_sensitive_value(
                    hex_value,
                    source="environment",
                    locator=f"HEX_{len(hex_value)}_SECRET",
                )
                with self.assertRaisesRegex(
                    SemanticContractError,
                    "serialization rejected sensitive value from environment",
                ):
                    serialize_public_task_card(card)

    def test_unmarked_hex_asset_ids_and_digest_filename_are_allowed(self) -> None:
        digest_filename = f"render-{HEX_32_REGRESSION_VALUE}.png"
        plan = load_json(PLAN_EXAMPLES / "image-only.json")
        plan["task_cards"][0]["input_assets"][0][
            "asset_id"
        ] = HEX_32_REGRESSION_VALUE
        plan["task_cards"][1]["input_assets"][0][
            "asset_id"
        ] = HEX_64_REGRESSION_VALUE
        plan["task_cards"][0]["instructions"].append(
            f"Preserve {digest_filename} as the source filename."
        )

        validate("task-plan.schema.json", plan)
        validate_plan_semantics(plan)
        serialized_cards = [
            serialize_public_task_card(card) for card in plan["task_cards"][:2]
        ]
        self.assertIn(HEX_32_REGRESSION_VALUE, serialized_cards[0])
        self.assertIn(HEX_64_REGRESSION_VALUE, serialized_cards[1])
        self.assertIn(digest_filename, serialized_cards[0])

    def test_unmarked_business_identifiers_and_long_filename_are_allowed(self) -> None:
        business_order = "CustomerOrder2026AugustBatch9471"
        base36_asset_id = "q7m2v9k4x8c5n1b6d3f0h7j2l9p4r8t"
        plan = load_json(PLAN_EXAMPLES / "image-only.json")
        plan["task_cards"][0]["objective"] = (
            f"Create a launch visual for {business_order}."
        )
        plan["task_cards"][0]["input_assets"][0]["asset_id"] = base36_asset_id
        plan["task_cards"][0]["instructions"].append(
            "Preserve campaign-2026-08-customer-order-9471-final-master-"
            "illustration.png as the original source filename."
        )
        validate("task-plan.schema.json", plan)
        validate_plan_semantics(plan)
        serialized = serialize_public_task_card(plan["task_cards"][0])
        self.assertIn(business_order, serialized)
        self.assertIn(base36_asset_id, serialized)

    def test_published_asset_requires_provenance(self) -> None:
        asset = load_json(OBJECT_EXAMPLES / "published-asset.json")
        del asset["publication_id"]
        with self.assertRaises(ValidationError):
            validate("asset-manifest.schema.json", asset)

        imported = load_json(OBJECT_EXAMPLES / "imported-asset.json")
        imported["publication_id"] = "pub_invalid"
        with self.assertRaises(ValidationError):
            validate("asset-manifest.schema.json", imported)

    def test_published_delivery_requires_published_asset_reference(self) -> None:
        delivery = load_json(OBJECT_EXAMPLES / "published-delivery.json")
        delivery["published_assets"] = []
        with self.assertRaises(ValidationError):
            validate("delivery.schema.json", delivery)

        delivery["status"] = "CANDIDATE"
        delivery["published_assets"] = [
            {
                "asset_id": "a_invalid",
                "manifest_relpath": "resources/manifests/a_invalid.json",
            }
        ]
        with self.assertRaises(ValidationError):
            validate("delivery.schema.json", delivery)

    def test_pending_approval_cannot_contain_a_resolution(self) -> None:
        approval = load_json(OBJECT_EXAMPLES / "pending-approval.json")
        approval.update(
            {
                "resolved_at": "2026-08-19T16:31:00Z",
                "resolved_by_type": "human",
                "resolved_by_id": "operator_01",
            }
        )
        with self.assertRaises(ValidationError):
            validate("approval-request.schema.json", approval)

    def test_token_total_semantic(self) -> None:
        usage = load_json(OBJECT_EXAMPLES / "token-usage.json")
        validate_usage_semantics(usage)
        usage["total_tokens"] += 1
        with self.assertRaisesRegex(SemanticContractError, "total tokens"):
            validate_usage_semantics(usage)

    def test_token_usage_1_1_keeps_image_units_distinct_from_tokens(self) -> None:
        usage = {
            "schema_version": "1.1",
            "event_id": "usage_image_contract",
            "task_id": "t_contract",
            "instance_id": "i_contract",
            "agent_type": "image",
            "request_id": "request_contract",
            "provider_request_id": None,
            "provider": "ark",
            "model": "seedream",
            "call_type": "text_to_image_model",
            "usage_basis": "image_units",
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "billing_units": [
                {
                    "unit": "image",
                    "quantity": 1,
                    "attributes": {"resolution": "2560x1440"},
                }
            ],
            "raw_usage": {},
            "occurred_at": "2026-08-21T10:00:00Z",
        }
        validate("token-usage-event-v1.1.schema.json", usage)
        validate_usage_semantics(usage)

        usage["billing_units"][0]["quantity"] = 0
        with self.assertRaises(ValidationError):
            validate("token-usage-event-v1.1.schema.json", usage)

        usage["billing_units"][0]["quantity"] = 1
        usage["total_tokens"] = 1
        with self.assertRaises(ValidationError):
            validate("token-usage-event-v1.1.schema.json", usage)

    def test_invalid_timestamp_and_non_http_ui_url_are_rejected(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-to-ppt.json")
        task = copy.deepcopy(plan["task"])
        task["created_at"] = "not-a-timestamp"
        with self.assertRaises(ValidationError):
            validate("main-task.schema.json", task)

        for timestamp in [
            "2026-08-20T00:00:00+08:00",
            "2026-08-19T16:00:00+00:00",
        ]:
            with self.subTest(timestamp=timestamp):
                task = copy.deepcopy(plan["task"])
                task["created_at"] = timestamp
                with self.assertRaises(ValidationError):
                    validate("main-task.schema.json", task)

        instance = copy.deepcopy(plan["instances"][0])
        instance["ui_url"] = "file:///etc/passwd"
        with self.assertRaises(ValidationError):
            validate("agent-instance.schema.json", instance)

    def test_error_response_is_strict(self) -> None:
        response = {
            "schema_version": "1.0",
            "error": {
                "code": "ADAPTER_UNAVAILABLE",
                "message": "PPT adapter is not available in Phase 1.",
                "retryable": False,
                "details": {"agent_type": "ppt"},
            },
        }
        validate("error-response.schema.json", response)
        response["error"]["unexpected"] = True
        with self.assertRaises(ValidationError):
            validate("error-response.schema.json", response)


class CatalogTests(unittest.TestCase):
    STATUS_SCHEMA_BY_DOMAIN: ClassVar[dict[str, str]] = {
        "main_task": "main-task.schema.json",
        "stage": "stage.schema.json",
        "agent_instance": "agent-instance.schema.json",
        "approval_request": "approval-request.schema.json",
        "delivery": "delivery.schema.json",
    }

    def test_credential_detection_policy_is_named_and_compilable(self) -> None:
        self.assertEqual(
            set(CREDENTIAL_POLICY),
            {
                "schema_version",
                "policy_id",
                "known_format_rules",
                "sensitive_sources",
                "sensitive_value_marker",
            },
        )
        self.assertEqual(CREDENTIAL_POLICY["schema_version"], "1.0")
        rule_ids = [
            rule["id"] for rule in CREDENTIAL_POLICY["known_format_rules"]
        ]
        self.assertEqual(len(rule_ids), len(set(rule_ids)))
        self.assertTrue(
            {
                "github_fine_grained_pat",
                "slack_token",
                "aws_access_key_id",
            }.issubset(rule_ids)
        )
        schema_rule_ids = {
            guard["$comment"]
            for guard in SCHEMA_DOCUMENTS["common.schema.json"]["$defs"][
                "credentialSafeText"
            ]["allOf"]
        }
        self.assertEqual(schema_rule_ids, set(rule_ids))
        for rule in CREDENTIAL_POLICY["known_format_rules"]:
            with self.subTest(rule=rule["id"]):
                self.assertEqual(set(rule), {"id", "pattern"})
                re.compile(rule["pattern"], re.IGNORECASE)
        source_ids = [
            source["id"] for source in CREDENTIAL_POLICY["sensitive_sources"]
        ]
        self.assertEqual(len(source_ids), len(set(source_ids)))
        self.assertEqual(set(source_ids), SENSITIVE_SOURCE_IDS)
        self.assertEqual(
            set(SENSITIVE_VALUE_POLICY),
            {
                "marker_id",
                "required_metadata",
                "raw_json_serializable",
                "public_serialization_action",
                "error_code",
            },
        )
        self.assertEqual(SENSITIVE_VALUE_POLICY["marker_id"], "sensitive_value")
        self.assertEqual(
            SENSITIVE_VALUE_POLICY["required_metadata"],
            ["source", "locator"],
        )
        self.assertFalse(SENSITIVE_VALUE_POLICY["raw_json_serializable"])
        self.assertEqual(
            SENSITIVE_VALUE_POLICY["public_serialization_action"], "reject"
        )
        self.assertEqual(SENSITIVE_VALUE_POLICY["error_code"], "VALIDATION_ERROR")

    def test_status_catalog_matches_schema_enums(self) -> None:
        catalog = load_json(CATALOGS / "status-codes.json")
        for domain, schema_name in self.STATUS_SCHEMA_BY_DOMAIN.items():
            with self.subTest(domain=domain):
                schema_statuses = set(
                    SCHEMA_DOCUMENTS[schema_name]["properties"]["status"]["enum"]
                )
                catalog_statuses = set(catalog[domain]["statuses"])
                self.assertEqual(schema_statuses, catalog_statuses)

    def test_status_transitions_are_closed_and_terminals_have_no_edges(self) -> None:
        catalog = load_json(CATALOGS / "status-codes.json")
        for domain in self.STATUS_SCHEMA_BY_DOMAIN:
            with self.subTest(domain=domain):
                section = catalog[domain]
                statuses = set(section["statuses"])
                self.assertEqual(set(section["transitions"]), statuses)
                for source, targets in section["transitions"].items():
                    self.assertTrue(
                        set(targets).issubset(statuses),
                        f"{domain}.{source} references an unknown target",
                    )
                for terminal in section["terminal"]:
                    # ARCHIVED is a reversible suspension, not business
                    # progression, so terminal states may keep exactly that
                    # one outgoing edge (main_task only).
                    edges = section["transitions"][terminal]
                    self.assertIn(edges, ([], ["ARCHIVED"]))

    def test_status_transitions_match_frozen_rfc_golden_table(self) -> None:
        catalog = load_json(CATALOGS / "status-codes.json")
        golden = load_json(GOLDEN / "status-transitions-v1.0.json")
        actual = {
            domain: {
                "terminal": catalog[domain]["terminal"],
                "transitions": catalog[domain]["transitions"],
            }
            for domain in self.STATUS_SCHEMA_BY_DOMAIN
        }
        self.assertEqual(actual, golden)

    def test_error_codes_are_unique_stable_machine_codes(self) -> None:
        catalog = load_json(CATALOGS / "error-codes.json")
        errors = catalog["errors"]
        codes = [item["code"] for item in errors]
        schema_codes = SCHEMA_DOCUMENTS["error-response.schema.json"][
            "properties"
        ]["error"]["properties"]["code"]["enum"]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(set(codes), set(schema_codes))
        self.assertTrue(
            all(re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) for code in codes)
        )
        self.assertTrue(
            {
                "SCHEMA_VERSION_UNSUPPORTED",
                "INVALID_STATE_TRANSITION",
                "ADAPTER_UNAVAILABLE",
                "BUDGET_GATE_DENIED",
                "PATH_OUTSIDE_TASK_ROOT",
                "ASSET_CORRUPTED",
            }.issubset(codes)
        )
        self.assertTrue(
            all(
                item["http_status"] in {400, 404, 409, 422, 500, 502, 503}
                for item in errors
            )
        )


class VersionCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_json(CATALOGS / "schema-versions.json")

    def test_compatibility_matrix(self) -> None:
        matrix = load_json(GOLDEN / "version-compatibility-cases.json")
        for case in matrix["cases"]:
            with self.subTest(case=case["name"]):
                accepted = schema_version_is_supported(
                    case["document_version"],
                    case["consumer_supported_versions"],
                    self.catalog["version_pattern"],
                )
                self.assertEqual(accepted, case["accepted"])
                if not accepted:
                    self.assertEqual(
                        case["error"], self.catalog["unknown_minor_version_error"]
                    )

    def test_unknown_minor_is_rejected_by_current_schema(self) -> None:
        task = load_json(PLAN_EXAMPLES / "image-only.json")["task"]
        task["schema_version"] = "1.1"
        self.assertFalse(
            schema_version_is_supported(
                task["schema_version"],
                self.catalog["supported_schema_versions"],
                self.catalog["version_pattern"],
            )
        )
        with self.assertRaises(ValidationError):
            validate("main-task.schema.json", task)

    def test_consumer_first_minor_rollout_keeps_old_examples_valid(self) -> None:
        supported_during_rollout = ["1.0", "1.1"]
        for path in sorted((CONTRACTS / "examples").rglob("*.json")):
            with self.subTest(example=path.relative_to(CONTRACTS)):
                document = load_json(path)
                self.assertTrue(
                    schema_version_is_supported(
                        document["schema_version"],
                        supported_during_rollout,
                        self.catalog["version_pattern"],
                    )
                )

    def test_state_machine_semantics_require_major_version(self) -> None:
        self.assertIn(
            "change_state_machine_semantics",
            self.catalog["major_required_changes"],
        )
        self.assertNotIn(
            "change_state_machine_semantics",
            self.catalog["minor_allowed_changes"],
        )
        self.assertEqual(self.catalog["rollout_order"], ["consumer", "producer"])
        self.assertEqual(
            self.catalog["acceptance_policy"], "exact_supported_versions"
        )


if __name__ == "__main__":
    unittest.main()
