"""Recoverable web task intake, first-upload and presentation workflows."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any, BinaryIO

from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..domain.service import TaskCommandService
from ..storage.atomic import atomic_write_json
from ..storage.repository import Actor, utc_now
from ..storage.store import FileStateStore
from .assets import AssetService
from .task_config import TaskConfigService

ACCEPTED_MIME_TYPES = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
    "text/plain",
    "text/markdown",
)
MAX_FILES = 20
MAX_TOTAL_BYTES = 200 * 1024 * 1024
PER_MIME_LIMITS = {
    "image/jpeg": 20 * 1024 * 1024,
    "image/png": 20 * 1024 * 1024,
    "image/webp": 20 * 1024 * 1024,
    "application/pdf": 50 * 1024 * 1024,
    "text/plain": 5 * 1024 * 1024,
    "text/markdown": 5 * 1024 * 1024,
}
MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}


class TaskIntakeService:
    """Coordinate the DRAFT lifecycle without making the browser a fact source."""

    def __init__(
        self,
        store: FileStateStore,
        commands: TaskCommandService,
        assets: AssetService,
        task_config: TaskConfigService,
    ) -> None:
        self.store = store
        self.commands = commands
        self.assets = assets
        self.task_config = task_config

    def create(
        self,
        *,
        prompt: str,
        start_policy: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise HarnessError("VALIDATION_ERROR", "The task Prompt is invalid.")
        if start_policy not in {"manual", "auto"}:
            raise HarnessError("VALIDATION_ERROR", "The task start policy is invalid.")
        self._require_human(envelope)
        if envelope.expected_revision != 0:
            raise HarnessError(
                "REVISION_CONFLICT",
                "Task intake creation requires expected revision zero.",
                {"expected_revision": envelope.expected_revision, "actual_revision": 0},
            )
        identity = hashlib.sha256(
            f"{envelope.actor_id}\0{envelope.idempotency_key}".encode()
        ).hexdigest()
        task_id = f"task_intake_{identity[:20]}"
        request = {
            "prompt": normalized_prompt,
            "start_policy": start_policy,
            "actor_id": envelope.actor_id,
        }
        with self.commands.task_guard(task_id):
            replay = self._lookup(task_id, "create_task_intake", request, envelope)
            if replay is not None:
                return replay
            self.task_config.pin_for_creation(task_id)
            created = self.commands.create_task(
                task_id=task_id,
                title=self._provisional_title(normalized_prompt),
                goal=normalized_prompt,
                master_owner="master_default",
                start_policy=start_policy,
                input_manifest="inputs/manifests/intake-empty.json",
                envelope=envelope.model_copy(
                    update={"idempotency_key": self._derived_key("create-task", identity)}
                ),
            )
            task = created["task"]
            workspace = self.store.layout.workspace_root / "tasks" / task_id
            atomic_write_json(
                workspace / "inputs" / "manifests" / "intake-empty.json",
                {
                    "schema_version": "1.0",
                    "manifest_id": f"empty_{identity[:20]}",
                    "task_id": task_id,
                    "assets": [],
                    "created_at": task["created_at"],
                },
                mode=0o640,
            )
            actor = Actor(envelope.actor_type, envelope.actor_id)
            navigation = self.store.task_navigation.get(task_id, task_id)
            if navigation is None:
                navigation = {
                    "schema_version": "1.0",
                    "task_id": task_id,
                    "pinned_at": None,
                    "archived_at": None,
                    "display_order": 0,
                    "revision": 1,
                    "updated_at": task["created_at"],
                }
                self.store.task_navigation.put(
                    task_id,
                    task_id,
                    navigation,
                    expected_revision=0,
                    actor=actor,
                    command="create_task_navigation",
                    idempotency_key=self._derived_key("create-navigation", identity),
                )
            intake = self.store.task_intake.get(task_id, task_id)
            if intake is None:
                intake = {
                    "schema_version": "1.0",
                    "task_id": task_id,
                    "prompt": normalized_prompt,
                    "upload_session": {
                        "session_id": f"upload_{identity[:20]}",
                        "status": "OPEN",
                        "accepted_mime_types": list(ACCEPTED_MIME_TYPES),
                        "max_files": MAX_FILES,
                        "max_total_bytes": MAX_TOTAL_BYTES,
                    },
                    "asset_ids": [],
                    "status": "DRAFT",
                    "start_policy": start_policy,
                    "revision": 1,
                    "created_at": task["created_at"],
                    "updated_at": task["created_at"],
                    "submitted_at": None,
                }
            result = {
                "schema_version": "1.0",
                "intake": deepcopy(intake),
                "intake_revision": 1,
                "task": deepcopy(task),
                "task_revision": int(created["revision"]),
                "navigation": deepcopy(navigation),
                "presentation_revision": 1,
                "assets": [],
            }
            request_sha256 = self._request_digest("create_task_intake", request)
            self.store.task_intake.put(
                task_id,
                task_id,
                intake,
                expected_revision=0,
                actor=actor,
                command="create_task_intake",
                idempotency_key=envelope.idempotency_key,
                command_result=result,
                request_sha256=request_sha256,
            )
            return self.store.idempotency.remember_digest(
                task_id,
                envelope.idempotency_key,
                "create_task_intake",
                request_sha256,
                result,
            )

    def get(self, task_id: str) -> dict[str, Any]:
        intake = self.store.task_intake.get(task_id, task_id)
        task = self.store.task.get(task_id, task_id)
        if intake is None or task is None:
            raise HarnessError("TASK_NOT_FOUND", "The requested task intake does not exist.")
        navigation = self.store.task_navigation.get(task_id, task_id)
        return {
            "schema_version": "1.0",
            "intake": deepcopy(intake),
            "intake_revision": self.store.task_intake.revision(task_id, task_id),
            "task": deepcopy(task),
            "task_revision": self.store.task.revision(task_id, task_id),
            "navigation": deepcopy(navigation),
            "presentation_revision": self.store.task_navigation.revision(task_id, task_id),
            "assets": self._asset_summaries(task_id, intake["asset_ids"]),
        }

    def upload_asset(
        self,
        task_id: str,
        stream: BinaryIO,
        *,
        filename: str,
        declared_mime_type: str,
        description: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        return self._upload_asset(
            task_id,
            stream,
            filename=filename,
            declared_mime_type=declared_mime_type,
            description=description,
            envelope=envelope,
            command="upload_task_intake_asset",
            source="web_task_intake",
            require_draft=True,
            asset_key_prefix="intake-asset",
        )

    def upload_task_asset(
        self,
        task_id: str,
        stream: BinaryIO,
        *,
        filename: str,
        declared_mime_type: str,
        description: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        """Append an input resource at any point in the task lifecycle."""
        return self._upload_asset(
            task_id,
            stream,
            filename=filename,
            declared_mime_type=declared_mime_type,
            description=description,
            envelope=envelope,
            command="upload_task_asset",
            source="web_task_asset",
            require_draft=False,
            asset_key_prefix="task-asset",
        )

    def _upload_asset(
        self,
        task_id: str,
        stream: BinaryIO,
        *,
        filename: str,
        declared_mime_type: str,
        description: str,
        envelope: CommandEnvelope,
        command: str,
        source: str,
        require_draft: bool,
        asset_key_prefix: str,
    ) -> dict[str, Any]:
        self._require_human(envelope)
        if len(description) > 4_000:
            raise HarnessError("VALIDATION_ERROR", "The file description is too long.")
        expected_mime = MIME_BY_SUFFIX.get(Path(filename).suffix.lower())
        if expected_mime is None or declared_mime_type != expected_mime:
            raise HarnessError(
                "ASSET_VALIDATION_FAILED",
                "The filename extension and declared MIME type do not match an allowed format.",
            )
        intake = self._draft(task_id) if require_draft else self._intake(task_id)
        actual_revision = self.store.task_intake.revision(task_id, task_id)
        self._allow_commutative_revision(
            task_id, envelope.expected_revision, actual_revision
        )
        existing = self._asset_summaries(task_id, intake["asset_ids"])
        if len(existing) >= MAX_FILES:
            raise HarnessError("ASSET_VALIDATION_FAILED", "The task already has 20 files.")
        remaining = MAX_TOTAL_BYTES - sum(item["size_bytes"] for item in existing)
        identity = hashlib.sha256(
            f"{task_id}\0{envelope.idempotency_key}".encode()
        ).hexdigest()
        manifest = self.assets.import_stream(
            task_id,
            stream,
            filename=filename,
            description=description,
            source=source,
            idempotency_key=self._derived_key(asset_key_prefix, identity),
            max_file_bytes=min(PER_MIME_LIMITS[declared_mime_type], remaining),
        )
        if manifest["mime_type"] != declared_mime_type:
            self.assets.remove_input_asset(
                task_id,
                manifest["asset_id"],
                idempotency_key=self._derived_key("reject-mime", identity),
            )
            raise HarnessError(
                "ASSET_VALIDATION_FAILED",
                "The detected MIME type does not match the selected file type.",
            )
        request = {
            "asset_id": manifest["asset_id"],
            "filename": filename,
            "description": description,
            "declared_mime_type": declared_mime_type,
            "sha256": manifest["sha256"],
        }
        with self.commands.task_guard(task_id):
            replay = self._lookup(task_id, command, request, envelope)
            if replay is not None:
                return replay
            try:
                current = self._draft(task_id) if require_draft else self._intake(task_id)
            except HarnessError:
                # The upload streamed outside the task command lock. If the intake
                # closed while using the draft-only route, hide the late import.
                self.assets.remove_input_asset(
                    task_id,
                    manifest["asset_id"],
                    idempotency_key=self._derived_key("reject-closed", identity),
                )
                raise
            current_revision = self.store.task_intake.revision(task_id, task_id)
            self._allow_commutative_revision(
                task_id, envelope.expected_revision, current_revision
            )
            if manifest["asset_id"] in current["asset_ids"]:
                return {
                    "schema_version": "1.0",
                    "intake": deepcopy(current),
                    "intake_revision": current_revision,
                    "asset": self._asset_summary(manifest),
                }
            active = self._asset_summaries(task_id, current["asset_ids"])
            if len(active) >= MAX_FILES or sum(
                item["size_bytes"] for item in active
            ) + manifest["size_bytes"] > MAX_TOTAL_BYTES:
                self.assets.remove_input_asset(
                    task_id,
                    manifest["asset_id"],
                    idempotency_key=self._derived_key("reject-capacity", identity),
                )
                raise HarnessError(
                    "ASSET_VALIDATION_FAILED", "The task upload limit would be exceeded."
                )
            updated = deepcopy(current)
            updated["asset_ids"].append(manifest["asset_id"])
            updated["revision"] = current_revision + 1
            updated["updated_at"] = utc_now()
            result = {
                "schema_version": "1.0",
                "intake": deepcopy(updated),
                "intake_revision": current_revision + 1,
                "asset": self._asset_summary(manifest),
            }
            return self._put_intake(
                task_id,
                updated,
                expected_revision=current_revision,
                command=command,
                request=request,
                envelope=envelope,
                result=result,
            )

    def remove_asset(
        self,
        task_id: str,
        asset_id: str,
        *,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        self._require_human(envelope)
        request = {"asset_id": asset_id}
        with self.commands.task_guard(task_id):
            replay = self._lookup(task_id, "remove_task_intake_asset", request, envelope)
            if replay is not None:
                return replay
            current = self._draft(task_id)
            current_revision = self.store.task_intake.revision(task_id, task_id)
            self._allow_commutative_revision(
                task_id, envelope.expected_revision, current_revision
            )
            if asset_id not in current["asset_ids"]:
                raise HarnessError(
                    "ASSET_VALIDATION_FAILED", "The requested intake asset does not exist."
                )
            removal_identity = hashlib.sha256(
                f"{task_id}\0{envelope.idempotency_key}".encode()
            ).hexdigest()
            self.assets.remove_input_asset(
                task_id,
                asset_id,
                idempotency_key=self._derived_key("remove-intake", removal_identity),
            )
            updated = deepcopy(current)
            updated["asset_ids"].remove(asset_id)
            updated["revision"] = current_revision + 1
            updated["updated_at"] = utc_now()
            result = {
                "schema_version": "1.0",
                "intake": deepcopy(updated),
                "intake_revision": current_revision + 1,
                "removed_asset_id": asset_id,
            }
            return self._put_intake(
                task_id,
                updated,
                expected_revision=current_revision,
                command="remove_task_intake_asset",
                request=request,
                envelope=envelope,
                result=result,
            )

    def submit(
        self,
        task_id: str,
        *,
        task_expected_revision: int,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        self._require_human(envelope)
        request = {"task_id": task_id, "task_expected_revision": task_expected_revision}
        with self.commands.task_guard(task_id):
            replay = self._lookup(task_id, "submit_task_intake", request, envelope)
            if replay is not None:
                return replay
            current = self._draft(task_id)
            current_revision = self.store.task_intake.revision(task_id, task_id)
            if envelope.expected_revision != current_revision:
                self._raise_revision(
                    "task_intake", task_id, envelope.expected_revision, current_revision
                )
            for asset_id in current["asset_ids"]:
                self.assets.verify_asset(task_id, asset_id)
            manifest_id = f"selected_{task_id}"
            if current["asset_ids"]:
                selected = self.assets.select_inputs(
                    task_id, current["asset_ids"], manifest_id=manifest_id
                )
                manifest_relpath = selected["manifest_relpath"]
            else:
                manifest_relpath = f"inputs/manifests/{manifest_id}.json"
                workspace = self.store.layout.workspace_root / "tasks" / task_id
                atomic_write_json(
                    workspace / manifest_relpath,
                    {
                        "schema_version": "1.0",
                        "manifest_id": manifest_id,
                        "task_id": task_id,
                        "assets": [],
                        "created_at": utc_now(),
                    },
                    mode=0o640,
                )
            identity = hashlib.sha256(
                f"{task_id}\0{envelope.idempotency_key}".encode()
            ).hexdigest()
            task_result = self.commands.register_input_manifest(
                task_id,
                manifest_relpath,
                envelope.model_copy(
                    update={
                        "idempotency_key": self._derived_key("submit-manifest", identity),
                        "expected_revision": task_expected_revision,
                    }
                ),
            )
            now = utc_now()
            updated = deepcopy(current)
            updated["status"] = "SUBMITTED"
            updated["upload_session"]["status"] = "LOCKED"
            updated["revision"] = current_revision + 1
            updated["updated_at"] = now
            updated["submitted_at"] = now
            result = {
                "schema_version": "1.0",
                "intake": deepcopy(updated),
                "intake_revision": current_revision + 1,
                "task": deepcopy(task_result["task"]),
                "task_revision": int(task_result["revision"]),
                "assets": self._asset_summaries(task_id, updated["asset_ids"]),
            }
            return self._put_intake(
                task_id,
                updated,
                expected_revision=current_revision,
                command="submit_task_intake",
                request=request,
                envelope=envelope,
                result=result,
            )

    def update_presentation(
        self,
        task_id: str,
        *,
        title: str | None,
        pinned: bool | None,
        archived: bool | None,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        self._require_human(envelope)
        supplied = sum(value is not None for value in (title, pinned, archived))
        if supplied != 1:
            raise HarnessError(
                "VALIDATION_ERROR", "Change exactly one presentation field at a time."
            )
        if title is not None:
            renamed = self.commands.rename_task(task_id, title, envelope)
            revision = int(renamed.get("task_revision", renamed.get("revision", 0)))
            return {
                "schema_version": "1.0",
                "task": deepcopy(renamed["task"]),
                "task_revision": revision,
                "navigation": deepcopy(
                    self.store.task_navigation.get(task_id, task_id)
                ),
                "presentation_revision": self.store.task_navigation.revision(
                    task_id, task_id
                ),
            }
        request = {"pinned": pinned, "archived": archived}
        command = "pin_task" if pinned is not None else "archive_task"
        with self.commands.task_guard(task_id):
            replay = self._lookup(task_id, command, request, envelope)
            if replay is not None:
                return replay
            current = self.store.task_navigation.get(task_id, task_id)
            current_revision = self.store.task_navigation.revision(task_id, task_id)
            if current is None:
                task = self.store.task.get(task_id, task_id)
                if task is None:
                    raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
                current = {
                    "schema_version": "1.0",
                    "task_id": task_id,
                    "pinned_at": None,
                    "archived_at": None,
                    "display_order": 0,
                    "revision": 0,
                    "updated_at": task["updated_at"],
                }
            if envelope.expected_revision != current_revision:
                self._raise_revision(
                    "task_navigation", task_id, envelope.expected_revision, current_revision
                )
            now = utc_now()
            updated = deepcopy(current)
            if pinned is not None:
                updated["pinned_at"] = now if pinned else None
                if pinned:
                    updated["archived_at"] = None
            else:
                updated["archived_at"] = now if archived else None
                if archived:
                    updated["pinned_at"] = None
            updated["revision"] = current_revision + 1
            updated["updated_at"] = now
            task = self.store.task.get(task_id, task_id)
            result = {
                "schema_version": "1.0",
                "task": deepcopy(task),
                "task_revision": self.store.task.revision(task_id, task_id),
                "navigation": deepcopy(updated),
                "presentation_revision": current_revision + 1,
            }
            request_sha256 = self._request_digest(command, request)
            self.store.task_navigation.put(
                task_id,
                task_id,
                updated,
                expected_revision=current_revision,
                actor=Actor(envelope.actor_type, envelope.actor_id),
                command=command,
                idempotency_key=envelope.idempotency_key,
                command_result=result,
                request_sha256=request_sha256,
            )
            return self.store.idempotency.remember_digest(
                task_id,
                envelope.idempotency_key,
                command,
                request_sha256,
                result,
            )

    def _draft(self, task_id: str) -> dict[str, Any]:
        intake = self._intake(task_id)
        if intake["status"] != "DRAFT" or intake["upload_session"]["status"] != "OPEN":
            raise HarnessError(
                "INVALID_STATE_TRANSITION", "The task intake no longer accepts uploads."
            )
        return deepcopy(intake)

    def _intake(self, task_id: str) -> dict[str, Any]:
        intake = self.store.task_intake.get(task_id, task_id)
        if intake is None:
            raise HarnessError("TASK_NOT_FOUND", "The requested task intake does not exist.")
        return deepcopy(intake)

    def _put_intake(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        command: str,
        request: dict[str, Any],
        envelope: CommandEnvelope,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        request_sha256 = self._request_digest(command, request)
        self.store.task_intake.put(
            task_id,
            task_id,
            payload,
            expected_revision=expected_revision,
            actor=Actor(envelope.actor_type, envelope.actor_id),
            command=command,
            idempotency_key=envelope.idempotency_key,
            command_result=result,
            request_sha256=request_sha256,
        )
        return self.store.idempotency.remember_digest(
            task_id,
            envelope.idempotency_key,
            command,
            request_sha256,
            result,
        )

    def _lookup(
        self,
        task_id: str,
        command: str,
        request: dict[str, Any],
        envelope: CommandEnvelope,
    ) -> dict[str, Any] | None:
        existing = self.store.idempotency.lookup(
            task_id, envelope.idempotency_key, command, request
        )
        if existing is not None:
            return existing
        request_sha256 = self._request_digest(command, request)
        committed = self.store.lookup_committed_command_result(
            task_id,
            envelope.idempotency_key,
            command,
            request_sha256,
        )
        if committed is None:
            return None
        return self.store.idempotency.remember_digest(
            task_id,
            envelope.idempotency_key,
            command,
            request_sha256,
            committed,
        )

    def _asset_summaries(
        self, task_id: str, asset_ids: list[str]
    ) -> list[dict[str, Any]]:
        by_id = {
            item["manifest"]["asset_id"]: item
            for item in self.assets.list_assets(task_id)
        }
        values: list[dict[str, Any]] = []
        for asset_id in asset_ids:
            item = by_id.get(asset_id)
            if item is None:
                raise HarnessError(
                    "ASSET_CORRUPTED",
                    "The intake references an unavailable uploaded asset.",
                    {"asset_id": asset_id},
                )
            values.append(
                {
                    **self._asset_summary(item["manifest"]),
                    "integrity_status": item["integrity_status"],
                }
            )
        return values

    @staticmethod
    def _asset_summary(manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "asset_id": manifest["asset_id"],
            "filename": Path(manifest["relative_path"]).name,
            "mime_type": manifest["mime_type"],
            "size_bytes": manifest["size_bytes"],
            "sha256": manifest["sha256"],
            "description": manifest["description"],
            "created_at": manifest["created_at"],
        }

    def _request_digest(self, command: str, request: dict[str, Any]) -> str:
        return self.store.idempotency.request_digest(command, request)

    @staticmethod
    def _derived_key(prefix: str, identity: str) -> str:
        return f"{prefix}-{identity[:40]}"

    @staticmethod
    def _provisional_title(prompt: str) -> str:
        first_line = next((line.strip() for line in prompt.splitlines() if line.strip()), prompt)
        compact = " ".join(first_line.split())
        return compact if len(compact) <= 64 else f"{compact[:63]}…"

    @staticmethod
    def _require_human(envelope: CommandEnvelope) -> None:
        if envelope.actor_type != "human":
            raise HarnessError(
                "VALIDATION_ERROR", "Only a human may change a web task intake."
            )

    @staticmethod
    def _allow_commutative_revision(
        task_id: str, expected: int, actual: int
    ) -> None:
        if expected > actual:
            TaskIntakeService._raise_revision(
                "task_intake", task_id, expected, actual
            )

    @staticmethod
    def _raise_revision(
        object_type: str,
        object_id: str,
        expected: int,
        actual: int,
    ) -> None:
        raise HarnessError(
            "REVISION_CONFLICT",
            "The object revision changed before this command committed.",
            {
                "object_type": object_type,
                "object_id": object_id,
                "expected_revision": expected,
                "actual_revision": actual,
            },
        )
