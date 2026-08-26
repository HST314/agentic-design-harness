"""Validated semantic PlanDrafts and deterministic PlanProposal materialization."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, NoReturn

from jsonschema import Draft202012Validator


@dataclass(frozen=True, slots=True)
class PlanDraftValidationError(ValueError):
    """A credential-safe description of an invalid model planning boundary."""

    schema: str
    path: str
    reason: str


def master_response_schema(asset_ids: list[str]) -> dict[str, Any]:
    """Build the strict, self-contained model response schema for one asset catalog."""

    input_asset_ids: dict[str, Any] = {
        "type": "array",
        "uniqueItems": True,
    }
    if asset_ids:
        input_asset_ids.update(
            {
                "maxItems": len(asset_ids),
                "items": {"type": "string", "enum": sorted(asset_ids)},
            }
        )
    else:
        input_asset_ids.update(
            {
                "maxItems": 0,
                "items": {"type": "string"},
            }
        )

    delivery = {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "role", "required", "accepted_mime_types"],
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["image", "presentation", "document", "archive", "other"],
            },
            "role": {"type": "string", "minLength": 1, "maxLength": 128},
            "required": {"type": "boolean"},
            "accepted_mime_types": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "pattern": "^[a-z0-9.+-]+/[a-z0-9.+-]+$",
                },
            },
        },
    }
    common_stage_properties = {
        "title": {"type": "string", "minLength": 1, "maxLength": 256},
        "required": {"type": "boolean"},
        "objective": {"type": "string", "minLength": 1, "maxLength": 20000},
        "instructions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {"type": "string", "minLength": 1, "maxLength": 10000},
        },
        "input_asset_ids": input_asset_ids,
        "expected_deliveries": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": delivery,
        },
    }
    required_stage_fields = [
        "type",
        "title",
        "required",
        "objective",
        "instructions",
        "input_asset_ids",
        "expected_deliveries",
        "parameters",
    ]
    image_stage = {
        "type": "object",
        "additionalProperties": False,
        "required": required_stage_fields,
        "properties": {
            **common_stage_properties,
            "type": {"const": "image"},
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "aspect_ratio",
                    "variants",
                    "usage_context",
                    "category_id",
                    "category_version",
                ],
                "properties": {
                    "aspect_ratio": {
                        "oneOf": [
                            {"type": "null"},
                            {
                                "type": "string",
                                "pattern": "^[1-9][0-9]{0,3}:[1-9][0-9]{0,3}$",
                            },
                        ]
                    },
                    "variants": {
                        "oneOf": [
                            {"type": "null"},
                            {"type": "integer", "minimum": 1, "maximum": 64},
                        ]
                    },
                    "usage_context": {
                        "oneOf": [
                            {"type": "null"},
                            {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 10000,
                            },
                        ]
                    },
                    "category_id": {
                        "oneOf": [
                            {"type": "null"},
                            {
                                "type": "string",
                                "pattern": "^[A-Za-z][A-Za-z0-9_.-]{0,127}$",
                            },
                        ]
                    },
                    "category_version": {
                        "oneOf": [
                            {"type": "null"},
                            {
                                "type": "string",
                                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
                            },
                        ]
                    },
                },
            },
        },
    }
    ppt_stage = {
        "type": "object",
        "additionalProperties": False,
        "required": required_stage_fields,
        "properties": {
            **common_stage_properties,
            "type": {"const": "ppt"},
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["slide_count", "planned_asset_role"],
                "properties": {
                    "slide_count": {
                        "oneOf": [
                            {"type": "null"},
                            {"type": "integer", "minimum": 1, "maximum": 500},
                        ]
                    },
                    "planned_asset_role": {
                        "oneOf": [
                            {"type": "null"},
                            {
                                "type": "string",
                                "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,127}$",
                            },
                        ]
                    },
                },
            },
        },
    }
    plan_draft = {
        "type": "object",
        "additionalProperties": False,
        "required": ["stages"],
        "properties": {
            "stages": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {"oneOf": [image_stage, ppt_stage]},
            }
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "message", "task_title", "plan_draft"],
        "properties": {
            "status": {"type": "string", "enum": ["NEEDS_INPUT", "PLAN_READY"]},
            "message": {"type": "string", "minLength": 1, "maxLength": 20000},
            "task_title": {
                "oneOf": [
                    {"type": "null"},
                    {"type": "string", "minLength": 1, "maxLength": 256},
                ]
            },
            "plan_draft": {"oneOf": [{"type": "null"}, plan_draft]},
        },
    }


def validate_master_response(schema: dict[str, Any], output: dict[str, Any]) -> None:
    """Re-validate provider-enforced structured output at the trust boundary."""

    errors = sorted(
        Draft202012Validator(schema).iter_errors(output),
        key=lambda item: list(item.absolute_path),
    )
    if not errors:
        return
    first = errors[0]
    path = ".".join(str(item) for item in first.absolute_path) or "$"
    raise PlanDraftValidationError("master-response", path, str(first.validator))


def materialize_plan_draft(
    task_id: str,
    revision: int,
    draft: dict[str, Any],
    *,
    created_at: str,
    asset_ids: set[str],
) -> dict[str, Any]:
    """Convert a valid semantic draft into server-owned durable planning objects."""

    stages = draft["stages"]
    if any(stage["type"] not in {"image", "ppt"} for stage in stages):
        _invalid("plan-draft", "stages", "unsupported_topology")

    proposal_id = _identifier("proposal", task_id, revision)
    materialized_stages: list[dict[str, Any]] = []
    work_items: list[dict[str, Any]] = []
    execution_cards: list[dict[str, Any]] = []
    previous_stage_id: str | None = None
    previous_stage_type: str | None = None
    previous_work_item_id: str | None = None
    for position, stage in enumerate(stages, start=1):
        referenced_assets = stage["input_asset_ids"]
        if not set(referenced_assets).issubset(asset_ids):
            _invalid(
                "plan-draft",
                f"stages.{position - 1}.input_asset_ids",
                "unknown_asset_reference",
            )
        stage_id = _identifier("stage", task_id, revision, position)
        work_item_id = _identifier("work", task_id, revision, position)
        instance_id = _identifier("instance", task_id, revision, position)
        card_id = _identifier("card", task_id, revision, position)
        stage_dependencies: list[str] = []
        work_dependencies: list[str] = []
        if (
            previous_stage_id is not None
            and previous_work_item_id is not None
            and previous_stage_type == "image"
            and stage["type"] == "ppt"
        ):
            stage_dependencies = [previous_stage_id]
            work_dependencies = [previous_work_item_id]
        materialized_stages.append(
            {
                "stage_id": stage_id,
                "type": stage["type"],
                "position": position,
                "depends_on": stage_dependencies,
                "required": stage["required"],
            }
        )
        work_items.append(
            {
                "schema_version": "1.0",
                "work_item_id": work_item_id,
                "task_id": task_id,
                "stage_id": stage_id,
                "title": stage["title"],
                "agent_type": stage["type"],
                "required": stage["required"],
                "depends_on": work_dependencies,
                "current_instance_id": instance_id,
                "instance_ids": [instance_id],
                "task_card_ids": [card_id],
            }
        )
        execution_cards.append(
            {
                "schema_version": "1.1",
                "card_id": card_id,
                "revision": 1,
                "task_id": task_id,
                "stage_id": stage_id,
                "instance_id": instance_id,
                "agent_type": stage["type"],
                "objective": stage["objective"],
                "instructions": deepcopy(stage["instructions"]),
                "input_assets": [
                    {
                        "asset_id": asset_id,
                        "manifest_relpath": f"inputs/manifests/{asset_id}.json",
                    }
                    for asset_id in referenced_assets
                ],
                "expected_deliveries": deepcopy(stage["expected_deliveries"]),
                "parameters": {
                    key: deepcopy(value)
                    for key, value in stage["parameters"].items()
                    if value is not None
                },
                "created_at": created_at,
            }
        )
        previous_stage_id = stage_id
        previous_stage_type = stage["type"]
        previous_work_item_id = work_item_id

    return {
        "schema_version": "1.0",
        "proposal_id": proposal_id,
        "task_id": task_id,
        "revision": revision,
        "status": "PENDING_CONFIRMATION",
        "stages": materialized_stages,
        "work_items": work_items,
        "execution_cards": execution_cards,
        "created_at": created_at,
        "updated_at": created_at,
        "confirmed_at": None,
    }


def _identifier(prefix: str, *parts: object) -> str:
    payload = "\0".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _invalid(schema: str, path: str, reason: str) -> NoReturn:
    raise PlanDraftValidationError(schema, path, reason)
