"""Crash recovery for asset import and publication transactions."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, fsync_directory, read_json
from ..storage.locks import FileLock
from ..storage.ndjson import append_record, recover_records
from ..storage.paths import normalized_relative_path, resolve_task_path
from ..storage.repository import utc_now
from ..storage.safe_open import is_link_or_reparse
from .asset_files import detect_mime, file_digest, kind_for_mime

CrashHook = Callable[[str], None]


class AssetRecoveryMixin:
    """Resume durable asset intents without growing the public asset facade."""

    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    def recover(self) -> list[dict[str, Any]]:
        """Finish committed-intent publications and quarantine corrupted assets."""

        results: list[dict[str, Any]] = []
        tasks_root = self.store.layout.control_root / "tasks"
        for task_dir in sorted(tasks_root.iterdir() if tasks_root.exists() else []):
            if not task_dir.is_dir():
                continue
            task_id = task_dir.name
            transaction_dir = self._transaction_dir(task_id)
            with FileLock(self._asset_lock(task_id), self.store.lock_timeout_seconds):
                for record_path in sorted(transaction_dir.glob("*.json")):
                    record = read_json(record_path)
                    if record.get("transaction_type") == "import":
                        results.append(self._resume_import(record_path))
                    elif record.get("transaction_type") == "publication":
                        results.append(self._resume_publication(record_path, None))
                    elif record.get("transaction_type") == "publication_batch":
                        results.append(self._resume_publication_batch(record_path, None))
                workspace = self.store.layout.workspace_root / "tasks" / task_id
                for temporary in workspace.glob("resources/shared/*/.*.tmp"):
                    publication_id = temporary.name[1:-4]
                    owned = any(
                        read_json(path).get("publication_id") == publication_id
                        for path in transaction_dir.glob("*.json")
                    )
                    if not owned:
                        temporary.unlink(missing_ok=True)
                for item in self._visible_asset_events(task_id):
                    try:
                        self.verify_asset(task_id, item["manifest"]["asset_id"])
                    except HarnessError as exc:
                        if exc.code == "ASSET_CORRUPTED":
                            results.append(
                                {
                                    "asset_id": item["manifest"]["asset_id"],
                                    "status": "CORRUPTED",
                                }
                            )
                        else:
                            raise
        return results

    def _resume_import(self, record_path: Path) -> dict[str, Any]:
        record = read_json(record_path)
        task_id = record["manifest"]["task_id"]
        asset_id = record["manifest"]["asset_id"]
        event = next(
            (
                item
                for item in recover_records(self._event_path(task_id))
                if item.get("event_type") == "ASSET_IMPORTED" and item.get("asset_id") == asset_id
            ),
            None,
        )
        if event is not None and event.get("request_sha256") != record["request_sha256"]:
            raise HarnessError(
                "IDEMPOTENCY_CONFLICT",
                "The committed import event does not match its transaction.",
            )
        try:
            self._verify_manifest_file(task_id, record["manifest"])
        except HarnessError:
            self._mark_corrupted(task_id, asset_id, "import file changed before commit")
            raise
        if event is None:
            committed_at = utc_now()
            append_record(
                self._event_path(task_id),
                {
                    "event_id": f"evt_{uuid.uuid4().hex}",
                    "event_type": "ASSET_IMPORTED",
                    "asset_id": asset_id,
                    "manifest": record["manifest"],
                    "manifest_relpath": record["manifest_relpath"],
                    "source": {"type": record["source"]},
                    "request_sha256": record["request_sha256"],
                    "idempotency_key": record["idempotency_key"],
                    "occurred_at": committed_at,
                },
            )
        else:
            committed_at = event["occurred_at"]
        if record["state"] != "COMMITTED":
            record.update({"state": "COMMITTED", "committed_at": committed_at})
            atomic_write_json(record_path, record)
        return deepcopy(record["manifest"])

    def _resume_publication(
        self, record_path: Path, crash_hook: CrashHook | None
    ) -> dict[str, Any]:
        record = read_json(record_path)
        if record["state"] == "COMMITTED":
            if record["request"].get("batch_id") is None:
                self.verify_asset(record["task_id"], record["asset_id"])
            else:
                self._verify_manifest_file(record["task_id"], record["manifest"])
            self._publication_temporary_path(record).unlink(missing_ok=True)
            self._publication_manifest_temporary_path(record).unlink(missing_ok=True)
            return deepcopy(record["manifest"])
        task_id = record["task_id"]
        committed_event = self._publication_event(task_id, record["publication_id"])
        if committed_event is not None:
            self._verify_manifest_file(task_id, committed_event["manifest"])
            record.update(
                {
                    "state": "COMMITTED",
                    "manifest": committed_event["manifest"],
                    "committed_at": committed_event["occurred_at"],
                }
            )
            atomic_write_json(record_path, record)
            self._publication_temporary_path(record).unlink(missing_ok=True)
            self._publication_manifest_temporary_path(record).unlink(missing_ok=True)
            return deepcopy(committed_event["manifest"])
        workspace = self.store.layout.workspace_root / "tasks" / task_id
        destination_parts = normalized_relative_path(record["destination_relpath"])
        shared_root = resolve_task_path(workspace, "resources/shared")
        destination_directory = shared_root / record["asset_id"]
        if destination_directory.exists() and is_link_or_reparse(destination_directory):
            self._invalid("The publication directory cannot be a symlink.")
        destination_directory.mkdir(mode=0o700, exist_ok=True)
        if is_link_or_reparse(destination_directory) or not destination_directory.is_dir():
            self._invalid("The publication directory is invalid.")
        expected_directory = workspace.joinpath(*destination_parts.parts[:-1])
        try:
            destination_matches = destination_directory.samefile(expected_directory)
        except OSError:
            destination_matches = False
        if not destination_matches:
            self._invalid("The publication destination does not match its asset identity.")
        destination = resolve_task_path(
            workspace,
            record["destination_relpath"],
            allowed_prefixes=("resources/shared",),
            require_exists=False,
        )
        temporary = self._publication_temporary_path(record)

        if record["state"] == "PREPARED":
            if destination.exists():
                self._invalid("The publication destination is already occupied.")
            source = resolve_task_path(
                workspace,
                record["source_relative_path"],
                allowed_prefixes=(f"instances/{record['instance_id']}/outputs",),
            )
            if temporary.exists():
                temporary.unlink()
            size, sha256 = self._copy_file(source, temporary, self.max_file_bytes)
            mime_type = detect_mime(temporary, source.name)
            try:
                self._validate_mime(mime_type)
            except HarnessError:
                temporary.unlink(missing_ok=True)
                raise
            record.update(
                {
                    "copied_size_bytes": size,
                    "copied_sha256": sha256,
                    "copied_mime_type": mime_type,
                    "state": "COPIED",
                }
            )
            atomic_write_json(record_path, record)
            if crash_hook:
                crash_hook("after_temporary_copy")
        if not temporary.is_file() or is_link_or_reparse(temporary):
            self._corrupted(record["asset_id"])
        if destination.exists():
            if not self._same_regular_file(temporary, destination):
                self._invalid("The publication destination is not owned by this transaction.")
        else:
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                self._invalid("The publication destination is already occupied.")
            if os.name != "nt":
                os.chmod(destination, 0o440)
            fsync_directory(destination.parent)
            if crash_hook:
                crash_hook("after_final_rename")

        final_size, final_sha256 = file_digest(destination)
        final_mime = detect_mime(destination, destination.name)
        expected = (
            record.get("copied_size_bytes"),
            record.get("copied_sha256"),
            record.get("copied_mime_type"),
        )
        if None in expected or expected != (final_size, final_sha256, final_mime):
            self._mark_corrupted(task_id, record["asset_id"], "final file changed before commit")
            raise HarnessError(
                "ASSET_CORRUPTED",
                "The published asset does not match its publication transaction.",
                {"asset_id": record["asset_id"]},
            )
        manifest = record.get("prepared_manifest")
        if manifest is None:
            now = utc_now()
            manifest = {
                "schema_version": "1.0",
                "asset_id": record["asset_id"],
                "task_id": task_id,
                "producer_instance_id": record["instance_id"],
                "kind": kind_for_mime(final_mime),
                "role": record["request"]["role"],
                "relative_path": record["destination_relpath"],
                "mime_type": final_mime,
                "size_bytes": final_size,
                "sha256": final_sha256,
                "description": record["request"]["description"],
                "derivation": deepcopy(record["request"].get("derivation")),
                "source_relative_path": record["source_relative_path"],
                "publication_id": record["publication_id"],
                "published_at": now,
                "created_at": record["created_at"],
            }
            record["prepared_manifest"] = manifest
            atomic_write_json(record_path, record)
        else:
            now = manifest["published_at"]
        self.store.contracts.validate("asset-manifest", manifest)
        manifest_path = workspace / record["manifest_relpath"]
        manifest_temporary = self._publication_manifest_temporary_path(record)
        if manifest_path.exists():
            if not self._same_regular_file(manifest_temporary, manifest_path):
                self._invalid("The publication manifest target is already occupied.")
        else:
            manifest_temporary.unlink(missing_ok=True)
            atomic_write_json(manifest_temporary, manifest, mode=0o640)
            try:
                os.link(manifest_temporary, manifest_path, follow_symlinks=False)
            except FileExistsError:
                self._invalid("The publication manifest target is already occupied.")
            if os.name != "nt":
                os.chmod(manifest_path, 0o440)
            fsync_directory(manifest_path.parent)
        if crash_hook:
            crash_hook("after_manifest_rename")
        if not self._publication_event(task_id, record["publication_id"]):
            append_record(
                self._event_path(task_id),
                {
                    "event_id": f"evt_{uuid.uuid4().hex}",
                    "event_type": "ASSET_PUBLISHED",
                    "asset_id": record["asset_id"],
                    "publication_id": record["publication_id"],
                    "batch_id": record["request"].get("batch_id"),
                    "manifest": manifest,
                    "manifest_relpath": record["manifest_relpath"],
                    "request_sha256": record["request_sha256"],
                    "idempotency_key": record["idempotency_key"],
                    "occurred_at": now,
                },
            )
        if crash_hook:
            crash_hook("after_publication_event")
        record.update({"state": "COMMITTED", "manifest": manifest, "committed_at": now})
        atomic_write_json(record_path, record)
        temporary.unlink(missing_ok=True)
        manifest_temporary.unlink(missing_ok=True)
        return deepcopy(manifest)

    def _resume_publication_batch(
        self, record_path: Path, crash_hook: CrashHook | None = None
    ) -> dict[str, Any]:
        record = read_json(record_path)
        task_id = record["task_id"]
        batch_id = record["batch_id"]
        bundle_manifest = record["request"].get("bundle_manifest")
        bundle_path = None
        if bundle_manifest is not None:
            self.store.contracts.validate("bundle-manifest", bundle_manifest)
            if (
                bundle_manifest["task_id"] != task_id
                or bundle_manifest["instance_id"] != record["instance_id"]
                or bundle_manifest["publication_batch_id"] != batch_id
            ):
                raise HarnessError(
                    "ASSET_VALIDATION_FAILED",
                    "The publication batch contains an invalid bundle manifest.",
                )
            workspace = self.store.layout.initialize_task(task_id)[1]
            bundle_path = (
                workspace / "resources" / "bundles" / f"{bundle_manifest['bundle_id']}.json"
            )
            if bundle_path.exists() and read_json(bundle_path) != bundle_manifest:
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The bundle manifest path already contains another bundle.",
                )
        committed = self._publication_batch_event(task_id, batch_id)
        if committed is not None:
            if committed["request_sha256"] != record["request_sha256"]:
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The committed publication batch does not match its transaction.",
                )
            if record["state"] != "COMMITTED":
                record.update(
                    {
                        "state": "COMMITTED",
                        "committed_at": committed["occurred_at"],
                        "result": committed,
                    }
                )
                atomic_write_json(record_path, record)
            if bundle_path is not None and not bundle_path.exists():
                atomic_write_json(bundle_path, bundle_manifest, mode=0o440)
            return deepcopy(committed)

        asset_ids = []
        for item in record["request"]["assets"]:
            publication = self._publication_event(task_id, item["publication_id"])
            if (
                publication is None
                or publication.get("batch_id") != batch_id
                or publication["asset_id"] != item["asset_id"]
                or publication["manifest"]["sha256"] != item["sha256"]
                or publication["manifest"]["task_id"] != task_id
                or publication["manifest"].get("producer_instance_id") != record["instance_id"]
            ):
                raise HarnessError(
                    "ASSET_VALIDATION_FAILED",
                    "A publication batch is missing one prepared asset.",
                    {"batch_id": batch_id, "asset_id": item["asset_id"]},
                )
            self._verify_manifest_file(task_id, publication["manifest"])
            asset_ids.append(item["asset_id"])
        if bundle_path is not None and not bundle_path.exists():
            atomic_write_json(bundle_path, bundle_manifest, mode=0o440)
        if crash_hook:
            crash_hook("after_bundle_manifest_write")
        now = utc_now()
        event = {
            "event_id": f"evt_{uuid.uuid4().hex}",
            "event_type": "ASSET_PUBLICATION_BATCH_COMMITTED",
            "batch_id": batch_id,
            "task_id": task_id,
            "instance_id": record["instance_id"],
            "asset_ids": asset_ids,
            "request_sha256": record["request_sha256"],
            "occurred_at": now,
        }
        if bundle_manifest is not None:
            event["bundle_id"] = bundle_manifest["bundle_id"]
        append_record(self._event_path(task_id), event)
        if crash_hook:
            crash_hook("after_publication_batch_event")
        record.update({"state": "COMMITTED", "committed_at": now, "result": event})
        atomic_write_json(record_path, record)
        return deepcopy(event)
