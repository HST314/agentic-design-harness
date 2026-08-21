"""Validated Token usage ingestion, completeness tracking and aggregation."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..adapters.base import AgentAdapter
from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import utc_now
from ..storage.store import FileStateStore

_TERMINAL_INSTANCE_STATUSES = {
    "SUCCEEDED",
    "FAILED",
    "FAILED_TO_START",
    "CRASHED",
    "CANCELLED",
    "SUPERSEDED",
    "ARCHIVED",
    "UNAVAILABLE",
}
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "total_tokens",
)


class UsageService:
    """Own the trust boundary around append-only provider usage facts."""

    def __init__(self, store: FileStateStore) -> None:
        self.store = store

    def ingest(
        self,
        task_id: str,
        instance_id: str,
        events: list[dict[str, Any]],
        *,
        source: str,
        cursor: str | None = None,
        collection_complete: bool | None = None,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        if source not in {"adapter", "internal"}:
            raise HarnessError("VALIDATION_ERROR", "The usage source is invalid.")
        if len(events) > 10_000:
            raise HarnessError("VALIDATION_ERROR", "A usage batch is too large.")
        instance = self._instance(task_id, instance_id)
        lock_path = self.store.layout.control_root / "locks" / f"usage-{task_id}.lock"
        with FileLock(lock_path, self.store.lock_timeout_seconds):
            existing = {item["event_id"]: item for item in self.store.usage.list(task_id)}
            staged: dict[str, dict[str, Any]] = {}
            duplicates = 0
            for raw in events:
                event = deepcopy(raw)
                self._validate_event(task_id, instance, event)
                previous = staged.get(event["event_id"], existing.get(event["event_id"]))
                if previous is not None:
                    if digest_json(previous) != digest_json(event):
                        raise HarnessError(
                            "IDEMPOTENCY_CONFLICT",
                            "A usage event id was reused with different accounting data.",
                            {"event_id": event["event_id"]},
                        )
                    duplicates += 1
                    continue
                staged[event["event_id"]] = event
            # Validate the complete batch before the first append so a caller
            # error cannot partially commit otherwise valid leading events.
            for event in staged.values():
                if not self.store.usage.append(task_id, event):
                    raise RuntimeError("usage event disappeared during the ingestion lock")
                existing[event["event_id"]] = event
            accepted = len(staged)
            state = self._read_state(task_id)
            prior = state["instances"].get(instance_id, {})
            seen_count = sum(1 for item in existing.values() if item["instance_id"] == instance_id)
            terminal = instance["status"] in _TERMINAL_INSTANCE_STATUSES
            complete = terminal if collection_complete is None else collection_complete
            if seen_count == 0:
                completeness = "NOT_REPORTED"
            elif complete:
                completeness = "COMPLETE"
            else:
                completeness = "PARTIAL"
            next_cursor = cursor
            if next_cursor is None and events:
                next_cursor = str(events[-1]["event_id"])
            state["instances"][instance_id] = {
                "instance_id": instance_id,
                "agent_type": instance["agent_type"],
                "status": completeness,
                "event_count": seen_count,
                "cursor": next_cursor if next_cursor is not None else prior.get("cursor"),
                "last_checked_at": utc_now(),
                "source": source,
            }
            state["updated_at"] = utc_now()
            atomic_write_json(self._state_path(task_id), state)
        return {
            "schema_version": "1.0",
            "task_id": task_id,
            "instance_id": instance_id,
            "accepted": accepted,
            "duplicates": duplicates,
            "completeness": completeness,
            "cursor": state["instances"][instance_id]["cursor"],
        }

    def collect_instance(
        self, task_id: str, instance_id: str, adapter: AgentAdapter
    ) -> dict[str, Any]:
        instance = self._instance(task_id, instance_id)
        if instance["agent_type"] != adapter.agent_type:
            raise HarnessError(
                "VALIDATION_ERROR", "The usage collector does not own this instance."
            )
        state = self._read_state(task_id)
        cursor = state["instances"].get(instance_id, {}).get("cursor")
        events = adapter.collect_usage(instance_id, cursor)
        if not isinstance(events, list):
            raise HarnessError("VALIDATION_ERROR", "The Agent adapter returned invalid usage data.")
        return self.ingest(
            task_id,
            instance_id,
            events,
            source="adapter",
            cursor=str(events[-1]["event_id"]) if events else cursor,
            collection_complete=instance["status"] in _TERMINAL_INSTANCE_STATUSES,
        )

    def summary(self, task_id: str, *, instance_id: str | None = None) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        task = self.store.task.get(task_id, task_id)
        if task is None:
            raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
        if instance_id is not None:
            validate_identifier(instance_id, "instance_id")
            self._instance(task_id, instance_id)
        events = [
            item
            for item in self.store.usage.list(task_id)
            if instance_id is None or item["instance_id"] == instance_id
        ]
        state = self._read_state(task_id)
        plan = self.store.plan.get(task_id, task_id)
        planned_instances = [] if plan is None else plan["instances"]
        if instance_id is not None:
            planned_instances = [
                item for item in planned_instances if item["instance_id"] == instance_id
            ]
        instance_rows = []
        for instance in planned_instances:
            row_events = [item for item in events if item["instance_id"] == instance["instance_id"]]
            collection = state["instances"].get(instance["instance_id"])
            completeness = (
                collection["status"]
                if collection is not None
                else ("PARTIAL" if row_events else "NOT_REPORTED")
            )
            instance_rows.append(
                {
                    "instance_id": instance["instance_id"],
                    "agent_type": instance["agent_type"],
                    "completeness": completeness,
                    "event_count": len(row_events),
                    "tokens": self._token_totals(row_events),
                    "cost": self._cost_summary(row_events),
                    "last_checked_at": (
                        None if collection is None else collection["last_checked_at"]
                    ),
                }
            )
        statuses = [item["completeness"] for item in instance_rows]
        if statuses and all(item == "COMPLETE" for item in statuses):
            completeness = "COMPLETE"
        elif events or any(item != "NOT_REPORTED" for item in statuses):
            completeness = "PARTIAL"
        else:
            completeness = "NOT_REPORTED"
        by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_hour: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            by_model[event["model"]].append(event)
            by_hour[event["occurred_at"][:13] + ":00:00Z"].append(event)
        return {
            "schema_version": "1.0",
            "task_id": task_id,
            "instance_id": instance_id,
            "completeness": completeness,
            "event_count": len(events),
            "tokens": self._token_totals(events),
            "cost": self._cost_summary(events),
            "instances": instance_rows,
            "models": [
                {
                    "model": model,
                    "event_count": len(items),
                    "tokens": self._token_totals(items),
                    "cost": self._cost_summary(items),
                }
                for model, items in sorted(by_model.items())
            ],
            "time_buckets": [
                {
                    "hour": hour,
                    "event_count": len(items),
                    "tokens": self._token_totals(items),
                }
                for hour, items in sorted(by_hour.items())
            ],
            "events": deepcopy(events),
        }

    def recover(self) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        tasks_root = self.store.layout.control_root / "tasks"
        for task_dir in sorted(tasks_root.iterdir() if tasks_root.exists() else []):
            if not task_dir.is_dir():
                continue
            task_id = task_dir.name
            events = self.store.usage.list(task_id)
            if not events and not self._state_path(task_id).exists():
                continue
            state = self._read_state(task_id)
            counts: dict[str, int] = defaultdict(int)
            last_event_ids: dict[str, str] = {}
            for event in events:
                counts[event["instance_id"]] += 1
                last_event_ids[event["instance_id"]] = event["event_id"]
            changed = False
            for instance_id, count in counts.items():
                entry = state["instances"].get(instance_id)
                if (
                    entry is None
                    or entry.get("event_count") != count
                    or entry.get("cursor") is None
                ):
                    instance = self.store.instance.get(task_id, instance_id)
                    if instance is None:
                        raise HarnessError(
                            "VALIDATION_ERROR",
                            "Usage recovery found an event for an unknown instance.",
                            {"instance_id": instance_id},
                        )
                    state["instances"][instance_id] = {
                        "instance_id": instance_id,
                        "agent_type": instance["agent_type"],
                        "status": "PARTIAL",
                        "event_count": count,
                        # Adapter cursors may be opaque pagination tokens. Keep
                        # a durable cursor when present; use the last event id
                        # only to repair a missing collection state.
                        "cursor": (
                            last_event_ids[instance_id]
                            if entry is None or entry.get("cursor") is None
                            else entry["cursor"]
                        ),
                        "last_checked_at": utc_now(),
                        "source": "recovery",
                    }
                    changed = True
            if changed:
                state["updated_at"] = utc_now()
                atomic_write_json(self._state_path(task_id), state)
                recovered.append({"task_id": task_id, "event_count": len(events)})
        return recovered

    def _validate_event(
        self, task_id: str, instance: dict[str, Any], event: dict[str, Any]
    ) -> None:
        try:
            self.store.contracts.validate("token-usage-event", event)
        except HarnessError:
            raise
        if event["task_id"] != task_id or event["instance_id"] != instance["instance_id"]:
            raise HarnessError(
                "VALIDATION_ERROR", "The usage event belongs to another task or instance."
            )
        if event["agent_type"] != instance["agent_type"]:
            raise HarnessError(
                "VALIDATION_ERROR", "The usage event Agent type does not match its instance."
            )
        if event["credential_pair_ref"] != instance.get("credential_pair_ref"):
            raise HarnessError(
                "VALIDATION_ERROR",
                "The usage event credential does not match the pinned instance pair.",
            )
        if event["total_tokens"] != event["input_tokens"] + event["output_tokens"]:
            raise HarnessError(
                "VALIDATION_ERROR", "Usage total_tokens must equal input plus output."
            )
        if event["cached_input_tokens"] > event["input_tokens"]:
            raise HarnessError("VALIDATION_ERROR", "Cached input usage exceeds total input usage.")
        if event["reasoning_tokens"] > event["output_tokens"]:
            raise HarnessError("VALIDATION_ERROR", "Reasoning usage exceeds total output usage.")

    def _instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None or instance.get("task_id") != task_id:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        return instance

    def _state_path(self, task_id: str) -> Path:
        return self.store.layout.control_root / "tasks" / task_id / "usage-state.json"

    def _read_state(self, task_id: str) -> dict[str, Any]:
        path = self._state_path(task_id)
        if not path.exists():
            return {
                "schema_version": "1.0",
                "task_id": task_id,
                "instances": {},
                "updated_at": utc_now(),
            }
        state = read_json(path)
        if (
            not isinstance(state, dict)
            or state.get("task_id") != task_id
            or not isinstance(state.get("instances"), dict)
        ):
            raise HarnessError("VALIDATION_ERROR", "The usage collection state is invalid.")
        return state

    @staticmethod
    def _token_totals(events: list[dict[str, Any]]) -> dict[str, int]:
        return {field: sum(int(event[field]) for event in events) for field in _TOKEN_FIELDS}

    @staticmethod
    def _cost_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
        reported = [event for event in events if "cost_micros" in event]
        revisions = sorted({event["price_catalog_revision"] for event in reported})
        if not events or not reported:
            completeness = "UNKNOWN"
        elif len(reported) == len(events):
            completeness = "COMPLETE"
        else:
            completeness = "PARTIAL"
        return {
            "completeness": completeness,
            "known_micros": sum(int(event["cost_micros"]) for event in reported),
            "priced_event_count": len(reported),
            "unpriced_event_count": len(events) - len(reported),
            "price_catalog_revisions": revisions,
        }
