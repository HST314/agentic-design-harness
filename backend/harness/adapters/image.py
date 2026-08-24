"""Runnable Image Agent adapter over its process and HTTP boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlencode, urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from ..contracts import ContractRegistry
from ..core.errors import HarnessError
from ..services.agent_config_materialization import ImageAgentConfigMaterializer
from ..services.assets import AssetService
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
from .image_attestation import RuntimeAttestation, attest_image_runtime
from .image_delivery import image_dimensions, normalize_image_delivery, stage_final_delivery
from .image_lock import (
    ImageAgentReleaseLock,
    default_image_agent_lock_path,
    load_image_agent_lock,
)
from .image_observation import MANAGED_ADAPTER_HEADER, ImageObservationMixin
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
from .types import (
    AgentInstanceSnapshot,
    DeliveryBundleCandidate,
    DeliveryCandidate,
    TaskCard,
    UsageEvent,
)

_PACKAGE_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
_ACTIVE_JOB_STATES = frozenset({"queued", "running", "cancelling"})


def image_dependency_pythonpath_entries(
    artifact_root: Path, *, os_name: str | None = None
) -> tuple[Path, ...]:
    """Return import roots required by a pip --target dependency artifact."""

    dependencies = artifact_root / "_dependencies"
    selected_os = os.name if os_name is None else os_name
    if selected_os == "nt":
        # pip --target does not process pywin32.pth. These are the same
        # locked directories that the wheel's .pth file would add.
        return (
            dependencies,
            dependencies / "win32",
            dependencies / "win32" / "lib",
            dependencies / "pythonwin",
        )
    return (dependencies,)


class ImageAgentAdapter(ImageObservationMixin):
    """Translate frozen Harness contracts to the Image Agent's local HTTP API."""

    agent_type = "image"
    available = True

    def __init__(
        self,
        store: FileStateStore,
        contracts: ContractRegistry,
        assets: AssetService,
        image_config: ImageAgentConfigMaterializer,
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
        self.image_config = image_config
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
        self.runtime_attestation: RuntimeAttestation | None = None
        self.runtime_artifact_root: Path | None = None
        self.runtime_builder = ImageRuntimeBuilder(
            source_root,
            dependency_root,
            revision=self.revision,
            package_version=self.package_version,
            source_content_sha256=self.release_lock.source_content_sha256,
            dependency_content_sha256=self.release_lock.runtime_dependency_tree_sha256,
        )

    def prepare_runtime_artifact(
        self, *, harness_root: Path, cache_root: Path
    ) -> RuntimeAttestation:
        """Attest and freeze the only Image runtime accepted by this process."""

        if not self.interpreter.is_absolute() or not self.interpreter.is_file():
            raise HarnessError(
                "IMAGE_RUNTIME_ATTESTATION_FAILED",
                "The isolated Image Agent interpreter is not configured.",
            )
        try:
            attestation = attest_image_runtime(
                self.release_lock,
                source_root=self.source_root,
                dependency_root=self.dependency_root,
                harness_root=harness_root,
            )
            builder = ImageRuntimeBuilder(
                self.source_root.resolve(strict=True),
                self.dependency_root.resolve(strict=True),
                revision=self.revision,
                package_version=self.package_version,
                source_content_sha256=attestation.source_sha256,
                dependency_content_sha256=attestation.dependency_sha256,
                identity_sha256=attestation.identity_sha256,
                platform=attestation.platform,
            )
            artifact_root = builder.prepare(cache_root)
            self._validate_runtime_source(artifact_root)
        except HarnessError as exc:
            if exc.code == "IMAGE_RUNTIME_ATTESTATION_FAILED":
                raise
            raise HarnessError(
                "IMAGE_RUNTIME_ATTESTATION_FAILED",
                "The Image Agent runtime artifact cannot be verified.",
                {"cause_code": exc.code},
            ) from None
        except (OSError, ValueError):
            raise HarnessError(
                "IMAGE_RUNTIME_ATTESTATION_FAILED",
                "The Image Agent runtime artifact cannot be inspected.",
            ) from None
        self.runtime_builder = builder
        self.runtime_artifact_root = artifact_root
        self.runtime_attestation = attestation
        return attestation

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
        artifact_root = self.runtime_artifact_root
        if artifact_root is None or self.runtime_attestation is None:
            raise HarnessError(
                "CONTROL_PLANE_NOT_READY",
                "The verified Image runtime artifact is not ready.",
            )
        self._validate_runtime_source(artifact_root)
        instance_id = str(request.instance["instance_id"])
        task_id = str(request.instance["task_id"])
        instance_root = self.store.layout.initialize_instance(task_id, instance_id)
        self.image_config.materialize(task_id, instance_id)
        image_card = self.map_task_card(request)
        runtime_root = instance_root / "runtime"
        atomic_write_json(runtime_root / "image-task-card.json", image_card, mode=0o640)
        state_path = self._state_path(task_id, instance_id)
        control_path = runtime_root / "managed-control.json"
        if control_path.exists():
            control = read_json(control_path)
        elif state_path.exists():
            raise HarnessError(
                "PROCESS_START_FAILED",
                "The prepared Image runtime lost its managed control file.",
                {"instance_id": instance_id},
            )
        else:
            control = {
                "schema_version": "1.0",
                "instance_id": instance_id,
                "request_key": secrets.token_urlsafe(32),
            }
            atomic_write_json(control_path, control, mode=0o600)
        managed_request_key = control.get("request_key")
        if (
            control.get("instance_id") != instance_id
            or not isinstance(managed_request_key, str)
            or len(managed_request_key) < 32
        ):
            raise HarnessError(
                "PROCESS_START_FAILED",
                "The Image runtime managed control file is invalid.",
                {"instance_id": instance_id},
            )
        expected_state = {
            "schema_version": "1.0",
            "task_id": task_id,
            "instance_id": instance_id,
            "task_card_sha256": digest_json(image_card),
            "source_revision": self.revision,
            "managed_request_key_sha256": hashlib.sha256(
                managed_request_key.encode("utf-8")
            ).hexdigest(),
            "project_created": False,
            "operation_id": None,
            "job_id": None,
            "timeline_cursor": 0,
        }
        if state_path.exists():
            current = read_json(state_path)
            immutable = (
                "task_id",
                "instance_id",
                "task_card_sha256",
                "source_revision",
                "managed_request_key_sha256",
            )
            if any(current.get(key) != expected_state[key] for key in immutable):
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The prepared Image runtime no longer matches its pinned task card.",
                    {"instance_id": instance_id},
                )
        else:
            atomic_write_json(state_path, expected_state)
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
                "IMAGE_AGENT_MANAGED_MODE": "1",
                "IMAGE_AGENT_MANAGED_PROJECT_ID": instance_id,
                "IMAGE_AGENT_CONTROL_FILE": str(control_path),
                "PYTHONPATH": os.pathsep.join(
                    str(path) for path in image_dependency_pythonpath_entries(artifact_root)
                ),
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
        if not source_refs:
            source_refs.append(
                {
                    "ref_id": card["card_id"],
                    "ref_type": "task_card",
                    "excerpt": card["objective"],
                    "source_hash": digest_json(
                        {
                            "objective": card["objective"],
                            "instructions": instructions,
                            "parameters": card["parameters"],
                            "revision": card["revision"],
                        }
                    ),
                }
            )
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
                control = read_json(
                    self.store.layout.initialize_instance(task_id, instance_id)
                    / "runtime"
                    / "managed-control.json"
                )
                managed_request_key = control.get("request_key")
                if not isinstance(managed_request_key, str) or len(managed_request_key) < 32:
                    raise HarnessError(
                        "PROCESS_START_FAILED",
                        "The Image runtime managed control file is invalid.",
                        {"instance_id": instance_id},
                    )
                self._request(
                    base_url,
                    "POST",
                    "/api/managed/projects",
                    {
                        "project_id": instance_id,
                        "task_card": card,
                        "defer_run": True,
                    },
                    expected_statuses=(201,),
                    headers={MANAGED_ADAPTER_HEADER: managed_request_key},
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

    def collect_delivery_bundles(self, instance_id: str) -> list[DeliveryBundleCandidate]:
        """Stage and validate every immutable branch candidate exposed by Image Agent."""

        task_id = self._task_id_for_instance(instance_id)
        base_url = self._base_url(task_id, instance_id)
        response = self._request(
            base_url,
            "POST",
            f"/api/projects/{instance_id}/delivery/candidates/finalize",
        )
        raw_candidates = None if response is None else response.get("candidates")
        if not isinstance(raw_candidates, list):
            self._protocol_error("Image Agent returned an invalid delivery candidate list.")
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
        instance_root = self.store.layout.initialize_instance(task_id, instance_id)
        project_root = instance_root / "work" / instance_id
        candidates: list[DeliveryBundleCandidate] = []
        seen: set[str] = set()
        for marker in raw_candidates:
            if not isinstance(marker, dict):
                self._protocol_error("Image Agent returned a malformed delivery candidate.")
            bundle_id = marker.get("bundle_id")
            checkpoint_id = marker.get("checkpoint_id")
            raw_branch_id = marker.get("branch_id")
            files = marker.get("files")
            image_descriptor = marker.get("image")
            note_descriptor = marker.get("design_note")
            if (
                not isinstance(bundle_id, str)
                or not isinstance(checkpoint_id, str)
                or not isinstance(raw_branch_id, str)
                or not isinstance(files, dict)
                or not isinstance(image_descriptor, dict)
                or not isinstance(note_descriptor, dict)
                or bundle_id in seen
            ):
                self._protocol_error("Image Agent returned a malformed delivery candidate.")
            validate_identifier(bundle_id, "bundle_id")
            validate_identifier(checkpoint_id, "checkpoint_id")
            branch_id = (
                raw_branch_id
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", raw_branch_id)
                else f"branch_{hashlib.sha256(raw_branch_id.encode('utf-8')).hexdigest()[:24]}"
            )
            image_relative = normalized_relative_path(str(files.get("image", "")))
            note_relative = normalized_relative_path(str(files.get("markdown", "")))
            if (
                len(image_relative.parts) != 2
                or image_relative.parts[0] != "delivery"
                or len(note_relative.parts) != 2
                or note_relative.parts[0] != "delivery"
            ):
                self._protocol_error("Image Agent delivery path escaped its delivery directory.")
            image_sha256 = image_descriptor.get("sha256")
            note_sha256 = note_descriptor.get("sha256")
            if (
                not isinstance(image_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", image_sha256) is None
                or not isinstance(note_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", note_sha256) is None
            ):
                self._protocol_error("Image Agent returned an invalid candidate digest.")
            image_output = instance_root / "outputs" / f"{bundle_id}-image{image_relative.suffix}"
            note_output = instance_root / "outputs" / f"{bundle_id}-design-note.md"
            stage_final_delivery(
                project_root,
                image_relative,
                image_output,
                expected_sha256=image_sha256,
            )
            stage_final_delivery(
                project_root,
                note_relative,
                note_output,
                expected_sha256=note_sha256,
            )
            normalized = normalize_image_delivery(
                image_output,
                accepted_mime_types=tuple(expected[0]["accepted_mime_types"]),
            )
            image_candidate = self.assets.inspect_delivery(
                task_id,
                instance_id,
                source_relative_path=(
                    f"instances/{instance_id}/"
                    f"{normalized['path'].relative_to(instance_root).as_posix()}"
                ),
                role=expected[0]["role"],
                description=f"Image Agent branch {raw_branch_id} final artwork.",
                expected_sha256=normalized["sha256"],
                derivation=normalized["derivation"],
            )
            note_candidate = self.assets.inspect_delivery(
                task_id,
                instance_id,
                source_relative_path=(
                    f"instances/{instance_id}/"
                    f"{note_output.relative_to(instance_root).as_posix()}"
                ),
                role="design_note",
                description=f"Image Agent branch {raw_branch_id} Markdown design note.",
                expected_sha256=note_sha256,
            )
            if note_candidate["mime_type"] != "text/markdown":
                self._protocol_error("Image Agent candidate note is not Markdown.")
            width, height = image_dimensions(normalized["path"])
            candidate: DeliveryBundleCandidate = {
                "schema_version": "1.0",
                "bundle_id": bundle_id,
                "task_id": task_id,
                "work_item_id": self._work_item_id(task_id, plan, card),
                "instance_id": instance_id,
                "task_card_revision": card["revision"],
                "branch_id": branch_id,
                "checkpoint_id": checkpoint_id,
                "image": {
                    "private_relative_path": image_candidate["source_relative_path"],
                    "mime_type": image_candidate["mime_type"],
                    "size_bytes": image_candidate["size_bytes"],
                    "sha256": image_candidate["sha256"],
                    "width": width,
                    "height": height,
                },
                "design_note": {
                    "private_relative_path": note_candidate["source_relative_path"],
                    "mime_type": "text/markdown",
                    "size_bytes": note_candidate["size_bytes"],
                    "sha256": note_candidate["sha256"],
                },
                "status": "PENDING_CONFIRMATION",
                "created_at": str(marker.get("created_at", "")),
                "decided_at": None,
                "actor": None,
                "publication_batch_id": None,
            }
            self.contracts.validate("delivery-bundle-candidate", candidate)
            seen.add(bundle_id)
            candidates.append(candidate)
        return sorted(
            candidates,
            key=lambda item: (item["branch_id"], item["checkpoint_id"], item["bundle_id"]),
        )

    def _work_item_id(
        self,
        task_id: str,
        plan: dict[str, Any],
        card: TaskCard,
    ) -> str:
        confirmed = [
            item
            for item in self.store.plan_proposal.list(task_id)
            if item["status"] == "CONFIRMED"
            and item["revision"] == plan["task"]["plan_revision"]
        ]
        for proposal in sorted(confirmed, key=lambda item: item["updated_at"], reverse=True):
            match = next(
                (
                    item
                    for item in proposal["work_items"]
                    if card["instance_id"] in item.get("instance_ids", [])
                    or item.get("current_instance_id") == card["instance_id"]
                    or card["card_id"] in item.get("task_card_ids", [])
                ),
                None,
            )
            if match is not None:
                return str(match["work_item_id"])
        identity = hashlib.sha256(card["card_id"].encode("utf-8")).hexdigest()[:24]
        return f"work_{identity}"

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
        state = self._state(task_id, instance_id)
        if not state.get("job_id"):
            # The durable request marker is written before the HTTP call.  A
            # missing job id therefore means replaying the same idempotency key
            # is the only safe way to decide whether the call crossed the wire.
            return AdapterRecoveryResult(
                recovered=False,
                status=status,
                details={"mode": "idempotent_start_replay"},
            )
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


    def _validate_runtime_source(self, source_root: Path | None = None) -> None:
        selected_root = source_root or self.runtime_artifact_root or self.source_root
        if self.revision != self.release_lock.revision:
            raise HarnessError(
                "SCHEMA_VERSION_UNSUPPORTED",
                "The configured Image Agent revision is not supported by this Harness build.",
            )
        if (
            not selected_root.is_absolute()
            or not selected_root.is_dir()
            or selected_root.is_symlink()
            or not self.interpreter.is_absolute()
            or not self.interpreter.is_file()
        ):
            raise HarnessError(
                "ADAPTER_UNAVAILABLE",
                "The Image Agent source or isolated interpreter is not configured.",
            )
        pyproject = selected_root / "pyproject.toml"
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
        selected_root = self.runtime_artifact_root or self.source_root
        schema_path = selected_root / "schemas" / "ImageTaskCard.schema.json"
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
