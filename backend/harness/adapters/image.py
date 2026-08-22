"""Runnable Image Agent adapter over its process and HTTP boundaries."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlencode, urlsplit

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
from .image_delivery import normalize_image_delivery, stage_final_delivery
from .image_lock import (
    ImageAgentReleaseLock,
    default_image_agent_lock_path,
    load_image_agent_lock,
)
from .image_observation import ImageObservationMixin
from .image_runtime import (
    IMAGE_ENTRYPOINT,
    IMAGE_WEB_REQUIREMENTS,
    ImageRuntimeBuilder,
)
from .image_usage import map_usage_page, usage_cursor
from .image_workflow import (
    HARNESS_CAPABILITIES,
    map_advance_payload,
)
from .types import AgentInstanceSnapshot, DeliveryCandidate, TaskCard, UsageEvent

_PACKAGE_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
_ACTIVE_JOB_STATES = frozenset({"queued", "running", "cancelling"})


class ImageAgentAdapter(ImageObservationMixin):
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
        release_lock: ImageAgentReleaseLock | None = None,
        revision: str | None = None,
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
        self.release_lock = release_lock or load_image_agent_lock(
            default_image_agent_lock_path()
        )
        self.revision = revision or self.release_lock.revision
        self.package_version = self.release_lock.package_version
        self.host = host
        self.request_timeout_seconds = request_timeout_seconds
        self.runtime_builder = ImageRuntimeBuilder(
            source_root,
            dependency_root,
            revision=self.revision,
            package_version=self.package_version,
            source_content_sha256=self.release_lock.source_content_sha256,
            dependency_content_sha256=self.release_lock.runtime_dependency_tree_sha256,
        )

    def validate_task_card(self, card: TaskCard) -> ValidationResult:
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
                "harness_output_contract": {
                    "expected_deliveries": deepcopy(card["expected_deliveries"]),
                    "aspect_ratio": card["parameters"].get("aspect_ratio"),
                },
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

    def collect_deliveries(self, instance_id: str) -> list[DeliveryCandidate]:
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
        normalized = normalize_image_delivery(
            output,
            accepted_mime_types=tuple(expected[0]["accepted_mime_types"]),
        )
        normalized_path = normalized["path"]
        normalized_relative = normalized_path.relative_to(instance_root).as_posix()
        note = envelope.get("design_note")
        description = "Image Agent verified final artwork."
        if isinstance(note, dict) and isinstance(note.get("task_fit"), str):
            description = note["task_fit"][:4000] or description
        return [
            {
                "source_relative_path": f"instances/{instance_id}/{normalized_relative}",
                "kind": "image",
                "role": expected[0]["role"],
                "description": description,
                "mime_type": normalized["mime_type"],
                "size_bytes": normalized["size_bytes"],
                "sha256": normalized["sha256"],
                "derivation": normalized["derivation"],
            }
        ]

    def collect_usage(self, instance_id: str, cursor: str | None) -> list[UsageEvent]:
        task_id = self._task_id_for_instance(instance_id)
        previous = usage_cursor(cursor)
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        base_url = self._base_url(task_id, instance_id)
        events: list[UsageEvent] = []
        while True:
            page = self._request(
                base_url,
                "GET",
                f"/api/projects/{instance_id}/usage?"
                f"{urlencode({'after': previous, 'limit': 500})}",
            )
            if page is None:
                self._protocol_error("Image Agent returned an empty usage response.")
            mapped, previous, has_more = map_usage_page(
                page,
                previous=previous,
                task_id=task_id,
                instance_id=instance_id,
                credential_pair_ref=instance.get("credential_pair_ref"),
            )
            events.extend(mapped)
            if len(events) > 10_000:
                self._protocol_error("Image Agent returned too many usage observations.")
            if not has_more:
                return events

    def get_ui_url(self, instance_id: str) -> str | None:
        task_id = self._task_id_for_instance(instance_id)
        instance = self.store.instance.get(task_id, instance_id)
        return None if instance is None else instance.get("ui_url")

    def validate_ui_url(
        self, instance: AgentInstanceSnapshot, ui_url: str
    ) -> ValidationResult:
        """Allow only the exact local origin allocated to this process instance."""

        errors: list[str] = []
        process = instance.get("process")
        try:
            parsed = urlsplit(ui_url)
            port = parsed.port
        except ValueError:
            return ValidationResult(False, ("The UI URL is malformed.",))
        if parsed.scheme != "http":
            errors.append("The Image workbench must use the local HTTP runtime.")
        if parsed.hostname != self.host:
            errors.append("The Image workbench host is outside the Adapter allowlist.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            errors.append("The Image workbench URL contains unsupported URL components.")
        if parsed.path not in {"", "/"}:
            errors.append("The Image workbench path is outside the Adapter entrypoint.")
        if not isinstance(process, dict) or process.get("state") != "RUNNING":
            errors.append("The Image workbench process is not running.")
        elif port != process.get("port"):
            errors.append("The Image workbench port does not match its process allocation.")
        return ValidationResult(not errors, tuple(errors))

    def recover(self, instance_snapshot: AgentInstanceSnapshot) -> AdapterRecoveryResult:
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


    def _validate_runtime_source(self) -> None:
        if self.revision != self.release_lock.revision:
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
        if match is None or match.group(1) != self.package_version:
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
