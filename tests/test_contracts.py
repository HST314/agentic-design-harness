from __future__ import annotations

import copy
import json
import re
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
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
SENSITIVE_PARAMETER_VALUE = re.compile(
    r"^(?:basic\s+\S+|bearer\s+\S+|sk-[a-z0-9_-]{8,}|gh[pousr]_[a-z0-9]{8,})$",
    re.IGNORECASE,
)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


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


def validate_public_parameter_values(value: Any) -> None:
    if isinstance(value, str) and SENSITIVE_PARAMETER_VALUE.fullmatch(value):
        raise SemanticContractError("public task card contains a plaintext credential")
    if isinstance(value, list):
        for item in value:
            validate_public_parameter_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            validate_public_parameter_values(item)


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
    if not dependencies_succeeded:
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

    if instance_statuses & ACTIVE_INSTANCE_STATUSES or any(
        item["status"] in {"READY", "RUNNING"} for item in required_stages
    ):
        expected = "RUNNING"
    elif required_instance_statuses & WAITING_INSTANCE_STATUSES:
        expected = "WAITING_APPROVAL"
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
        if task_status == "PARTIAL":
            has_incomplete_optional = any(
                not item["required"] and item["status"] != "SUCCEEDED"
                for item in stages
            ) or any(
                not item["required"]
                and item["status"] not in COMPLETED_INSTANCE_STATUSES
                for item in instances
            )
            if not has_incomplete_optional:
                raise SemanticContractError(
                    "partial task has no downgraded or incomplete optional child"
                )
            return
        expected = "SUCCEEDED"
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

    if not asset["relative_path"].startswith(
        f"resources/shared/{asset['asset_id']}/"
    ):
        raise SemanticContractError(
            "published asset must resolve inside its shared asset directory"
        )
    if not asset["source_relative_path"].startswith(
        f"instances/{producer}/outputs/"
    ):
        raise SemanticContractError(
            "published asset source must resolve inside producer outputs"
        )


def validate_plan_semantics(plan: dict[str, Any]) -> None:
    """Validate invariants that span multiple JSON Schema objects."""

    task = plan["task"]
    task_id = task["task_id"]
    stages = sorted(plan["stages"], key=lambda item: item["position"])
    instances = plan["instances"]
    cards = plan["task_cards"]

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

    for stage in stages:
        aggregate = expected_stage_status(stage, stage_by_id, instance_by_id)
        if stage["status"] != aggregate:
            raise SemanticContractError(
                f"stage {stage['stage_id']} status {stage['status']} "
                f"does not match aggregate {aggregate}"
            )
    validate_task_aggregate(task, stages, instances)

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
        validate_public_parameter_values(card["parameters"])
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
            "imported-asset.json": "asset-manifest.schema.json",
            "published-asset.json": "asset-manifest.schema.json",
            "published-delivery.json": "delivery.schema.json",
            "pending-approval.json": "approval-request.schema.json",
            "token-usage.json": "token-usage-event.schema.json",
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

    def test_root_contracts_use_current_schema_version(self) -> None:
        catalog = load_json(CATALOGS / "schema-versions.json")
        current = catalog["current_schema_version"]
        self.assertEqual(catalog["supported_schema_versions"], [current])
        for path in sorted((CONTRACTS / "examples").rglob("*.json")):
            with self.subTest(example=path.relative_to(CONTRACTS)):
                document = load_json(path)
                self.assertEqual(document["schema_version"], current)


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
    def test_succeeded_task_with_ready_children_is_rejected(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-only.json")
        plan["task"]["status"] = "SUCCEEDED"
        with self.assertRaisesRegex(SemanticContractError, "task status SUCCEEDED"):
            validate_plan_semantics(plan)

    def test_stage_status_must_match_instance_aggregate(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-to-ppt.json")
        plan["stages"][0]["status"] = "SUCCEEDED"
        with self.assertRaisesRegex(SemanticContractError, "stage s_visual status"):
            validate_plan_semantics(plan)

    def test_required_ppt_blocks_only_after_image_stage_finishes(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-to-ppt.json")
        plan["instances"][0]["status"] = "SUCCEEDED"
        plan["stages"][0]["status"] = "SUCCEEDED"
        plan["stages"][1]["status"] = "UNAVAILABLE"
        plan["task"]["status"] = "BLOCKED_UNAVAILABLE"
        validate_plan_semantics(plan)

        plan["task"]["status"] = "SUCCEEDED"
        with self.assertRaisesRegex(
            SemanticContractError, "does not match aggregate BLOCKED_UNAVAILABLE"
        ):
            validate_plan_semantics(plan)

    def test_running_work_has_priority_over_waiting_approval(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-only.json")
        plan["task"]["status"] = "RUNNING"
        plan["stages"][0]["status"] = "RUNNING"
        plan["instances"][0]["status"] = "WAITING_APPROVAL"
        plan["instances"][1]["status"] = "RUNNING"
        plan["instances"][2]["status"] = "SUCCEEDED"
        validate_plan_semantics(plan)

        plan["instances"][1]["status"] = "SUCCEEDED"
        plan["stages"][0]["status"] = "WAITING_APPROVAL"
        plan["task"]["status"] = "WAITING_APPROVAL"
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

    def test_task_card_parameters_reject_secrets_and_host_paths(self) -> None:
        plan = load_json(PLAN_EXAMPLES / "image-only.json")
        card = copy.deepcopy(plan["task_cards"][0])
        card["parameters"]["api_key"] = "must-not-be-public"
        with self.assertRaises(ValidationError):
            validate("task-card.schema.json", card)

        card = copy.deepcopy(plan["task_cards"][0])
        card["parameters"]["openai_api_key"] = "sk-plaintext-secret"
        with self.assertRaises(ValidationError):
            validate("task-card.schema.json", card)

        card = copy.deepcopy(plan["task_cards"][0])
        card["parameters"]["provider"] = {
            "session_token": "must-not-be-public"
        }
        with self.assertRaises(ValidationError):
            validate("task-card.schema.json", card)

        secret_plan = copy.deepcopy(plan)
        secret_plan["task_cards"][0]["parameters"]["model_hint"] = (
            "sk-plaintext-secret"
        )
        with self.assertRaisesRegex(SemanticContractError, "plaintext credential"):
            validate_plan_semantics(secret_plan)

        card = copy.deepcopy(plan["task_cards"][0])
        card["parameters"]["author_name"] = "design-team"
        validate("task-card.schema.json", card)

        card = copy.deepcopy(plan["task_cards"][0])
        card["parameters"]["output_dir"] = "/etc"
        with self.assertRaises(ValidationError):
            validate("task-card.schema.json", card)

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
    STATUS_SCHEMA_BY_DOMAIN = {
        "main_task": "main-task.schema.json",
        "stage": "stage.schema.json",
        "agent_instance": "agent-instance.schema.json",
        "approval_request": "approval-request.schema.json",
        "delivery": "delivery.schema.json",
    }

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
                    self.assertEqual(section["transitions"][terminal], [])

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
                item["http_status"] in {400, 404, 409, 422, 500}
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
