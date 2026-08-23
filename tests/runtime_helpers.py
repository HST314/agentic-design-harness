"""Shared builders for Phase 1 runtime behavior tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harness.contracts import ContractRegistry
from harness.core.config_kernel import ConfigSnapshot
from harness.domain.commands import CommandEnvelope
from harness.domain.service import TaskCommandService
from harness.storage.atomic import atomic_write_json
from harness.storage.store import FileStateStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = PROJECT_ROOT / "contracts" / "v1"


def build_config_snapshot(
    *,
    base_url: str = "http://127.0.0.1:18000",
    api_key: str = "test-provider-secret-value",
    visual_analysis: str = "auto",
    max_files_per_task: int = 20,
    max_pdf_pages: int = 100,
    supervisor_port_start: int = 18100,
    supervisor_port_end: int = 18199,
    supervisor_startup_timeout: int = 30,
    supervisor_shutdown_grace: int = 5,
) -> ConfigSnapshot:
    return ConfigSnapshot.model_validate(
        {
            "schema_version": "1.0",
            "revision": "cfg_test_snapshot",
            "providers": {
                "schema_version": "1.0",
                "providers": {"ark": {"base_url": base_url, "api_key": api_key}},
            },
            "model_list": {
                "schema_version": "1.0",
                "text_models": [
                    {
                        "id": "ark-text-primary",
                        "label": "Test text model",
                        "provider": "ark",
                        "model": "text-model",
                        "capabilities": ["structured_output", "tool_calling"],
                        "parameters": {},
                    }
                ],
                "vlm_models": [
                    {
                        "id": "ark-vlm-primary",
                        "label": "Test vision model",
                        "provider": "ark",
                        "model": "vision-model",
                        "capabilities": ["image_input", "structured_output"],
                        "parameters": {},
                    }
                ],
                "image_models": [
                    {
                        "id": "ark-image-primary",
                        "label": "Test image model",
                        "provider": "ark",
                        "model": "image-model",
                        "capabilities": ["text_to_image", "image_to_image"],
                        "parameters": {},
                    }
                ],
            },
            "runtime": {
                "schema_version": "1.0",
                "server": {"host": "127.0.0.1", "port": 18080, "log_level": "INFO"},
                "models": {
                    "master": "ark-text-primary",
                    "text_reasoning": "ark-text-primary",
                    "vision_understanding": "ark-vlm-primary",
                    "image_generation": "ark-image-primary",
                },
                "master": {
                    "model_timeout_seconds": 10,
                    "max_tool_rounds": 4,
                    "max_clarification_questions": 3,
                    "require_plan_confirmation": True,
                },
                "document_processing": {
                    "max_files_per_task": max_files_per_task,
                    "max_total_bytes": 209715200,
                    "max_pdf_pages": max_pdf_pages,
                    "text_chunk_chars": 6000,
                    "visual_analysis": visual_analysis,
                    "require_source_citations": True,
                },
                "image_agent": {
                    "question_preference": "proactive",
                    "candidate_concurrency": 5,
                    "default_output_size": "2560x1440",
                    "response_format": "url",
                    "watermark": False,
                    "advanced_model_overrides": {
                        "intake_clarify": None,
                        "confirmation_build": None,
                        "initial_candidate_generation": None,
                        "self_check_inspection": None,
                        "self_check_rework": None,
                        "human_prompt_rework": None,
                    },
                },
                "supervisor": {
                    "port_range_start": supervisor_port_start,
                    "port_range_end": supervisor_port_end,
                    "startup_timeout_seconds": supervisor_startup_timeout,
                    "shutdown_grace_seconds": supervisor_shutdown_grace,
                },
            },
        }
    )


def build_store(root: Path) -> FileStateStore:
    contracts = ContractRegistry(CONTRACTS_ROOT)
    lock_timeout = 5.0 if os.name == "nt" else 0.25
    return FileStateStore(root / "control-data", root / "workspace", contracts, lock_timeout)


def build_service(root: Path) -> tuple[FileStateStore, TaskCommandService]:
    store = build_store(root)
    store.start()
    return store, TaskCommandService(store, store.contracts)


def register_model_call_attempt(
    store: FileStateStore,
    task_id: str,
    instance_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    """Create the authoritative record normally written by ProcessSupervisor."""
    record = {
        "attempt_id": attempt_id,
        "request_id": f"request_{attempt_id}",
        "task_id": task_id,
        "instance_id": instance_id,
        "launch_id": f"launch_{instance_id}",
        "status": "COMPLETED",
        "started_at": "2026-08-21T02:00:00Z",
        "completed_at": "2026-08-21T02:00:01Z",
    }
    path = (
        store.layout.control_root
        / "tasks"
        / task_id
        / "attempts"
        / f"{attempt_id}.json"
    )
    atomic_write_json(path, record)
    return record


def envelope(
    key: str,
    expected: int,
    actor_type: str = "human",
    actor_id: str = "tester",
) -> CommandEnvelope:
    return CommandEnvelope(
        idempotency_key=key,
        actor_type=actor_type,
        actor_id=actor_id,
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
