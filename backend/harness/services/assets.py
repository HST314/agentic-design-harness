"""Task workspace, multimodal import and controlled delivery publication."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, cast
from urllib.parse import quote

from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, digest_json, fsync_directory, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.ndjson import append_record, recover_records
from ..storage.paths import normalized_relative_path, resolve_task_path
from ..storage.repository import utc_now
from ..storage.store import FileStateStore
from .asset_browser import (
    browser_roots,
    resolve_committed_browser_path,
    verify_browser_event_path,
)
from .asset_files import detect_mime, file_digest, kind_for_mime
from .asset_reader import OpenedCommittedAsset, open_committed_asset
from .asset_recovery import AssetRecoveryMixin

CrashHook = Callable[[str], None]

SAFE_PREVIEW_MIME = {
    "application/json",
    "text/markdown",
    "text/plain",
}
DEFAULT_ALLOWED_MIME = {
    "application/json",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/markdown",
    "text/plain",
}
_SAFE_FILENAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,255}$")


class AssetService(AssetRecoveryMixin):
    """Owns every transition from untrusted bytes to a registered task asset."""

    def __init__(
        self,
        store: FileStateStore,
        *,
        max_file_bytes: int = 50 * 1024 * 1024,
        max_task_bytes: int = 250 * 1024 * 1024,
        allowed_mime_types: set[str] | None = None,
        preview_limit_bytes: int = 512 * 1024,
    ) -> None:
        self.store = store
        self.max_file_bytes = max_file_bytes
        self.max_task_bytes = max_task_bytes
        self.allowed_mime_types = allowed_mime_types or DEFAULT_ALLOWED_MIME
        self.preview_limit_bytes = preview_limit_bytes

    def initialize_task_workspace(self, task_id: str) -> Path:
        self._require_task(task_id)
        _, workspace = self.store.layout.initialize_task(task_id)
        return workspace

    def initialize_instance_workspace(self, task_id: str, instance_id: str) -> Path:
        instance = self._require_instance(task_id, instance_id)
        if instance["status"] == "ARCHIVED":
            raise HarnessError("INVALID_STATE_TRANSITION", "An archived instance is read-only.")
        return self.store.layout.initialize_instance(task_id, instance_id)

    def import_bytes(
        self,
        task_id: str,
        *,
        filename: str,
        content: bytes,
        description: str,
        source: str,
        idempotency_key: str,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        with tempfile.TemporaryFile("w+b") as stream:
            stream.write(content)
            stream.seek(0)
            return self._import_stream(
                task_id,
                filename=filename,
                stream=stream,
                description=description,
                source=source,
                idempotency_key=idempotency_key,
                crash_hook=crash_hook,
            )

    def import_file(
        self,
        task_id: str,
        source_path: Path,
        *,
        filename: str | None = None,
        description: str,
        source: str,
        idempotency_key: str,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        if source_path.is_symlink() or not source_path.is_file():
            self._invalid("The import source must be a regular non-symlink file.")
        descriptor = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            return self._import_stream(
                task_id,
                filename=filename or source_path.name,
                stream=stream,
                description=description,
                source=source,
                idempotency_key=idempotency_key,
                crash_hook=crash_hook,
            )

    def _import_stream(
        self,
        task_id: str,
        *,
        filename: str,
        stream: BinaryIO,
        description: str,
        source: str,
        idempotency_key: str,
        crash_hook: CrashHook | None,
    ) -> dict[str, Any]:
        self._require_task(task_id)
        self._validate_filename(filename)
        if len(description) > 4000 or not source or len(source) > 512:
            self._invalid("Asset description or source metadata is invalid.")
        self._validate_idempotency_key(idempotency_key)
        content_size = 0
        content_digest = hashlib.sha256()
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as staged:
            while chunk := stream.read(1024 * 1024):
                content_size += len(chunk)
                if content_size > self.max_file_bytes:
                    self._invalid("The asset exceeds the per-file size limit.")
                content_digest.update(chunk)
                staged.write(chunk)
            staged.seek(0)
            content_sha256 = content_digest.hexdigest()
            return self._commit_import(
                task_id,
                filename=filename,
                stream=cast(BinaryIO, staged),
                description=description,
                source=source,
                idempotency_key=idempotency_key,
                content_size=content_size,
                content_sha256=content_sha256,
                crash_hook=crash_hook,
            )

    def _commit_import(
        self,
        task_id: str,
        *,
        filename: str,
        stream: BinaryIO,
        description: str,
        source: str,
        idempotency_key: str,
        content_size: int,
        content_sha256: str,
        crash_hook: CrashHook | None,
    ) -> dict[str, Any]:
        workspace = self.initialize_task_workspace(task_id)
        lock = FileLock(self._asset_lock(task_id), self.store.lock_timeout_seconds)
        transaction_id = f"imp_{hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]}"
        record_path = self._transaction_dir(task_id) / f"{transaction_id}.json"
        request: dict[str, Any] = {
            "filename": filename,
            "description": description,
            "source": source,
            "content_size": content_size,
            "content_sha256": content_sha256,
        }
        with lock:
            existing = read_json(record_path) if record_path.exists() else None
            if existing is not None:
                self._check_request(existing, "import", request)
                return self._resume_import(record_path)

            current_total = sum(
                item["manifest"]["size_bytes"] for item in self.list_assets(task_id)
            )
            asset_digest = hashlib.sha256(f"{task_id}:{idempotency_key}".encode()).hexdigest()
            asset_id = f"a_imp_{asset_digest[:20]}"
            destination_dir = workspace / "inputs" / "original" / asset_id
            destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination = destination_dir / filename
            size, sha256 = self._copy_stream(stream, destination, self.max_file_bytes)
            if (size, sha256) != (content_size, content_sha256):
                raise RuntimeError("staged import bytes changed before commit")
            if current_total + size > self.max_task_bytes:
                destination.unlink(missing_ok=True)
                self._invalid("The task upload size limit would be exceeded.")
            mime_type = detect_mime(destination, filename)
            try:
                self._validate_mime(mime_type)
            except HarnessError:
                destination.unlink(missing_ok=True)
                raise
            os.chmod(destination, 0o440)
            now = utc_now()
            manifest = {
                "schema_version": "1.0",
                "asset_id": asset_id,
                "task_id": task_id,
                "producer_instance_id": None,
                "kind": kind_for_mime(mime_type),
                "role": "user_reference",
                "relative_path": destination.relative_to(workspace).as_posix(),
                "mime_type": mime_type,
                "size_bytes": size,
                "sha256": sha256,
                "description": description,
                "created_at": now,
            }
            self.store.contracts.validate("asset-manifest", manifest)
            manifest_relpath = f"inputs/manifests/{asset_id}.json"
            atomic_write_json(workspace / manifest_relpath, manifest, mode=0o640)
            atomic_write_json(
                workspace / "inputs" / "manifests" / f"{asset_id}.source.json",
                {
                    "asset_id": asset_id,
                    "filename": filename,
                    "source": source,
                    "recorded_at": now,
                },
                mode=0o640,
            )
            record = {
                "transaction_type": "import",
                "transaction_id": transaction_id,
                "idempotency_key": idempotency_key,
                "request_sha256": digest_json({"kind": "import", **request}),
                "source": source,
                "state": "PREPARED",
                "manifest": manifest,
                "manifest_relpath": manifest_relpath,
            }
            atomic_write_json(record_path, record)
            if crash_hook:
                crash_hook("after_import_prepare")
            result = self._resume_import(record_path)
            if crash_hook:
                crash_hook("after_import_event")
            return result

    def select_inputs(
        self,
        task_id: str,
        asset_ids: list[str],
        *,
        manifest_id: str,
    ) -> dict[str, Any]:
        self._require_task(task_id)
        validate_identifier(manifest_id, "manifest_id")
        if not asset_ids or len(asset_ids) != len(set(asset_ids)):
            self._invalid("Selected inputs must contain unique registered asset ids.")
        workspace = self.initialize_task_workspace(task_id)
        with FileLock(self._asset_lock(task_id), self.store.lock_timeout_seconds):
            registered = {item["manifest"]["asset_id"]: item for item in self.list_assets(task_id)}
            entries: list[dict[str, str]] = []
            task_card_inputs: list[dict[str, str]] = []
            for asset_id in asset_ids:
                item = registered.get(asset_id)
                if item is None or item["integrity_status"] != "VERIFIED":
                    raise HarnessError(
                        "ASSET_VALIDATION_FAILED",
                        "Only verified registered assets may be selected.",
                        {"asset_id": asset_id},
                    )
                manifest = item["manifest"]
                source_path = resolve_task_path(workspace, manifest["relative_path"])
                selected_dir = workspace / "inputs" / "selected" / asset_id
                selected_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                selected_path = selected_dir / source_path.name
                self._copy_file(source_path, selected_path, self.max_file_bytes)
                os.chmod(selected_path, 0o440)
                manifest_relpath = self._manifest_relpath(manifest)
                entries.append(
                    {
                        "asset_id": asset_id,
                        "manifest_relpath": manifest_relpath,
                        "selected_relpath": selected_path.relative_to(workspace).as_posix(),
                    }
                )
                task_card_inputs.append(
                    {"asset_id": asset_id, "manifest_relpath": manifest_relpath}
                )
            selected_manifest = {
                "schema_version": "1.0",
                "manifest_id": manifest_id,
                "task_id": task_id,
                "assets": entries,
                "created_at": utc_now(),
            }
            relative = f"inputs/manifests/{manifest_id}.json"
            atomic_write_json(workspace / relative, selected_manifest, mode=0o640)
            return {
                "manifest": selected_manifest,
                "manifest_relpath": relative,
                "task_card_inputs": task_card_inputs,
            }

    def publish_delivery(
        self,
        task_id: str,
        instance_id: str,
        *,
        source_relative_path: str,
        role: str,
        description: str,
        idempotency_key: str,
        batch_id: str | None = None,
        derivation: dict[str, Any] | None = None,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        instance = self._require_instance(task_id, instance_id)
        if instance["status"] == "ARCHIVED":
            raise HarnessError("INVALID_STATE_TRANSITION", "An archived instance is read-only.")
        self._validate_idempotency_key(idempotency_key)
        if batch_id is not None:
            validate_identifier(batch_id, "batch_id")
        if not role or len(role) > 128 or len(description) > 4000:
            self._invalid("Delivery role or description is invalid.")
        normalized = normalized_relative_path(source_relative_path).as_posix()
        expected_prefix = f"instances/{instance_id}/outputs"
        workspace = self.initialize_task_workspace(task_id)
        source = resolve_task_path(
            workspace,
            normalized,
            allowed_prefixes=(expected_prefix,),
        )
        if source.is_symlink() or not source.is_file():
            self._invalid("A delivery source must be a regular instance output file.")
        request: dict[str, Any] = {
            "instance_id": instance_id,
            "source_relative_path": normalized,
            "role": role,
            "description": description,
        }
        if batch_id is not None:
            request["batch_id"] = batch_id
        if derivation is not None:
            request["derivation"] = deepcopy(derivation)
        transaction_digest = hashlib.sha256(f"{instance_id}:{idempotency_key}".encode()).hexdigest()
        transaction_id = f"pub_{transaction_digest[:24]}"
        asset_digest = hashlib.sha256(f"{task_id}:{transaction_id}".encode()).hexdigest()
        asset_id = f"a_pub_{asset_digest[:20]}"
        record_path = self._transaction_dir(task_id) / f"{transaction_id}.json"
        with FileLock(self._asset_lock(task_id), self.store.lock_timeout_seconds):
            if record_path.exists():
                record = read_json(record_path)
                self._check_request(record, "publication", request)
            else:
                destination_relpath = f"resources/shared/{asset_id}/{source.name}"
                record = {
                    "transaction_type": "publication",
                    "transaction_id": transaction_id,
                    "publication_id": transaction_id,
                    "asset_id": asset_id,
                    "task_id": task_id,
                    "instance_id": instance_id,
                    "idempotency_key": idempotency_key,
                    "request": request,
                    "request_sha256": digest_json({"kind": "publication", **request}),
                    "source_relative_path": normalized,
                    "destination_relpath": destination_relpath,
                    "manifest_relpath": f"resources/manifests/{asset_id}.json",
                    "state": "PREPARED",
                    "created_at": utc_now(),
                }
                atomic_write_json(record_path, record)
            return self._resume_publication(record_path, crash_hook)

    def inspect_delivery(
        self,
        task_id: str,
        instance_id: str,
        *,
        source_relative_path: str,
        role: str,
        description: str,
        expected_sha256: str | None = None,
        derivation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Inspect one private output without creating a public transaction or event."""

        self._require_instance(task_id, instance_id)
        if not role or len(role) > 128 or len(description) > 4000:
            self._invalid("Delivery role or description is invalid.")
        normalized = normalized_relative_path(source_relative_path).as_posix()
        workspace = self.initialize_task_workspace(task_id)
        source = resolve_task_path(
            workspace,
            normalized,
            allowed_prefixes=(f"instances/{instance_id}/outputs",),
        )
        if source.is_symlink() or not source.is_file():
            self._invalid("A delivery source must be a regular instance output file.")
        size_bytes, sha256 = file_digest(source)
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise HarnessError(
                "ASSET_CORRUPTED",
                "The collected delivery digest changed before publication.",
                {"expected_sha256": expected_sha256, "actual_sha256": sha256},
            )
        mime_type = detect_mime(source, source.name)
        self._validate_mime(mime_type)
        candidate = {
            "source_relative_path": normalized,
            "kind": kind_for_mime(mime_type),
            "role": role,
            "description": description,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "derivation": deepcopy(derivation),
        }
        self._validate_derivation(candidate)
        return candidate

    def commit_publication_batch(
        self,
        task_id: str,
        instance_id: str,
        *,
        batch_id: str,
        manifests: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Commit one visibility gate for a fully validated set of publications."""

        self._require_instance(task_id, instance_id)
        validate_identifier(batch_id, "batch_id")
        if not manifests:
            self._invalid("A publication batch cannot be empty.")
        for manifest in manifests:
            self.store.contracts.validate("asset-manifest", manifest)
            if (
                manifest["task_id"] != task_id
                or manifest.get("producer_instance_id") != instance_id
            ):
                self._invalid("Every batch asset must belong to its producing instance.")
        ordered = sorted(manifests, key=lambda item: item["asset_id"])
        if len({item["asset_id"] for item in ordered}) != len(ordered):
            self._invalid("A publication batch cannot contain duplicate assets.")
        request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "batch_id": batch_id,
            "assets": [
                {
                    "asset_id": item["asset_id"],
                    "publication_id": item["publication_id"],
                    "sha256": item["sha256"],
                }
                for item in ordered
            ],
        }
        record_path = self._transaction_dir(task_id) / f"{batch_id}.json"
        with FileLock(self._asset_lock(task_id), self.store.lock_timeout_seconds):
            if record_path.exists():
                record = read_json(record_path)
                self._check_request(record, "publication_batch", request)
            else:
                record = {
                    "transaction_type": "publication_batch",
                    "transaction_id": batch_id,
                    "batch_id": batch_id,
                    "task_id": task_id,
                    "instance_id": instance_id,
                    "request": request,
                    "request_sha256": digest_json({"kind": "publication_batch", **request}),
                    "state": "PREPARED",
                    "created_at": utc_now(),
                }
                atomic_write_json(record_path, record)
            return self._resume_publication_batch(record_path)

    def list_publication_batch(
        self, task_id: str, instance_id: str, batch_id: str
    ) -> list[dict[str, Any]]:
        """List verified private manifests prepared for one publication batch."""

        self._require_instance(task_id, instance_id)
        validate_identifier(batch_id, "batch_id")
        manifests: list[dict[str, Any]] = []
        for event in recover_records(self._event_path(task_id)):
            if event.get("event_type") != "ASSET_PUBLISHED" or event.get("batch_id") != batch_id:
                continue
            manifest = event["manifest"]
            if manifest.get("producer_instance_id") != instance_id:
                self._invalid("A publication batch contains an asset from another instance.")
            self._verify_manifest_file(task_id, manifest)
            manifests.append(deepcopy(manifest))
        return sorted(manifests, key=lambda item: item["asset_id"])

    def replay_delivery(
        self,
        task_id: str,
        instance_id: str,
        *,
        source_relative_path: str,
        role: str,
        description: str,
        idempotency_key: str,
        batch_id: str | None = None,
        derivation: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return an exact committed publication replay without creating one."""

        self._require_instance(task_id, instance_id)
        self._validate_idempotency_key(idempotency_key)
        normalized = normalized_relative_path(source_relative_path).as_posix()
        request: dict[str, Any] = {
            "instance_id": instance_id,
            "source_relative_path": normalized,
            "role": role,
            "description": description,
        }
        if batch_id is not None:
            validate_identifier(batch_id, "batch_id")
            request["batch_id"] = batch_id
        if derivation is not None:
            request["derivation"] = deepcopy(derivation)
        transaction_digest = hashlib.sha256(f"{instance_id}:{idempotency_key}".encode()).hexdigest()
        record_path = self._transaction_dir(task_id) / f"pub_{transaction_digest[:24]}.json"
        with FileLock(self._asset_lock(task_id), self.store.lock_timeout_seconds):
            if not record_path.exists():
                return None
            record = read_json(record_path)
            self._check_request(record, "publication", request)
            return self._resume_publication(record_path, None)


    def list_assets(self, task_id: str) -> list[dict[str, Any]]:
        self._require_task(task_id)
        values: list[dict[str, Any]] = []
        for event in self._visible_asset_events(task_id):
            manifest = event["manifest"]
            status = self._integrity_status(task_id, manifest["asset_id"])
            values.append({"manifest": deepcopy(manifest), "integrity_status": status})
        return values

    def verify_asset(self, task_id: str, asset_id: str) -> dict[str, Any]:
        validate_identifier(asset_id, "asset_id")
        event = next(
            (
                item
                for item in self._visible_asset_events(task_id)
                if item["manifest"]["asset_id"] == asset_id
            ),
            None,
        )
        if event is None:
            self._invalid("The asset has no committed import or publication fact.")
        manifest = event["manifest"]
        try:
            self._verify_manifest_file(task_id, manifest)
        except HarnessError:
            self._mark_corrupted(task_id, asset_id, "manifest digest does not match final file")
            raise
        return deepcopy(manifest)

    def list_files(self, task_id: str, group: str = "all") -> list[dict[str, Any]]:
        workspace = self.initialize_task_workspace(task_id)
        if group not in {"inputs", "shared", "instances", "all"}:
            self._invalid("Unknown resource-browser group.")
        entries: list[dict[str, Any]] = []
        roots = browser_roots(workspace, group)
        for event in self._visible_asset_events(task_id):
            manifest = event["manifest"]
            try:
                self._verify_manifest_file(task_id, manifest)
                verified = [
                    (
                        relative,
                        verify_browser_event_path(
                            workspace,
                            relative,
                            event,
                            self._verify_manifest_file,
                            verify_asset=False,
                        ),
                    )
                    for relative in (
                        manifest["relative_path"],
                        event["manifest_relpath"],
                    )
                ]
            except HarnessError:
                self._mark_corrupted(
                    task_id, event["asset_id"], "browser file failed live verification"
                )
                continue
            for relative, path in verified:
                if not any(path == root or root in path.parents for root in roots):
                    continue
                if relative == manifest["relative_path"]:
                    mime_type = manifest["mime_type"]
                    size = manifest["size_bytes"]
                    sha256 = manifest["sha256"]
                else:
                    mime_type = detect_mime(path, path.name)
                    size, sha256 = file_digest(path)
                entries.append(
                    {
                        "relative_path": relative,
                        "filename": path.name,
                        "mime_type": mime_type,
                        "size_bytes": size,
                        "sha256": sha256,
                        "previewable": size <= self.preview_limit_bytes
                        and (
                            mime_type.startswith("image/")
                            or mime_type in SAFE_PREVIEW_MIME
                        ),
                    }
                )
        return entries

    def preview(self, task_id: str, relative_path: str) -> dict[str, Any]:
        workspace = self.initialize_task_workspace(task_id)
        with self._open_browser_file(task_id, workspace, relative_path) as opened:
            if opened.size_bytes > self.preview_limit_bytes:
                self._invalid("The file cannot be safely previewed.")
            if opened.mime_type.startswith("image/"):
                return {
                    "mime_type": opened.mime_type,
                    "content": opened.stream.read(),
                    "encoding": None,
                }
            if opened.mime_type not in SAFE_PREVIEW_MIME:
                self._invalid("This file type is download-only.")
            try:
                content = opened.stream.read().decode("utf-8")
                if opened.mime_type == "application/json":
                    json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._invalid(f"The preview content is invalid: {type(exc).__name__}.")
            return {
                "mime_type": opened.mime_type,
                "content": content,
                "encoding": "utf-8",
            }

    def download(self, task_id: str, relative_path: str) -> dict[str, Any]:
        workspace = self.initialize_task_workspace(task_id)
        opened = self._open_browser_file(task_id, workspace, relative_path)
        filename = quote(opened.filename, safe="")
        return {
            "stream": opened.stream,
            "mime_type": opened.mime_type,
            "size_bytes": opened.size_bytes,
            "sha256": opened.sha256,
            "headers": {
                "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        }

    def _open_browser_file(
        self, task_id: str, workspace: Path, relative_path: str
    ) -> OpenedCommittedAsset:
        try:
            return open_committed_asset(
                workspace, relative_path, self._visible_asset_events(task_id)
            )
        except HarnessError as exc:
            if exc.code == "ASSET_CORRUPTED" and exc.details.get("asset_id"):
                self._mark_corrupted(
                    task_id,
                    str(exc.details["asset_id"]),
                    "browser file failed descriptor-safe verification",
                )
            raise

    def _visible_asset_events(self, task_id: str) -> list[dict[str, Any]]:
        records = recover_records(self._event_path(task_id))
        committed_batches = {
            event["batch_id"]: event
            for event in records
            if event.get("event_type") == "ASSET_PUBLICATION_BATCH_COMMITTED"
        }
        instance_statuses: dict[str, str | None] = {}
        latest: dict[str, dict[str, Any]] = {}
        for event in records:
            if event.get("event_type") in {"ASSET_IMPORTED", "ASSET_PUBLISHED"}:
                batch_id = event.get("batch_id")
                if batch_id is not None:
                    batch = committed_batches.get(batch_id)
                    producer = event["manifest"]["producer_instance_id"]
                    if producer not in instance_statuses:
                        instance = self.store.instance.get(task_id, producer)
                        instance_statuses[producer] = (
                            instance["status"] if instance is not None else None
                        )
                    if (
                        batch is None
                        or event["asset_id"] not in batch["asset_ids"]
                        or instance_statuses[producer] != "SUCCEEDED"
                    ):
                        continue
                latest[event["asset_id"]] = event
        return sorted(latest.values(), key=lambda item: (item["occurred_at"], item["asset_id"]))

    def _resolve_browser_path(self, task_id: str, workspace: Path, relative_path: str) -> Path:
        return resolve_committed_browser_path(
            workspace,
            relative_path,
            self._visible_asset_events(task_id),
            self._verify_manifest_file,
        )

    @staticmethod
    def _same_regular_file(first: Path, second: Path) -> bool:
        try:
            return (
                not first.is_symlink()
                and not second.is_symlink()
                and first.is_file()
                and second.is_file()
                and os.path.samestat(os.lstat(first), os.lstat(second))
            )
        except OSError:
            return False

    def _publication_temporary_path(self, record: dict[str, Any]) -> Path:
        workspace = self.store.layout.workspace_root / "tasks" / record["task_id"]
        destination = workspace / record["destination_relpath"]
        return destination.parent / f".{record['publication_id']}.tmp"

    def _publication_manifest_temporary_path(self, record: dict[str, Any]) -> Path:
        workspace = self.store.layout.workspace_root / "tasks" / record["task_id"]
        manifest = workspace / record["manifest_relpath"]
        return manifest.parent / f".{record['publication_id']}.tmp"

    def _verify_manifest_file(self, task_id: str, manifest: dict[str, Any]) -> None:
        workspace = self.store.layout.workspace_root / "tasks" / task_id
        path = resolve_task_path(workspace, manifest["relative_path"])
        if path.is_symlink() or not path.is_file():
            self._corrupted(manifest["asset_id"])
        size, sha256 = file_digest(path)
        mime_type = detect_mime(path, path.name)
        if (size, sha256, mime_type) != (
            manifest["size_bytes"],
            manifest["sha256"],
            manifest["mime_type"],
        ):
            self._corrupted(manifest["asset_id"])

    def _publication_event(self, task_id: str, publication_id: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in recover_records(self._event_path(task_id))
                if item.get("event_type") == "ASSET_PUBLISHED"
                and item.get("publication_id") == publication_id
            ),
            None,
        )

    def _publication_batch_event(self, task_id: str, batch_id: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in recover_records(self._event_path(task_id))
                if item.get("event_type") == "ASSET_PUBLICATION_BATCH_COMMITTED"
                and item.get("batch_id") == batch_id
            ),
            None,
        )

    def _integrity_status(self, task_id: str, asset_id: str) -> str:
        path = self._status_dir(task_id) / f"{asset_id}.json"
        return read_json(path)["status"] if path.exists() else "VERIFIED"

    def _mark_corrupted(self, task_id: str, asset_id: str, reason: str) -> None:
        path = self._status_dir(task_id) / f"{asset_id}.json"
        if path.exists() and read_json(path).get("status") == "CORRUPTED":
            return
        now = utc_now()
        atomic_write_json(
            path,
            {"asset_id": asset_id, "status": "CORRUPTED", "reason": reason, "detected_at": now},
        )
        append_record(
            self._event_path(task_id),
            {
                "event_id": f"evt_{uuid.uuid4().hex}",
                "event_type": "ASSET_CORRUPTED",
                "asset_id": asset_id,
                "reason": reason,
                "occurred_at": now,
            },
        )

    def _copy_file(self, source: Path, destination: Path, limit: int) -> tuple[int, str]:
        if source.is_symlink() or not source.is_file():
            self._invalid("The source must be a regular non-symlink file.")
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            return self._copy_stream(stream, destination, limit)

    def _copy_stream(self, stream: BinaryIO, destination: Path, limit: int) -> tuple[int, str]:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".upload", dir=destination.parent
        )
        temporary = Path(temporary_name)
        size = 0
        digest = hashlib.sha256()
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        self._invalid("The asset exceeds the per-file size limit.")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o640)
            fsync_directory(destination.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return size, digest.hexdigest()

    def _check_request(self, record: dict[str, Any], kind: str, request: dict[str, Any]) -> None:
        if record["request_sha256"] != digest_json({"kind": kind, **request}):
            raise HarnessError(
                "IDEMPOTENCY_CONFLICT",
                "The asset idempotency key was used for a different request.",
                {"transaction_type": kind},
            )

    def _require_task(self, task_id: str) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        task = self.store.task.get(task_id, task_id)
        if task is None:
            raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
        return task

    def _require_instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        validate_identifier(instance_id, "instance_id")
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None or instance["task_id"] != task_id:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        return instance

    def _event_path(self, task_id: str) -> Path:
        return self.store.layout.control_root / "tasks" / task_id / "events.ndjson"

    def _asset_lock(self, task_id: str) -> Path:
        return self.store.layout.control_root / "locks" / f"assets-{task_id}.lock"

    def _transaction_dir(self, task_id: str) -> Path:
        path = self.store.layout.control_root / "tasks" / task_id / "asset-transactions"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def _status_dir(self, task_id: str) -> Path:
        path = self.store.layout.control_root / "tasks" / task_id / "asset-status"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    @staticmethod
    def _manifest_relpath(manifest: dict[str, Any]) -> str:
        prefix = "resources" if manifest["producer_instance_id"] else "inputs"
        return f"{prefix}/manifests/{manifest['asset_id']}.json"

    @staticmethod
    def _validate_filename(filename: str) -> None:
        if not _SAFE_FILENAME.fullmatch(filename) or filename in {".", ".."}:
            raise HarnessError(
                "ASSET_VALIDATION_FAILED",
                "The uploaded filename is unsafe.",
                {"filename": filename},
            )

    def _validate_mime(self, mime_type: str) -> None:
        if mime_type not in self.allowed_mime_types:
            self._invalid("The detected MIME type is not allowed.")

    def _validate_derivation(self, candidate: dict[str, Any]) -> None:
        derivation = candidate.get("derivation")
        if derivation is None:
            return
        if not isinstance(derivation, dict) or (
            derivation.get("derived_sha256") != candidate["sha256"]
            or derivation.get("derived_mime_type") != candidate["mime_type"]
        ):
            self._invalid("Delivery derivation does not match the derived file.")
        self.store.contracts.validate("delivery-derivation", derivation)

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if not value or len(value) > 128 or "\x00" in value:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The idempotency key is invalid.",
                {"field": "idempotency_key"},
            )

    @staticmethod
    def _invalid(message: str) -> NoReturn:
        raise HarnessError("ASSET_VALIDATION_FAILED", message)

    @staticmethod
    def _corrupted(asset_id: str) -> NoReturn:
        raise HarnessError(
            "ASSET_CORRUPTED",
            "The asset no longer matches its committed manifest.",
            {"asset_id": asset_id},
        )
