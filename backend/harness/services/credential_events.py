"""Lifecycle queries for the append-only credential assignment ledger."""

from __future__ import annotations

from typing import Any

from ..core.errors import HarnessError

ASSIGNMENT_EVENT_TYPES = frozenset(
    {
        "CREDENTIAL_PAIR_ASSIGNED",
        "CREDENTIAL_PAIR_REASSIGNED",
        "CREDENTIAL_INSTANCE_CREATION_REVOKED",
    }
)


def event_for_id(
    events: list[dict[str, Any]], field_name: str, value: str
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in events
            if item.get(field_name) == value
            and item.get("event_type")
            in {"CREDENTIAL_PAIR_ASSIGNED", "CREDENTIAL_PAIR_REASSIGNED"}
        ),
        None,
    )


def revocation_for_creation(
    events: list[dict[str, Any]], creation_id: str
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in events
            if item.get("event_type") == "CREDENTIAL_INSTANCE_CREATION_REVOKED"
            and item.get("creation_id") == creation_id
        ),
        None,
    )


def active_assignment_chain(
    events: list[dict[str, Any]], task_id: str, instance_id: str
) -> list[dict[str, Any]]:
    matching = [
        event
        for event in events
        if event.get("event_type") in ASSIGNMENT_EVENT_TYPES
        and event.get("task_id") == task_id
        and event.get("instance_id") == instance_id
    ]
    starts = [
        index
        for index, event in enumerate(matching)
        if event["event_type"] == "CREDENTIAL_PAIR_ASSIGNED"
    ]
    return [] if not starts else matching[starts[-1] :]


def creation_assignment_chain(
    events: list[dict[str, Any]], creation_id: str
) -> list[dict[str, Any]]:
    assigned = event_for_id(events, "creation_id", creation_id)
    if assigned is None:
        raise HarnessError(
            "INSTANCE_NOT_FOUND",
            "The requested instance creation does not exist.",
            {"creation_id": creation_id},
        )
    matching = [
        event
        for event in events
        if event.get("event_type") in ASSIGNMENT_EVENT_TYPES
        and event.get("task_id") == assigned["task_id"]
        and event.get("instance_id") == assigned["instance_id"]
    ]
    start = next(index for index, event in enumerate(matching) if event is assigned)
    end = next(
        (
            index
            for index in range(start + 1, len(matching))
            if matching[index]["event_type"] == "CREDENTIAL_PAIR_ASSIGNED"
        ),
        len(matching),
    )
    return matching[start:end]


def revocation_summary(
    assigned: dict[str, Any], revoked: dict[str, Any]
) -> dict[str, Any]:
    return {
        "task_id": assigned["task_id"],
        "instance_id": assigned["instance_id"],
        "creation_id": assigned["creation_id"],
        "revocation_id": revoked["revocation_id"],
        "status": "REVOKED",
    }
