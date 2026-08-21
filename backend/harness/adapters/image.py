"""Runnable Image Agent adapter over its process and HTTP boundaries."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from itertools import pairwise
from pathlib import Path
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from ..contracts import ContractRegistry
from ..core.errors import HarnessError
from ..services.assets import AssetService
from ..services.configuration import ConfigurationService
from ..services.process_runtime import AgentRuntimeArtifact, ProcessSpec
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.paths import normalized_relative_path
from ..storage.store import FileStateStore
from .base import (
    AdapterCommandResult,
    AdapterObservation,
    AdapterRecoveryResult,
    PrepareRequest,
    ValidationResult,
)
from .image_delivery import stage_final_delivery
from .image_runtime import (
    IMAGE_ENTRYPOINT,
    IMAGE_WEB_REQUIREMENTS,
    ImageRuntimeBuilder,
)
from .image_workflow import (
    HARNESS_CAPABILITIES,
    KNOWN_CAPABILITIES,
    RUNNING_PHASES,
    WAITING_PHASES,
    approval_context,
    map_advance_payload,
)
from .image_workflow import (
    normalized_capabilities as normalize_workflow_capabilities,
)

SUPPORTED_IMAGE_AGENT_REVISION = "61c5b4f1b66d5d85f62b39b5b338ac2304e94d26"
SUPPORTED_IMAGE_AGENT_PACKAGE_VERSION = "1.7.8"
SUPPORTED_IMAGE_API_MAJOR = "1"
_SUPPORTED_IMAGE_RUNTIME_ATTESTATIONS = {
    SUPPORTED_IMAGE_AGENT_REVISION: {
        "source_content_sha256": (
            "f50108651d00454916d2f58aae583cf60d6ac65ded3881ec6bbb5323bf8dc047"
        ),
        "dependency_content_sha256": (
            "1bb3aace0b0ade79ae43f32bbf65551acec8de0e090e75d8cd5173ab74b969bb"
        ),
    }
}

_PACKAGE_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
_JOB_ID = re.compile(r"^job_[A-Za-z0-9]+$")
_JOB_STATES = frozenset(
    {"queued", "running", "cancelling", "succeeded", "failed", "cancelled", "interrupted"}
)
_ACTIVE_JOB_STATES = frozenset({"queued", "running", "cancelling"})
_FAILED_JOB_STATES = frozenset({"failed", "cancelled", "interrupted"})
_REQUIRED_ROUTES = frozenset(
    {
        "/api/health",
        "/api/jobs/{job_id}",
        "/api/projects",
        "/api/projects/{project_id}",
        "/api/projects/{project_id}/delivery/finalize",
        "/api/projects/{project_id}/jobs",
        "/api/projects/{project_id}/policy",
        "/api/projects/{project_id}/timeline",
        "/api/settings/models",
    }
)
_REQUIRED_ROUTE_METHODS = (
    ("/api/health", "get"),
    ("/api/jobs/{job_id}", "get"),
    ("/api/jobs/{job_id}/cancel", "post"),
    ("/api/projects", "post"),
    ("/api/projects/{project_id}", "get"),
    ("/api/projects/{project_id}/delivery/finalize", "post"),
    ("/api/projects/{project_id}/jobs", "post"),
    ("/api/projects/{project_id}/policy", "post"),
    ("/api/projects/{project_id}/timeline", "get"),
    ("/api/settings/models", "get"),
    ("/api/settings/models", "post"),
)


class ImageAgentAdapter:
    """Translate frozen Harness contracts to the Image Agent's local HTTP API."""

    agent_type = "image"
    available = True

    def __init__(
        self,
        store: FileStateStore,
        contracts: ContractRegistry,
        assets: AssetService,
        configuration: ConfigurationService,
        *,
        source_root: Path,
        interpreter: Path,
        dependency_root: Path,
        revision: str = SUPPORTED_IMAGE_AGENT_REVISION,
        host: str = "127.0.0.1",
        request_timeout_seconds: float = 5.0,
    ) -> None:
        self.store = store
        self.contracts = contracts
        self.assets = assets
        self.configuration = configuration
        self.source_root = source_root
        self.interpreter = interpreter
        self.dependency_root = dependency_root
        self.revision = revision
        self.host = host
        self.request_timeout_seconds = request_timeout_seconds
        attestation = _SUPPORTED_IMAGE_RUNTIME_ATTESTATIONS.get(revision, {})
        self.runtime_builder = ImageRuntimeBuilder(
            source_root,
            dependency_root,
            revision=revision,
            package_version=SUPPORTED_IMAGE_AGENT_PACKAGE_VERSION,
            source_content_sha256=attestation.get("source_content_sha256", ""),
            dependency_content_sha256=attestation.get("dependency_content_sha256", ""),
        )

    def validate_task_card(self, card: dict[str, Any]) -> ValidationResult:
        try:
            self.contracts.validate("task-card-v1.1", card)
        except (HarnessError, ValueError) as exc:
            return ValidationResult(valid=False, errors=(str(exc),))
        errors: list[str] = []
        if card.get("agent_type") != self.agent_type:
            errors.append("Task card agent_type must be image.")
        parameters = card.get("parameters", {})
        if not parameters.get("usage_context"):
            errors.append("Image TaskCard 1.1 requires parameters.usage_context.")
        if bool(parameters.get("category_id")) != bool(parameters.get("category_version")):
            errors.append("Image category_id and category_version must be supplied together.")
        if not card.get("input_assets"):
            errors.append("Image Agent requires at least one verified source asset.")
        required_images = [
            item
            for item in card.get("expected_deliveries", [])
            if item.get("required") and item.get("kind") == "image"
        ]
        if len(required_images) != 1:
            errors.append("Image Agent requires exactly one required final image delivery.")
        return ValidationResult(valid=not errors, errors=tuple(errors))

    def prepare(self, request: PrepareRequest) -> ProcessSpec:
        validation = self.validate_task_card(request.task_card)
        if not validation.valid:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The Image Agent adapter rejected its task card.",
                {"errors": list(validation.errors)},
            )
        self._validate_runtime_source()
        instance_id = str(request.instance["instance_id"])
        task_id = str(request.instance["task_id"])
        instance_root = self.store.layout.initialize_instance(task_id, instance_id)
        self.configuration.create_instance_snapshot(task_id, instance_id)
        image_card = self.map_task_card(request)
        runtime_root = instance_root / "runtime"
        atomic_write_json(runtime_root / "image-task-card.json", image_card, mode=0o640)
        state_path = self._state_path(task_id, instance_id)
        expected_state = {
            "schema_version": "1.0",
            "task_id": task_id,
            "instance_id": instance_id,
            "task_card_sha256": digest_json(image_card),
            "source_revision": self.revision,
            "project_created": False,
            "operation_id": None,
            "job_id": None,
            "timeline_cursor": 0,
        }
        if state_path.exists():
            current = read_json(state_path)
            immutable = ("task_id", "instance_id", "task_card_sha256", "source_revision")
            if any(current.get(key) != expected_state[key] for key in immutable):
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The prepared Image runtime no longer matches its pinned task card.",
                    {"instance_id": instance_id},
                )
        else:
            atomic_write_json(state_path, expected_state)
        artifact_root = self.runtime_builder.prepare(runtime_root)
        entrypoint = artifact_root / IMAGE_ENTRYPOINT
        return ProcessSpec(
            command=(
                str(self.interpreter),
                str(entrypoint),
                "--host",
                "{host}",
                "--port",
                "{port}",
            ),
            runtime_artifact=AgentRuntimeArtifact(
                artifact_id="image-agent-mvp",
                revision=self.revision,
                source_root=artifact_root,
                entrypoint_relpath=IMAGE_ENTRYPOINT,
                dependency_lock_relpaths=(
                    "pyproject.toml",
                    "requirements.lock",
                    IMAGE_WEB_REQUIREMENTS,
                ),
                environment_root=self.interpreter.parent.parent,
            ),
            public_environment={
                "IMAGE_AGENT_FRONT_PROJECTS_ROOT": str(instance_root / "work"),
                "IMAGE_AGENT_MODEL_LIBRARY": str(artifact_root / "configs" / "model_library.yaml"),
                "PYTHONPATH": str(artifact_root / "_dependencies"),
            },
            health_path="/api/health",
            readiness_path="/api/health",
            ui_path="/",
        )

    def map_task_card(self, request: PrepareRequest) -> dict[str, Any]:
        """Build the strict, lossless ImageTaskCard described by ADR 0002."""

        card = request.task_card
        task_id = str(card["task_id"])
        manifests: list[dict[str, Any]] = []
        for reference in card["input_assets"]:
            manifest = self.assets.verify_asset(task_id, str(reference["asset_id"]))
            expected_manifest = (
                f"inputs/manifests/{manifest['asset_id']}.json"
                if manifest["producer_instance_id"] is None
                else f"resources/manifests/{manifest['asset_id']}.json"
            )
            if reference["manifest_relpath"] != expected_manifest:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "An Image input references a non-authoritative asset manifest.",
                    {"asset_id": manifest["asset_id"]},
                )
            manifests.append(manifest)
        instructions = list(card["instructions"])
        image_parameters = {
            key: deepcopy(value)
            for key, value in card["parameters"].items()
            if key in {"aspect_ratio", "variants"}
        }
        category_ref = None
        if card["parameters"].get("category_id"):
            category_ref = {
                "category_id": card["parameters"]["category_id"],
                "version": card["parameters"]["category_version"],
            }
        source_refs = [
            {
                "ref_id": manifest["asset_id"],
                "ref_type": manifest["kind"],
                "excerpt": manifest["description"] or None,
                "source_hash": manifest["sha256"],
            }
            for manifest in manifests
        ]
        asset_inputs = []
        for manifest in manifests:
            usage_rule = " ".join(
                part for part in (manifest["description"].strip(), *instructions) if part
            )
            if not usage_rule:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "Every Image input requires an explicit non-empty usage rule.",
                    {"asset_id": manifest["asset_id"]},
                )
            asset_inputs.append(
                {
                    "asset_id": manifest["asset_id"],
                    "asset_type": manifest["kind"],
                    "usage_rule": usage_rule,
                    "verified": True,
                }
            )
        mapped = {
            "task_id": card["instance_id"],
            "project_id": card["instance_id"],
            "parent_task_id": card["task_id"],
            "source_refs": source_refs,
            "deliverable_goal": card["objective"],
            "usage_context": card["parameters"]["usage_context"],
            "category_ref": category_ref,
            "known_facts": {
                "harness_instructions": instructions,
                "harness_parameters": image_parameters,
            },
            "unknowns": {},
            "asset_inputs": asset_inputs,
            "status": "draft",
        }
        self._validate_image_contract(mapped)
        return mapped

    def start(self, instance_id: str, operation_id: str) -> AdapterCommandResult:
        task_id = self._task_id_for_instance(instance_id)
        state = self._state(task_id, instance_id)
        base_url = self._base_url(task_id, instance_id)
        compatibility = self._check_compatibility(base_url)
        runtime_config = read_json(
            self.store.layout.initialize_instance(task_id, instance_id) / "runtime-config.json"
        )
        offline_mode = runtime_config["config"]["image_runtime_policy"]["offline_mode"]
        card = read_json(
            self.store.layout.initialize_instance(task_id, instance_id)
            / "runtime"
            / "image-task-card.json"
        )
        if not state["project_created"]:
            existing = self._request(
                base_url, "GET", f"/api/projects/{instance_id}", allow_404=True
            )
            if existing is None:
                self._request(
                    base_url,
                    "POST",
                    "/api/projects",
                    {
                        "project_id": instance_id,
                        "task_card": card,
                        "offline": offline_mode,
                        "defer_run": True,
                    },
                    expected_statuses=(201,),
                )
            state["project_created"] = True
            self._write_state(task_id, instance_id, state)
        job = self._request(
            base_url,
            "POST",
            f"/api/projects/{instance_id}/jobs",
            {"idempotency_key": operation_id},
            expected_statuses=(200, 202),
        )
        job = self._require_job(job)
        state.update(
            {
                "operation_id": operation_id,
                "job_id": job["job_id"],
                "compatibility": compatibility,
            }
        )
        self._write_state(task_id, instance_id, state)
        return AdapterCommandResult(
            accepted=True,
            operation_id=operation_id,
            details={"job_id": job["job_id"], "job_status": job["status"]},
        )

    def stop(self, instance_id: str, reason: str, operation_id: str) -> AdapterCommandResult:
        validate_identifier(operation_id, "operation_id")
        if not reason or len(reason) > 256 or "\x00" in reason:
            raise HarnessError("VALIDATION_ERROR", "The stop reason is invalid.")
        task_id = self._task_id_for_instance(instance_id)
        state = self._state(task_id, instance_id)
        job_id = state.get("job_id")
        job_status = None
        if isinstance(job_id, str):
            base_url = self._base_url(task_id, instance_id)
            current = self._request(base_url, "GET", f"/api/jobs/{job_id}")
            current = self._require_job(current)
            job_status = current["status"]
            if job_status in _ACTIVE_JOB_STATES:
                cancelled = self._request(
                    base_url,
                    "POST",
                    f"/api/jobs/{job_id}/cancel",
                    {},
                )
                cancelled = self._require_job(cancelled)
                job_status = cancelled["status"]
        state.update(
            {
                "stop_operation_id": operation_id,
                "stop_reason": reason,
                "job_status_at_stop": job_status,
            }
        )
        self._write_state(task_id, instance_id, state)
        return AdapterCommandResult(
            accepted=True,
            operation_id=operation_id,
            details={"job_id": job_id, "job_status": job_status},
        )

    def get_status(self, instance_id: str) -> AdapterObservation:
        task_id = self._task_id_for_instance(instance_id)
        state = self._state(task_id, instance_id)
        base_url = self._base_url(task_id, instance_id)
        job = None
        if state.get("job_id"):
            job = self._request(base_url, "GET", f"/api/jobs/{state['job_id']}")
            job = self._require_job(job)
        timeline = self._request(
            base_url,
            "GET",
            f"/api/projects/{instance_id}/timeline?"
            f"{urlencode({'after': state['timeline_cursor'], 'limit': 100})}",
        )
        cursor = self._validate_timeline(timeline, int(state["timeline_cursor"]))
        state["timeline_cursor"] = cursor
        view = self._request(base_url, "GET", f"/api/projects/{instance_id}")
        if view is None:
            self._protocol_error("Image Agent returned an empty project response.")
        observation = self._observation(view, job, cursor, state.get("compatibility"))
        state["last_observation"] = {
            "status": observation.status,
            "step_id": observation.step_id,
            "capabilities": list(observation.capabilities),
            "details": deepcopy(observation.details),
        }
        self._write_state(task_id, instance_id, state)
        return observation

    def request_advance(
        self,
        instance_id: str,
        action: str,
        payload: dict[str, Any],
        operation_id: str,
    ) -> AdapterCommandResult:
        if action not in HARNESS_CAPABILITIES:
            raise HarnessError("VALIDATION_ERROR", "Unknown Image Agent capability.")
        task_id = self._task_id_for_instance(instance_id)
        observation = self.get_status(instance_id)
        if observation.status != "WAITING_APPROVAL" or action not in observation.capabilities:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "The Image action is not available at the current workflow step.",
                {"instance_id": instance_id, "capability": action},
            )
        request_payload = map_advance_payload(action, payload)
        request_payload["idempotency_key"] = operation_id
        base_url = self._base_url(task_id, instance_id)
        job = self._request(
            base_url,
            "POST",
            f"/api/projects/{instance_id}/jobs",
            request_payload,
            expected_statuses=(200, 202),
        )
        job = self._require_job(job)
        state = self._state(task_id, instance_id)
        state.update({"operation_id": operation_id, "job_id": job["job_id"]})
        self._write_state(task_id, instance_id, state)
        return AdapterCommandResult(
            accepted=True,
            operation_id=operation_id,
            details={"job_id": job["job_id"], "job_status": job["status"]},
        )

    def apply_config(
        self,
        instance_id: str,
        config: dict[str, Any],
        revision: int,
        operation_id: str,
    ) -> AdapterCommandResult:
        validate_identifier(operation_id, "operation_id")
        task_id = self._task_id_for_instance(instance_id)
        base_url = self._base_url(task_id, instance_id)
        self._check_compatibility(base_url)
        files = self.configuration.image_runtime_files(config)
        policy = files["runtime.yaml"]
        target_bindings = {
            item["state"]: item for item in files["model-config.yaml"]["state_bindings"]
        }
        settings = self._request(base_url, "GET", "/api/settings/models")
        updates: dict[str, str] = {}
        if not isinstance(settings, dict):
            self._protocol_error("Image Agent model settings response is malformed.")
        library = settings.get("library")
        states = settings.get("states")
        if not isinstance(library, dict) or not isinstance(states, list):
            self._protocol_error("Image Agent model settings response is malformed.")
        for state in states:
            if not isinstance(state, dict) or not isinstance(state.get("state"), str):
                self._protocol_error("Image Agent model settings response is malformed.")
            target = target_bindings.get(state["state"])
            if target is None:
                continue
            current = state.get("binding")
            if (
                isinstance(current, dict)
                and current.get("provider") == target["provider"]
                and current.get("model") == target["model"]
            ):
                continue
            group = state.get("group")
            options = library.get(group) if isinstance(group, str) else None
            if not isinstance(options, list):
                self._protocol_error("Image Agent model library response is malformed.")
            match = next(
                (
                    item
                    for item in options
                    if isinstance(item, dict)
                    and item.get("provider") == target["provider"]
                    and item.get("model") == target["model"]
                    and isinstance(item.get("id"), str)
                ),
                None,
            )
            if match is None:
                return AdapterCommandResult(
                    accepted=False,
                    operation_id=operation_id,
                    details={
                        "config_revision": revision,
                        "restart_required": True,
                        "reason": "MODEL_BINDING_NOT_HOT_APPLICABLE",
                        "state": state["state"],
                    },
                )
            updates[state["state"]] = match["id"]
        # Complete the model preflight before the first mutation. A binding
        # that cannot be hot-applied must not leave only the policy half of the
        # requested configuration active.
        self._request(
            base_url,
            "POST",
            f"/api/projects/{instance_id}/policy",
            {
                "policy": policy,
                "actor": "harness_config_service",
                "confirmed": True,
            },
        )
        if updates:
            self._request(
                base_url,
                "POST",
                "/api/settings/models",
                {
                    "bindings": updates,
                    "actor": "harness_config_service",
                    "confirmed": True,
                },
            )
        state = self._state(task_id, instance_id)
        state["config_revision"] = revision
        state["config_operation_id"] = operation_id
        self._write_state(task_id, instance_id, state)
        return AdapterCommandResult(
            accepted=True,
            operation_id=operation_id,
            details={
                "config_revision": revision,
                "restart_required": False,
                "updated_model_bindings": sorted(updates),
            },
        )

    def collect_deliveries(self, instance_id: str) -> list[dict[str, Any]]:
        task_id = self._task_id_for_instance(instance_id)
        base_url = self._base_url(task_id, instance_id)
        view = self._request(base_url, "GET", f"/api/projects/{instance_id}")
        snapshot = None if view is None else view.get("snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("completed") is not True:
            return []
        envelope = snapshot.get("delivery_envelope")
        if not isinstance(envelope, dict):
            self._protocol_error("Image Agent completed without a delivery envelope.")
        final_image = envelope.get("final_image")
        if (
            not isinstance(final_image, dict)
            or not isinstance(final_image.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", final_image["sha256"]) is None
        ):
            self._protocol_error("Image Agent returned an invalid delivery envelope.")
        marker = self._request(
            base_url,
            "POST",
            f"/api/projects/{instance_id}/delivery/finalize",
        )
        files = None if marker is None else marker.get("files")
        if (
            not isinstance(marker, dict)
            or marker.get("finalized") is not True
            or marker.get("asset_sha256") != final_image["sha256"]
            or not isinstance(files, dict)
            or not isinstance(files.get("image"), str)
        ):
            self._protocol_error("Image Agent returned an invalid finalized delivery marker.")
        relative = normalized_relative_path(files["image"])
        if len(relative.parts) != 2 or relative.parts[0] != "delivery":
            self._protocol_error("Image Agent delivery path escaped its delivery directory.")
        instance_root = self.store.layout.initialize_instance(task_id, instance_id)
        project_root = instance_root / "work" / instance_id
        output_name = relative.name
        output = instance_root / "outputs" / output_name
        stage_final_delivery(
            project_root,
            relative,
            output,
            expected_sha256=final_image["sha256"],
        )
        plan = self.store.plan.get(task_id, task_id)
        if plan is None:
            raise HarnessError("VALIDATION_ERROR", "The Image instance has no task plan.")
        card = next(
            (item for item in plan["task_cards"] if item["instance_id"] == instance_id),
            None,
        )
        if card is None:
            raise HarnessError("VALIDATION_ERROR", "The Image instance has no task card.")
        expected = [
            item
            for item in card["expected_deliveries"]
            if item["required"] and item["kind"] == "image"
        ]
        if len(expected) != 1:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The Image delivery cannot be mapped to one required final image role.",
            )
        note = envelope.get("design_note")
        description = "Image Agent verified final artwork."
        if isinstance(note, dict) and isinstance(note.get("task_fit"), str):
            description = note["task_fit"][:4000] or description
        return [
            {
                "source_relative_path": (f"instances/{instance_id}/outputs/{output_name}"),
                "kind": "image",
                "role": expected[0]["role"],
                "description": description,
                "sha256": final_image["sha256"],
            }
        ]

    def collect_usage(self, instance_id: str, cursor: str | None) -> list[dict[str, Any]]:
        # The pinned Image Agent does not expose provider usage yet. Returning
        # an empty observation is deliberate: UsageService persists
        # NOT_REPORTED instead of manufacturing a zero-Token event.
        self._task_id_for_instance(instance_id)
        return []

    def get_ui_url(self, instance_id: str) -> str | None:
        task_id = self._task_id_for_instance(instance_id)
        instance = self.store.instance.get(task_id, instance_id)
        return None if instance is None else instance.get("ui_url")

    def recover(self, instance_snapshot: dict[str, Any]) -> AdapterRecoveryResult:
        instance_id = str(instance_snapshot["instance_id"])
        task_id = str(instance_snapshot["task_id"])
        if not self._state_path(task_id, instance_id).exists():
            return AdapterRecoveryResult(recovered=False, status="FAILED")
        status = str(instance_snapshot["status"])
        if status not in {"STARTING", "RUNNING", "WAITING_APPROVAL"}:
            return AdapterRecoveryResult(recovered=True, status=status)
        observation = self.get_status(instance_id)
        return AdapterRecoveryResult(
            recovered=True,
            status=observation.status,
            details={
                "step_id": observation.step_id,
                "capabilities": list(observation.capabilities),
                "compatibility": observation.details.get("compatibility"),
            },
        )

    def _observation(
        self,
        view: dict[str, Any],
        job: dict[str, Any] | None,
        timeline_cursor: int,
        compatibility: dict[str, Any] | None,
    ) -> AdapterObservation:
        job_status = None if job is None else job["status"]
        details: dict[str, Any] = {
            "job_id": None if job is None else job["job_id"],
            "job_status": job_status,
            "timeline_cursor": timeline_cursor,
            "compatibility": compatibility,
        }
        manifest = view.get("manifest")
        snapshot = view.get("snapshot")
        capabilities = view.get("capabilities")
        if (
            not isinstance(manifest, dict)
            or not isinstance(snapshot, dict)
            or not isinstance(capabilities, list)
        ):
            return self._compatibility_failure(details, "Image project response is malformed.")
        failed_step = manifest.get("failed_step")
        if failed_step is not None and not isinstance(failed_step, dict):
            return self._compatibility_failure(details, "Image project manifest is malformed.")
        if any(
            not isinstance(item, str) or item not in KNOWN_CAPABILITIES for item in capabilities
        ) or len(capabilities) != len(set(capabilities)):
            return self._compatibility_failure(
                details, "Image Agent returned an unknown capability or duplicate capability."
            )
        if snapshot and (
            ("waiting" in snapshot and type(snapshot["waiting"]) is not bool)
            or ("completed" in snapshot and type(snapshot["completed"]) is not bool)
        ):
            return self._compatibility_failure(
                details, "Image Agent returned a malformed snapshot."
            )
        phase = snapshot.get("phase")
        if snapshot and (
            not isinstance(phase, str) or phase not in WAITING_PHASES | RUNNING_PHASES
        ):
            return self._compatibility_failure(details, "Image Agent returned an unknown phase.")
        if not snapshot and capabilities:
            return self._compatibility_failure(
                details, "An empty Image snapshot published capabilities."
            )
        if snapshot:
            state_name = snapshot.get("state")
            if state_name is not None and not isinstance(state_name, str):
                return self._compatibility_failure(
                    details, "Image Agent returned a malformed workflow state."
                )
            normalized_capabilities = normalize_workflow_capabilities(snapshot, capabilities)
            normalized_capabilities = tuple(
                item for item in normalized_capabilities if item in HARNESS_CAPABILITIES
            )
            details.update(
                {
                    "phase": phase,
                    "state": state_name,
                    "completed": snapshot.get("completed", False),
                    "approval_context": approval_context(snapshot),
                }
            )
        else:
            normalized_capabilities = tuple(capabilities)
        if job_status == "succeeded" and not snapshot:
            return self._compatibility_failure(
                details,
                "A succeeded Image job did not publish a non-empty snapshot.",
            )
        if job_status in _ACTIVE_JOB_STATES:
            return AdapterObservation(status="RUNNING", details=details)
        if job is not None and job_status in _FAILED_JOB_STATES:
            details["error"] = deepcopy(job.get("error"))
            return AdapterObservation(status="FAILED", details=details)
        if failed_step:
            details["error"] = deepcopy(failed_step)
            return AdapterObservation(status="FAILED", details=details)
        if not snapshot:
            return AdapterObservation(status="RUNNING", details=details)
        if snapshot.get("completed") is True:
            return AdapterObservation(
                status="RUNNING",
                step_id=phase,
                capabilities=(),
                details=details,
            )
        if phase in WAITING_PHASES:
            if snapshot.get("waiting") is not True or not normalized_capabilities:
                return self._compatibility_failure(
                    details,
                    "A waiting Image phase did not publish its waiting flag and capability.",
                )
            return AdapterObservation(
                status="WAITING_APPROVAL",
                step_id=phase,
                capabilities=normalized_capabilities,
                details=details,
            )
        if snapshot.get("waiting") is True:
            return self._compatibility_failure(
                details, "A running Image phase unexpectedly published a waiting flag."
            )
        if phase in {"candidate_generation_completed", "calibration_completed"}:
            if not normalized_capabilities:
                return self._compatibility_failure(
                    details, "An Image decision phase did not publish a legal action."
                )
            return AdapterObservation(
                status="WAITING_APPROVAL",
                step_id=phase,
                capabilities=normalized_capabilities,
                details=details,
            )
        return AdapterObservation(
            status="RUNNING",
            step_id=phase,
            capabilities=normalized_capabilities,
            details=details,
        )

    @staticmethod
    def _compatibility_failure(details: dict[str, Any], message: str) -> AdapterObservation:
        return AdapterObservation(
            status="FAILED",
            details={**details, "compatibility_error": message},
        )

    def _check_compatibility(self, base_url: str) -> dict[str, Any]:
        document = self._request(base_url, "GET", "/openapi.json")
        if not isinstance(document, dict):
            self._protocol_error("Image Agent OpenAPI metadata is malformed.")
        info = document.get("info")
        paths = document.get("paths")
        components_root = document.get("components")
        if (
            not isinstance(info, dict)
            or not isinstance(paths, dict)
            or not isinstance(components_root, dict)
        ):
            self._protocol_error("Image Agent OpenAPI metadata is malformed.")
        components = components_root.get("schemas")
        if not isinstance(components, dict):
            self._protocol_error("Image Agent OpenAPI metadata is malformed.")
        api_version = info.get("version")
        create_schema = components.get("CreateProjectRequest")
        advance_schema = components.get("AdvanceRequest")
        required_routes_valid = all(
            isinstance(paths.get(route), dict) and isinstance(paths[route].get(method), dict)
            for route, method in _REQUIRED_ROUTE_METHODS
        )
        if not isinstance(create_schema, dict) or not isinstance(advance_schema, dict):
            self._protocol_error("Image Agent OpenAPI metadata is malformed.")
        create_properties = create_schema.get("properties")
        advance_properties = advance_schema.get("properties")
        if not isinstance(create_properties, dict) or not isinstance(advance_properties, dict):
            self._protocol_error("Image Agent OpenAPI metadata is malformed.")
        if (
            not isinstance(api_version, str)
            or api_version.split(".", 1)[0] != SUPPORTED_IMAGE_API_MAJOR
            or not _REQUIRED_ROUTES.issubset(paths)
            or not required_routes_valid
            or "defer_run" not in create_properties
            or "idempotency_key" not in advance_properties
        ):
            self._protocol_error("Image Agent API version or capabilities are unsupported.")
        return {
            "api_version": api_version,
            "source_revision": self.revision,
            "package_version": SUPPORTED_IMAGE_AGENT_PACKAGE_VERSION,
        }

    def _request(
        self,
        base_url: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected_statuses: tuple[int, ...] = (200,),
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{base_url}{path}",
            data=data,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.request_timeout_seconds) as response:
                if response.status not in expected_statuses:
                    self._protocol_error("Image Agent returned an unexpected HTTP status.")
                content_type = response.headers.get_content_type()
                raw = response.read(8 * 1024 * 1024 + 1)
        except HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            raise HarnessError(
                "PROCESS_START_FAILED",
                "Image Agent rejected a local adapter request.",
                {"http_status": exc.code, "path": path.split("?", 1)[0]},
            ) from None
        except (OSError, TimeoutError, URLError):
            raise HarnessError(
                "PROCESS_START_FAILED",
                "Image Agent did not answer a local adapter request.",
                {"path": path.split("?", 1)[0]},
            ) from None
        if len(raw) > 8 * 1024 * 1024 or content_type != "application/json":
            self._protocol_error("Image Agent returned an unsafe response body.")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._protocol_error("Image Agent returned invalid JSON.")
        if not isinstance(value, dict):
            self._protocol_error("Image Agent returned a non-object response.")
        return value

    @staticmethod
    def _require_job(job: dict[str, Any] | None) -> dict[str, Any]:
        if (
            not isinstance(job, dict)
            or not isinstance(job.get("job_id"), str)
            or _JOB_ID.fullmatch(job["job_id"]) is None
            or not isinstance(job.get("status"), str)
            or job["status"] not in _JOB_STATES
        ):
            ImageAgentAdapter._protocol_error("Image Agent returned an invalid job record.")
        for field in (
            "project_id",
            "operation",
            "idempotency_key",
            "created_at",
            "started_at",
            "finished_at",
        ):
            if field in job and not isinstance(job[field], str):
                ImageAgentAdapter._protocol_error("Image Agent returned an invalid job record.")
        error = job.get("error")
        if error is not None and (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), str)
            or not isinstance(error.get("message"), str)
            or ("category" in error and not isinstance(error["category"], str))
        ):
            ImageAgentAdapter._protocol_error("Image Agent returned an invalid job record.")
        if job["status"] in {"failed", "interrupted"} and error is None:
            ImageAgentAdapter._protocol_error("Image Agent returned an invalid job record.")
        if job["status"] == "succeeded" and not isinstance(job.get("result"), dict):
            ImageAgentAdapter._protocol_error("Image Agent returned an invalid job record.")
        if "cancellation_requested" in job and type(job["cancellation_requested"]) is not bool:
            ImageAgentAdapter._protocol_error("Image Agent returned an invalid job record.")
        events = job.get("events")
        if events is not None:
            if not isinstance(events, list):
                ImageAgentAdapter._protocol_error("Image Agent returned an invalid job record.")
            previous_sequence = 0
            for event in events:
                if (
                    not isinstance(event, dict)
                    or type(event.get("seq")) is not int
                    or event["seq"] <= previous_sequence
                    or not isinstance(event.get("type"), str)
                    or not isinstance(event.get("timestamp"), str)
                ):
                    ImageAgentAdapter._protocol_error("Image Agent returned an invalid job record.")
                previous_sequence = event["seq"]
        return job

    @staticmethod
    def _validate_timeline(timeline: dict[str, Any] | None, previous: int) -> int:
        if (
            not isinstance(timeline, dict)
            or not isinstance(timeline.get("items"), list)
            or type(timeline.get("next_cursor")) is not int
            or type(timeline.get("has_more")) is not bool
            or len(timeline.get("items", [])) > 100
        ):
            ImageAgentAdapter._protocol_error("Image Agent returned an invalid timeline page.")
        sequences: list[int] = []
        for item in timeline["items"]:
            if (
                not isinstance(item, dict)
                or type(item.get("sequence")) is not int
                or not isinstance(item.get("type"), str)
                or not isinstance(item.get("timestamp"), str)
            ):
                ImageAgentAdapter._protocol_error("Image Agent returned an invalid timeline page.")
            sequences.append(item["sequence"])
        cursor = timeline["next_cursor"]
        if (
            any(sequence <= previous for sequence in sequences)
            or any(current <= prior for prior, current in pairwise(sequences))
            or cursor < previous
            or cursor != (sequences[-1] if sequences else previous)
            or (timeline["has_more"] and not sequences)
        ):
            ImageAgentAdapter._protocol_error("Image Agent timeline cursor is invalid.")
        return cursor

    def _validate_runtime_source(self) -> None:
        if self.revision != SUPPORTED_IMAGE_AGENT_REVISION:
            raise HarnessError(
                "SCHEMA_VERSION_UNSUPPORTED",
                "The configured Image Agent revision is not supported by this Harness build.",
            )
        if (
            not self.source_root.is_absolute()
            or not self.source_root.is_dir()
            or self.source_root.is_symlink()
            or not self.interpreter.is_absolute()
            or not self.interpreter.is_file()
            or not self.dependency_root.is_absolute()
            or not self.dependency_root.is_dir()
            or self.dependency_root.is_symlink()
        ):
            raise HarnessError(
                "ADAPTER_UNAVAILABLE",
                "The Image Agent source or isolated interpreter is not configured.",
            )
        pyproject = self.source_root / "pyproject.toml"
        try:
            match = _PACKAGE_VERSION.search(pyproject.read_text(encoding="utf-8"))
        except OSError:
            match = None
        if match is None or match.group(1) != SUPPORTED_IMAGE_AGENT_PACKAGE_VERSION:
            raise HarnessError(
                "SCHEMA_VERSION_UNSUPPORTED",
                "The configured Image Agent package version is unsupported.",
            )

    def _validate_image_contract(self, card: dict[str, Any]) -> None:
        schema_path = self.source_root / "schemas" / "ImageTaskCard.schema.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(card)
        except (OSError, json.JSONDecodeError, SchemaError, ValidationError):
            raise HarnessError(
                "VALIDATION_ERROR", "The mapped ImageTaskCard failed consumer validation."
            ) from None
        if not card["source_refs"]:
            raise HarnessError(
                "VALIDATION_ERROR", "The mapped ImageTaskCard requires a source reference."
            )

    def _task_id_for_instance(self, instance_id: str) -> str:
        validate_identifier(instance_id, "instance_id")
        matches = []
        tasks_root = self.store.layout.control_root / "tasks"
        for path in tasks_root.glob(f"*/instances/{instance_id}.json"):
            if path.is_file():
                matches.append(path.parent.parent.name)
        if len(matches) != 1:
            raise HarnessError(
                "INSTANCE_NOT_FOUND",
                "The Image adapter could not resolve one unique instance.",
                {"instance_id": instance_id},
            )
        return matches[0]

    def _base_url(self, task_id: str, instance_id: str) -> str:
        instance = self.store.instance.get(task_id, instance_id)
        process = None if instance is None else instance.get("process")
        if not isinstance(process, dict) or process.get("state") != "RUNNING":
            raise HarnessError("PROCESS_START_FAILED", "The Image Agent process is not running.")
        return f"http://{self.host}:{int(process['port'])}"

    def _state_path(self, task_id: str, instance_id: str) -> Path:
        return (
            self.store.layout.initialize_instance(task_id, instance_id)
            / "runtime"
            / "image-adapter.json"
        )

    def _state(self, task_id: str, instance_id: str) -> dict[str, Any]:
        path = self._state_path(task_id, instance_id)
        if not path.exists():
            raise HarnessError("PROCESS_START_FAILED", "The Image Agent adapter was not prepared.")
        return read_json(path)

    def _write_state(self, task_id: str, instance_id: str, state: dict[str, Any]) -> None:
        atomic_write_json(self._state_path(task_id, instance_id), state)

    @staticmethod
    def _protocol_error(message: str) -> NoReturn:
        raise HarnessError("VALIDATION_ERROR", message)
