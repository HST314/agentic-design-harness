"""Credential-safe audit events and durable runtime-settings counters."""

from __future__ import annotations

import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..storage.atomic import atomic_write_json, read_json
from ..storage.config_revision_io import validate_public_config_tree
from ..storage.locks import FileLock
from ..storage.ndjson import append_record
from ..storage.repository import utc_now
from ..storage.store import FileStateStore

RUNTIME_CONFIG_EVENTS = frozenset(
    {
        "CONFIG_PROPOSAL_CREATED",
        "CONFIG_PROPOSAL_CONFIRMED",
        "CONFIG_WAITING_SAFE_POINT",
        "CONFIG_REVISION_APPLIED",
        "CONFIG_REVISION_FAILED",
        "TASK_CONFIG_REBASED",
        "TASK_CONFIG_REBASE_REVIEW_REQUIRED",
        "CONFIG_SYNC_TOGGLED",
    }
)


class RuntimeConfigObservability:
    def __init__(self, store: FileStateStore) -> None:
        self.store = store
        self.metrics_path = store.layout.control_root / "runtime-config-metrics.json"
        self.metrics_lock = store.layout.control_root / "locks" / "runtime-config-metrics.lock"

    def event(
        self,
        task_id: str,
        event_type: str,
        *,
        actor: dict[str, str],
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        if event_type not in RUNTIME_CONFIG_EVENTS:
            raise ValueError("unsupported runtime configuration event")
        safe_fields = deepcopy(fields)
        result = str(safe_fields.pop("result", "COMMITTED"))
        record = {
            "event_id": f"evt_{uuid.uuid4().hex}",
            "event_type": event_type,
            "task_id": task_id,
            "actor": deepcopy(actor),
            "result": result,
            "occurred_at": utc_now(),
            **safe_fields,
        }
        validate_public_config_tree(record)
        append_record(
            self.store.layout.control_root / "tasks" / task_id / "events.ndjson",
            record,
        )
        return record

    def increment(self, name: str, amount: int = 1) -> None:
        allowed = {
            "task_rebase_succeeded",
            "task_rebase_skipped",
            "task_rebase_conflicted",
            "config_branch_create_failures",
            "config_hash_mismatches",
            "sync_scope_changes",
            "proposals_confirmed",
            "revisions_applied",
        }
        if name not in allowed or isinstance(amount, bool) or amount < 0:
            raise ValueError("unsupported runtime configuration metric")
        with FileLock(self.metrics_lock, self.store.lock_timeout_seconds):
            document = self._read_metrics()
            document["counters"][name] = int(document["counters"].get(name, 0)) + amount
            document["updated_at"] = utc_now()
            atomic_write_json(self.metrics_path, document, mode=0o640)

    def observe_apply_latency(self, seconds: float) -> None:
        bounded = max(0.0, min(float(seconds), 365 * 24 * 3600))
        with FileLock(self.metrics_lock, self.store.lock_timeout_seconds):
            document = self._read_metrics()
            latency = document["proposal_confirm_to_applied_seconds"]
            latency["count"] = int(latency["count"]) + 1
            latency["total"] = float(latency["total"]) + bounded
            latency["max"] = max(float(latency["max"]), bounded)
            document["updated_at"] = utc_now()
            atomic_write_json(self.metrics_path, document, mode=0o640)

    def snapshot(self, saga_root: Path) -> dict[str, Any]:
        document = self._read_metrics()
        waiting = []
        for path in saga_root.glob("*.json"):
            saga = read_json(path)
            if saga.get("state") == "WAITING_SAFE_POINT":
                waiting.append(str(saga.get("confirmed_at") or saga.get("updated_at") or ""))
        return {
            **document,
            "waiting_safe_point": {
                "count": len(waiting),
                "oldest_confirmed_at": min(waiting) if waiting else None,
            },
        }

    def _read_metrics(self) -> dict[str, Any]:
        if self.metrics_path.exists():
            return read_json(self.metrics_path)
        return {
            "schema_version": "1.0",
            "counters": {},
            "proposal_confirm_to_applied_seconds": {
                "count": 0,
                "total": 0.0,
                "max": 0.0,
            },
            "updated_at": utc_now(),
        }
