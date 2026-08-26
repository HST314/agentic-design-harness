"""Immutable instance runtime configuration bundles and active pointers."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from ..core.errors import HarnessError
from .atomic import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    canonical_yaml_bytes,
    digest_json,
)
from .config_revision_io import (
    CrashHook,
    ensure_private_directory,
    invoke_crash_hook,
    parse_yaml_object,
    publish_immutable_directory,
    read_json_object,
    read_regular_bytes,
    recover_temporary_paths,
    sha256_bytes,
    validate_config_tree_shape,
    validate_public_config_tree,
)
from .layout import validate_identifier
from .locks import FileLock
from .store import FileStateStore

_MATERIALIZATION_FIELDS = {"source_config_revision", "config_hash", "generated_at"}
_MODEL_STATES = (
    "intake_clarify",
    "confirmation_build",
    "initial_candidate_generation",
    "self_check_inspection",
    "self_check_rework",
    "human_prompt_rework",
)


class InstanceConfigRevisionStore:
    """Persist complete runtime/model bundles before advancing the active revision."""

    def __init__(self, store: FileStateStore) -> None:
        self.store = store

    def write_revision(
        self,
        task_id: str,
        instance_id: str,
        manifest: dict[str, Any],
        runtime: dict[str, Any],
        model_config: dict[str, Any],
        *,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        self._validate_scope(task_id, instance_id)
        self.store.contracts.validate("instance-runtime-config-manifest", manifest)
        if manifest["task_id"] != task_id or manifest["instance_id"] != instance_id:
            raise HarnessError(
                "VALIDATION_ERROR", "The runtime configuration manifest has the wrong scope."
            )
        if manifest["parent_revision_id"] == manifest["revision_id"]:
            raise HarnessError(
                "VALIDATION_ERROR", "A runtime configuration revision cannot be its own parent."
            )
        validate_public_config_tree(runtime)
        validate_public_config_tree(model_config)
        runtime_bytes = canonical_yaml_bytes(runtime)
        model_bytes = canonical_yaml_bytes(model_config)
        self._validate_hashes(manifest, runtime_bytes, model_bytes)
        files = {
            "manifest.json": canonical_json_bytes(manifest) + b"\n",
            "runtime.yaml": runtime_bytes,
            "model_config.yaml": model_bytes,
        }
        with FileLock(self._lock_path(task_id, instance_id), self.store.lock_timeout_seconds):
            self.store.layout.initialize_instance(task_id, instance_id)
            root = self._runtime_root(task_id, instance_id)
            ensure_private_directory(root)
            ensure_private_directory(root / "proposals")
            ensure_private_directory(self._revisions_root(task_id, instance_id))
            publish_immutable_directory(
                self._revision_root(task_id, instance_id, manifest["revision_id"]),
                files,
                crash_hook=crash_hook,
            )
        return {
            "manifest": deepcopy(manifest),
            "runtime": deepcopy(runtime),
            "model_config": deepcopy(model_config),
        }

    def next_revision_id(self, task_id: str, instance_id: str) -> str:
        """Allocate after every published revision, including failed attempts."""

        self._validate_scope(task_id, instance_id)
        sequences = [
            int(path.name.rsplit("r", 1)[1])
            for path in self._revisions_root(task_id, instance_id).glob(
                "cfg-inst-r[0-9][0-9][0-9][0-9][0-9][0-9]"
            )
            if path.is_dir()
        ]
        current = self.read_current(task_id, instance_id)
        if current is not None:
            sequences.append(
                int(current["manifest"]["revision_id"].rsplit("r", 1)[1])
            )
        return f"cfg-inst-r{max(sequences, default=0) + 1:06d}"

    def set_current(
        self,
        task_id: str,
        instance_id: str,
        revision_id: str,
        *,
        expected_revision: int,
        updated_at: str,
        applied_receipt: dict[str, Any] | None = None,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        self._validate_scope(task_id, instance_id)
        validate_identifier(revision_id, "revision_id")
        with FileLock(self._lock_path(task_id, instance_id), self.store.lock_timeout_seconds):
            current = self.read_current(task_id, instance_id)
            current_number = 0 if current is None else int(current["state"]["revision"])
            if current_number != expected_revision:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "The instance configuration state changed after it was read.",
                    {"expected_revision": expected_revision, "actual_revision": current_number},
                )
            target = self.read_revision(task_id, instance_id, revision_id)
            manifest = target["manifest"]
            if manifest["apply_status"] != "APPLIED" and applied_receipt is None:
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "Only an applied configuration revision may become current.",
                    {"revision_id": revision_id},
                )
            if applied_receipt is not None:
                self._validate_application_receipt(manifest, applied_receipt)
                self.record_application(
                    task_id,
                    instance_id,
                    revision_id,
                    applied_receipt,
                )
            expected_parent = None if current is None else current["manifest"]["revision_id"]
            if manifest["parent_revision_id"] != expected_parent:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "The instance configuration revision parent is stale.",
                    {"expected_parent_revision_id": expected_parent},
                )
            state = {
                "schema_version": "2.0",
                "task_id": task_id,
                "instance_id": instance_id,
                "current_revision_id": revision_id,
                "pending_revision_id": None,
                "revision": expected_revision + 1,
                "created_at": updated_at if current is None else current["state"]["created_at"],
                "updated_at": updated_at,
            }
            self.store.contracts.validate("instance-runtime-config-state", state)
            validate_public_config_tree(state)
            self.store.layout.initialize_instance(task_id, instance_id)
            ensure_private_directory(self._runtime_root(task_id, instance_id))
            atomic_write_json(self._state_path(task_id, instance_id), state, mode=0o640)
            invoke_crash_hook(crash_hook, "after_instance_state_published")
            return state

    def set_pending(
        self,
        task_id: str,
        instance_id: str,
        revision_id: str,
        *,
        expected_revision: int,
        updated_at: str,
    ) -> dict[str, Any]:
        """CAS the single pending revision without changing the active pointer."""

        self._validate_scope(task_id, instance_id)
        validate_identifier(revision_id, "revision_id")
        with FileLock(self._lock_path(task_id, instance_id), self.store.lock_timeout_seconds):
            current = self.read_current(task_id, instance_id)
            if current is None:
                raise HarnessError(
                    "CONFIG_INTEGRITY_FAILED",
                    "A pending revision requires an active instance baseline.",
                )
            state = current["state"]
            if int(state["revision"]) != expected_revision:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "The instance configuration state changed after it was read.",
                    {
                        "expected_revision": expected_revision,
                        "actual_revision": int(state["revision"]),
                    },
                )
            existing = state.get("pending_revision_id")
            if existing not in {None, revision_id}:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "Another runtime configuration revision is already pending.",
                    {"pending_revision_id": existing},
                )
            self.read_revision(task_id, instance_id, revision_id)
            if existing == revision_id:
                return deepcopy(state)
            updated = {
                **state,
                "pending_revision_id": revision_id,
                "revision": expected_revision + 1,
                "updated_at": updated_at,
            }
            self.store.contracts.validate("instance-runtime-config-state", updated)
            validate_public_config_tree(updated)
            atomic_write_json(self._state_path(task_id, instance_id), updated, mode=0o640)
            return deepcopy(updated)

    def clear_pending(
        self,
        task_id: str,
        instance_id: str,
        revision_id: str,
        *,
        expected_revision: int,
        updated_at: str,
    ) -> dict[str, Any]:
        """CAS-clear one failed pending revision without advancing the active pointer."""

        self._validate_scope(task_id, instance_id)
        validate_identifier(revision_id, "revision_id")
        with FileLock(self._lock_path(task_id, instance_id), self.store.lock_timeout_seconds):
            current = self.read_current(task_id, instance_id)
            if current is None:
                raise HarnessError(
                    "CONFIG_INTEGRITY_FAILED",
                    "A pending revision requires an active instance baseline.",
                )
            state = current["state"]
            if int(state["revision"]) != expected_revision:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "The instance configuration state changed after it was read.",
                    {
                        "expected_revision": expected_revision,
                        "actual_revision": int(state["revision"]),
                    },
                )
            existing = state.get("pending_revision_id")
            if existing is None:
                return deepcopy(state)
            if existing != revision_id:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "Another runtime configuration revision is already pending.",
                    {"pending_revision_id": existing},
                )
            updated = {
                **state,
                "pending_revision_id": None,
                "revision": expected_revision + 1,
                "updated_at": updated_at,
            }
            self.store.contracts.validate("instance-runtime-config-state", updated)
            validate_public_config_tree(updated)
            atomic_write_json(self._state_path(task_id, instance_id), updated, mode=0o640)
            return deepcopy(updated)

    def record_application(
        self,
        task_id: str,
        instance_id: str,
        revision_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish the immutable local receipt for a remote branch application."""

        self._validate_scope(task_id, instance_id)
        validate_identifier(revision_id, "revision_id")
        manifest = self.read_revision(task_id, instance_id, revision_id)["manifest"]
        self._validate_application_receipt(manifest, receipt)
        document = {
            "schema_version": "1.0",
            "task_id": task_id,
            "instance_id": instance_id,
            "revision_id": revision_id,
            "branch_id": receipt["branch_id"],
            "checkpoint_id": receipt["checkpoint_id"],
            "from_checkpoint": receipt["from_checkpoint"],
            "effective_from_state": receipt["effective_from_state"],
            "config_hash": receipt["config_hash"],
            "applied_at": receipt["applied_at"],
        }
        validate_public_config_tree(document)
        root = self._applications_root(task_id, instance_id)
        ensure_private_directory(root)
        path = root / f"{revision_id}.json"
        content = canonical_json_bytes(document) + b"\n"
        if path.exists():
            if read_regular_bytes(path, trusted_root=root) != content:
                raise HarnessError(
                    "CONFIG_INTEGRITY_FAILED",
                    "The runtime configuration application receipt changed after publication.",
                    {"revision_id": revision_id},
                )
            return document
        atomic_write_bytes(path, content, mode=0o440)
        return document

    def read_application(
        self, task_id: str, instance_id: str, revision_id: str
    ) -> dict[str, Any] | None:
        path = self._applications_root(task_id, instance_id) / f"{revision_id}.json"
        if not path.exists():
            return None
        document = read_json_object(
            path, trusted_root=self._applications_root(task_id, instance_id)
        )
        manifest = self.read_revision(task_id, instance_id, revision_id)["manifest"]
        self._validate_application_receipt(manifest, document)
        return document

    def read_current(self, task_id: str, instance_id: str) -> dict[str, Any] | None:
        self._validate_scope(task_id, instance_id)
        state_path = self._state_path(task_id, instance_id)
        if not state_path.exists():
            legacy = self._legacy_bundle(task_id, instance_id)
            return deepcopy(legacy) if legacy is not None else None
        state = read_json_object(
            state_path,
            trusted_root=self._runtime_root(task_id, instance_id),
        )
        self._validate_persisted("instance-runtime-config-state", state)
        validate_public_config_tree(state)
        if state.get("task_id") != task_id or state.get("instance_id") != instance_id:
            self._integrity("The instance configuration state has the wrong scope.")
        bundle = self.read_revision(task_id, instance_id, str(state["current_revision_id"]))
        if bundle["manifest"]["apply_status"] != "APPLIED":
            receipt = self.read_application(task_id, instance_id, str(state["current_revision_id"]))
            if receipt is None:
                self._integrity("The active instance configuration revision is not applied.")
            bundle["application"] = receipt
        pending = state.get("pending_revision_id")
        if pending is not None:
            if pending == state["current_revision_id"]:
                self._integrity("The pending instance revision is already current.")
            self.read_revision(task_id, instance_id, str(pending))
        return {"state": state, **bundle, "legacy": False}

    def read_revision(
        self, task_id: str, instance_id: str, revision_id: str
    ) -> dict[str, Any]:
        self._validate_scope(task_id, instance_id)
        validate_identifier(revision_id, "revision_id")
        root = self._revision_root(task_id, instance_id, revision_id)
        if not root.exists() and revision_id == "cfg-inst-r000001":
            legacy = self._legacy_bundle(task_id, instance_id)
            if legacy is not None:
                return {
                    key: deepcopy(legacy[key])
                    for key in ("manifest", "runtime", "model_config")
                }
        manifest = read_json_object(root / "manifest.json", trusted_root=root)
        self._validate_persisted("instance-runtime-config-manifest", manifest)
        if (
            manifest.get("task_id") != task_id
            or manifest.get("instance_id") != instance_id
            or manifest.get("revision_id") != revision_id
        ):
            self._integrity("The instance configuration revision identity is inconsistent.")
        if manifest.get("parent_revision_id") == revision_id:
            self._integrity("An instance configuration revision cannot be its own parent.")
        runtime_bytes = read_regular_bytes(root / "runtime.yaml", trusted_root=root)
        model_bytes = read_regular_bytes(root / "model_config.yaml", trusted_root=root)
        self._validate_hashes(manifest, runtime_bytes, model_bytes)
        runtime = parse_yaml_object(runtime_bytes, filename="runtime.yaml")
        model_config = parse_yaml_object(model_bytes, filename="model_config.yaml")
        validate_public_config_tree(runtime)
        validate_public_config_tree(model_config)
        return {"manifest": manifest, "runtime": runtime, "model_config": model_config}

    def recover(self, task_id: str, instance_id: str) -> dict[str, Any]:
        """Discard incomplete staged directories and verify the active pointer."""

        self._validate_scope(task_id, instance_id)
        root = self._runtime_root(task_id, instance_id)
        if not root.exists():
            return {"removed_temporary_paths": [], "current_revision_id": None}
        removed = [
            *recover_temporary_paths(root),
            *recover_temporary_paths(self._revisions_root(task_id, instance_id)),
        ]
        current = self.read_current(task_id, instance_id)
        return {
            "removed_temporary_paths": removed,
            "current_revision_id": (
                None if current is None else current["manifest"]["revision_id"]
            ),
        }

    @staticmethod
    def content_hash(runtime_sha256: str, model_config_sha256: str) -> str:
        return digest_json(
            {
                "runtime_sha256": runtime_sha256,
                "model_config_sha256": model_config_sha256,
            }
        )

    @classmethod
    def build_manifest(
        cls,
        *,
        task_id: str,
        instance_id: str,
        revision_id: str,
        parent_revision_id: str | None,
        task_config_revision_id: str,
        overrides: dict[str, Any],
        effective_runtime: dict[str, Any],
        model_bindings: dict[str, str],
        runtime: dict[str, Any],
        model_config: dict[str, Any],
        created_by: dict[str, str],
        created_at: str,
        confirmed_at: str | None,
        apply_mode: str,
        apply_status: str,
        branch_id: str | None = None,
        checkpoint_id: str | None = None,
        effective_from_state: str | None = None,
    ) -> dict[str, Any]:
        runtime_sha256 = sha256_bytes(canonical_yaml_bytes(runtime))
        model_sha256 = sha256_bytes(canonical_yaml_bytes(model_config))
        return {
            "schema_version": "2.0",
            "task_id": task_id,
            "instance_id": instance_id,
            "revision_id": revision_id,
            "parent_revision_id": parent_revision_id,
            "task_config_revision_id": task_config_revision_id,
            "overrides": deepcopy(overrides),
            "effective_runtime": deepcopy(effective_runtime),
            "model_bindings": dict(model_bindings),
            "runtime_sha256": runtime_sha256,
            "model_config_sha256": model_sha256,
            "config_hash": cls.content_hash(runtime_sha256, model_sha256),
            "created_by": dict(created_by),
            "created_at": created_at,
            "confirmed_at": confirmed_at,
            "apply_mode": apply_mode,
            "apply_status": apply_status,
            "branch_id": branch_id,
            "checkpoint_id": checkpoint_id,
            "effective_from_state": effective_from_state,
        }

    def _legacy_bundle(self, task_id: str, instance_id: str) -> dict[str, Any] | None:
        root = self._runtime_root(task_id, instance_id)
        runtime_path = root / "runtime.yaml"
        model_path = root / "model_config.yaml"
        if not runtime_path.exists() and not model_path.exists():
            return None
        if not runtime_path.exists() or not model_path.exists():
            self._integrity("The legacy instance configuration is incomplete.")
        runtime_bytes = read_regular_bytes(runtime_path, trusted_root=root)
        model_bytes = read_regular_bytes(model_path, trusted_root=root)
        runtime = parse_yaml_object(runtime_bytes, filename="runtime.yaml")
        model_config = parse_yaml_object(model_bytes, filename="model_config.yaml")
        validate_config_tree_shape(runtime)
        validate_config_tree_shape(model_config)
        self._validate_legacy_metadata(runtime, model_config)
        bindings = self._legacy_bindings(model_config)
        created_at = str(runtime["generated_at"])
        runtime_sha256 = sha256_bytes(runtime_bytes)
        model_sha256 = sha256_bytes(model_bytes)
        manifest = {
            "schema_version": "2.0",
            "task_id": task_id,
            "instance_id": instance_id,
            "revision_id": "cfg-inst-r000001",
            "parent_revision_id": None,
            "task_config_revision_id": "task-config-r000001",
            "overrides": {},
            "effective_runtime": self._legacy_effective_runtime(runtime),
            "model_bindings": bindings,
            "runtime_sha256": runtime_sha256,
            "model_config_sha256": model_sha256,
            "config_hash": self.content_hash(runtime_sha256, model_sha256),
            "created_by": {"type": "system", "id": "legacy_migration"},
            "created_at": created_at,
            "confirmed_at": created_at,
            "apply_mode": "before_start",
            "apply_status": "APPLIED",
            "branch_id": None,
            "checkpoint_id": None,
            "effective_from_state": "initial",
        }
        state = {
            "schema_version": "2.0",
            "task_id": task_id,
            "instance_id": instance_id,
            "current_revision_id": manifest["revision_id"],
            "pending_revision_id": None,
            "revision": 1,
            "created_at": created_at,
            "updated_at": created_at,
        }
        self._validate_persisted("instance-runtime-config-manifest", manifest)
        self._validate_persisted("instance-runtime-config-state", state)
        return {
            "state": state,
            "manifest": manifest,
            "runtime": runtime,
            "model_config": model_config,
            "legacy": True,
        }

    @staticmethod
    def _legacy_effective_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
        self_check = runtime.get("self_check", {})
        if not isinstance(self_check, dict):
            self_check = {}
        return {
            "question_preference": runtime.get("question_preference", "proactive"),
            "max_auto_questions": runtime.get("max_auto_questions", 3),
            "clarification_total_budget": runtime.get("clarification_total_budget", 10),
            "candidate_concurrency": runtime["candidate_concurrency"],
            "default_output_size": runtime["default_output_size"],
            "response_format": runtime["response_format"],
            "watermark": runtime["watermark"],
            "self_check": {
                "termination": self_check.get("termination", "solo"),
                "fixed_rounds": self_check.get("fixed_rounds", 2),
                "max_rounds": self_check.get("max_rounds", 4),
                "stop_early_on_pass": self_check.get("stop_early_on_pass", False),
            },
        }

    @staticmethod
    def _legacy_bindings(model_config: dict[str, Any]) -> dict[str, str]:
        raw = model_config.get("state_bindings")
        if not isinstance(raw, list):
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED", "The legacy model bindings are invalid."
            )
        bindings = {
            str(item.get("state")): str(item.get("model"))
            for item in raw
            if isinstance(item, dict) and item.get("state") in _MODEL_STATES
        }
        if set(bindings) != set(_MODEL_STATES) or any(not value for value in bindings.values()):
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED", "The legacy model bindings are incomplete."
            )
        return bindings

    @staticmethod
    def _validate_legacy_metadata(
        runtime: dict[str, Any], model_config: dict[str, Any]
    ) -> None:
        metadata = {
            key: runtime.get(key)
            for key in _MATERIALIZATION_FIELDS
        }
        if (
            not all(isinstance(value, str) and value for value in metadata.values())
            or any(model_config.get(key) != value for key, value in metadata.items())
        ):
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED", "The legacy configuration metadata is inconsistent."
            )
        runtime_body = {
            key: value for key, value in runtime.items() if key not in _MATERIALIZATION_FIELDS
        }
        model_body = {
            key: value
            for key, value in model_config.items()
            if key not in _MATERIALIZATION_FIELDS
        }
        if digest_json({"runtime": runtime_body, "model_config": model_body}) != metadata[
            "config_hash"
        ]:
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED", "The legacy configuration hash is invalid."
            )

    @classmethod
    def _validate_hashes(
        cls, manifest: dict[str, Any], runtime_bytes: bytes, model_bytes: bytes
    ) -> None:
        validate_public_config_tree(manifest)
        runtime_sha256 = sha256_bytes(runtime_bytes)
        model_sha256 = sha256_bytes(model_bytes)
        expected = cls.content_hash(runtime_sha256, model_sha256)
        if (
            manifest.get("runtime_sha256") != runtime_sha256
            or manifest.get("model_config_sha256") != model_sha256
            or manifest.get("config_hash") != expected
        ):
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "The instance configuration bundle failed its content hash checks.",
                {"revision_id": str(manifest.get("revision_id", "unknown"))},
            )

    def _validate_persisted(self, schema: str, document: dict[str, Any]) -> None:
        try:
            self.store.contracts.validate(schema, document)
        except HarnessError as exc:
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "A persisted instance configuration document failed contract validation.",
                {"schema": schema},
            ) from exc

    @staticmethod
    def _validate_scope(task_id: str, instance_id: str) -> None:
        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")

    def _runtime_root(self, task_id: str, instance_id: str) -> Path:
        return (
            self.store.layout.workspace_root
            / "tasks"
            / task_id
            / "instances"
            / instance_id
            / "runtime-config"
        )

    def _revisions_root(self, task_id: str, instance_id: str) -> Path:
        return self._runtime_root(task_id, instance_id) / "revisions"

    def _applications_root(self, task_id: str, instance_id: str) -> Path:
        return self._runtime_root(task_id, instance_id) / "applications"

    def _revision_root(self, task_id: str, instance_id: str, revision_id: str) -> Path:
        validate_identifier(revision_id, "revision_id")
        return self._revisions_root(task_id, instance_id) / revision_id

    def _state_path(self, task_id: str, instance_id: str) -> Path:
        return self._runtime_root(task_id, instance_id) / "state.json"

    def _lock_path(self, task_id: str, instance_id: str) -> Path:
        return (
            self.store.layout.control_root
            / "locks"
            / f"image-config-{task_id}-{instance_id}.lock"
        )

    @staticmethod
    def _integrity(message: str) -> None:
        raise HarnessError("CONFIG_INTEGRITY_FAILED", message)

    @staticmethod
    def _validate_application_receipt(
        manifest: dict[str, Any], receipt: dict[str, Any]
    ) -> None:
        required_strings = (
            "branch_id",
            "checkpoint_id",
            "from_checkpoint",
            "effective_from_state",
            "config_hash",
            "applied_at",
        )
        if any(
            not isinstance(receipt.get(field), str) or not receipt[field]
            for field in required_strings
        ):
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "The runtime configuration application receipt is incomplete.",
            )
        if (
            receipt.get("revision_id", manifest["revision_id"])
            != manifest["revision_id"]
            or receipt["config_hash"] != manifest["config_hash"]
            or receipt["effective_from_state"] != manifest["effective_from_state"]
        ):
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "The runtime configuration application receipt does not match its revision.",
                {"revision_id": manifest["revision_id"]},
            )
