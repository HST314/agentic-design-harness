"""Pinned credential-pair pool with crash-recoverable creation commits."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ..core.errors import HarnessError
from ..storage.atomic import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_yaml,
    canonical_json_bytes,
    digest_json,
    fsync_directory,
    read_json,
)
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.ndjson import append_record, recover_records
from ..storage.repository import Actor, utc_now
from ..storage.safe_open import open_regular_readonly
from ..storage.store import FileStateStore
from .credential_events import (
    active_assignment_chain,
    creation_assignment_chain,
    event_for_id,
    revocation_for_creation,
    revocation_summary,
)


class CredentialPair(BaseModel):
    """One indivisible Provider credential and its exact environment mapping."""

    model_config = ConfigDict(extra="forbid")

    credential_pair_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    key_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    base_url: str = Field(min_length=8, max_length=2048)
    api_key: str = Field(min_length=1, max_length=8192, repr=False)
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    base_url_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    revision: int = Field(ge=1)
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an HTTP(S) service root without credentials")
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("api_key cannot contain control delimiters")
        return value

    @model_validator(mode="after")
    def validate_environment_pair(self) -> CredentialPair:
        if self.api_key_env == self.base_url_env:
            raise ValueError("credential fields require two distinct environment names")
        if "KEY" not in self.api_key_env or "URL" not in self.base_url_env:
            raise ValueError("credential environment mapping is not explicit")
        return self


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    credential_pair_id: str
    provider: str
    key_id: str
    base_url: str
    revision: int
    api_key_env: str
    base_url_env: str
    api_key: str = field(repr=False)

    def as_environment(self) -> dict[str, str]:
        return {self.api_key_env: self.api_key, self.base_url_env: self.base_url}

    def safe_summary(self) -> dict[str, Any]:
        parsed = urlsplit(self.base_url)
        return {
            "credential_pair_id": self.credential_pair_id,
            "provider": self.provider,
            "key_id": self.key_id,
            "key_tail": self.api_key[-4:].rjust(4, "*"),
            "base_url_hint": f"{parsed.scheme}://{parsed.hostname}/…",
            "revision": self.revision,
        }


class CredentialPoolService:
    """Allocates complete pairs at the authoritative instance creation commit."""

    def __init__(self, store: FileStateStore) -> None:
        self.store = store
        self.secret_path = store.layout.control_root / "secrets" / "key-pool.yaml"
        self.state_path = store.layout.control_root / "config" / "key-pool-state.json"
        self.events_path = store.layout.control_root / "credential-events.ndjson"
        self.integrity_key_path = (
            store.layout.control_root / "secrets" / "pair-integrity.key"
        )
        self.intent_root = store.layout.control_root / "config" / "creation-intents"
        self.lock_path = store.layout.control_root / "locks" / "credential-pool.lock"

    def configure_pool(self, pairs: Sequence[dict[str, Any] | CredentialPair]) -> dict[str, Any]:
        try:
            validated = [
                item if isinstance(item, CredentialPair) else CredentialPair.model_validate(item)
                for item in pairs
            ]
        except ValidationError as exc:
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "A credential pair failed strict validation.",
                {"error_count": exc.error_count()},
            ) from None
        active_ids = [item.credential_pair_id for item in validated]
        if not validated or len(active_ids) != len(set(active_ids)):
            self._invalid("The active credential pool must contain unique pair ids.")
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            document = self._load_secret_document(required=False)
            historical = {
                (item["credential_pair_id"], item["revision"]): item
                for item in document.get("pairs", [])
            }
            for pair in validated:
                raw = pair.model_dump(mode="json")
                key = (pair.credential_pair_id, pair.revision)
                previous = historical.get(key)
                comparable = {key: value for key, value in raw.items() if key != "enabled"}
                prior_comparable = (
                    {key: value for key, value in previous.items() if key != "enabled"}
                    if previous is not None
                    else None
                )
                if prior_comparable is not None and prior_comparable != comparable:
                    self._invalid(
                        "A credential pair revision is immutable; increment revision to edit it."
                    )
                historical[key] = {**raw, "enabled": True}
            secret_document = {
                "schema_version": "1.0",
                "active": [
                    {
                        "credential_pair_id": item.credential_pair_id,
                        "revision": item.revision,
                        "enabled": item.enabled,
                    }
                    for item in validated
                ],
                "pairs": [historical[key] for key in sorted(historical)],
            }
            atomic_write_yaml(self.secret_path, secret_document, mode=0o600)
            os.chmod(self.secret_path, 0o600)
            self._write_state_projection()
            return {"pairs": self.list_redacted(), "count": len(validated)}

    def list_redacted(self) -> list[dict[str, Any]]:
        document = self._load_secret_document(required=False)
        historical = {
            (item["credential_pair_id"], item["revision"]): item
            for item in document.get("pairs", [])
        }
        values: list[dict[str, Any]] = []
        for active in document.get("active", []):
            raw = historical[(active["credential_pair_id"], active["revision"])]
            pair = CredentialPair.model_validate({**raw, "enabled": active["enabled"]})
            values.append(pair_to_resolved(pair).safe_summary() | {"enabled": pair.enabled})
        return values

    def resolve_active_pair(self, pair_id: str, revision: int) -> ResolvedCredential:
        """Resolve one explicitly selected active pair without exposing it to an API response."""

        validate_identifier(pair_id, "credential_pair_id")
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            document = self._load_secret_document(required=False)
            active = next(
                (
                    item
                    for item in document.get("active", [])
                    if item.get("credential_pair_id") == pair_id
                    and item.get("revision") == revision
                ),
                None,
            )
            if active is None or not active.get("enabled"):
                raise HarnessError(
                    "CREDENTIAL_PAIR_UNAVAILABLE",
                    "The selected credential pair is not active and enabled.",
                    {"credential_pair_id": pair_id, "revision": revision},
                )
            return self._resolve_pair(pair_id, revision)

    def create_instance(
        self,
        task_id: str,
        initial_instance: dict[str, Any],
        *,
        provider: str,
        creation_id: str,
        actor: Actor,
        crash_hook=None,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(creation_id, "creation_id")
        if self.store.task.get(task_id, task_id) is None:
            raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
        instance = deepcopy(initial_instance)
        instance_id = validate_identifier(instance.get("instance_id", ""), "instance_id")
        if instance.get("task_id") != task_id:
            self._invalid("The initial instance summary belongs to another task.")
        instance.pop("credential_pair_ref", None)
        instance.pop("credential_pair_revision", None)
        request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "provider": provider,
            "initial_instance": instance,
        }
        request_sha256 = digest_json(request)
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            events = recover_records(self.events_path)
            committed = event_for_id(events, "creation_id", creation_id)
            if committed is not None:
                self._check_event_request(committed, request_sha256, "instance creation")
                chain = creation_assignment_chain(events, creation_id)
                if chain[-1]["event_type"] == "CREDENTIAL_INSTANCE_CREATION_REVOKED":
                    raise HarnessError(
                        "INVALID_STATE_TRANSITION",
                        "The instance creation was revoked by its application workflow.",
                        {"creation_id": creation_id},
                    )
                self._recover_assignment_chain(chain)
                return self._creation_result(committed)
            active_chain = active_assignment_chain(events, task_id, instance_id)
            if active_chain and active_chain[-1]["event_type"] != (
                "CREDENTIAL_INSTANCE_CREATION_REVOKED"
            ):
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The instance already has a committed credential assignment.",
                    {"instance_id": instance_id},
                )
            intent_path = self._intent_path(creation_id)
            if intent_path.exists() and read_json(intent_path)["request_sha256"] != request_sha256:
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The creation id was reused for a different pre-commit request.",
                    {"creation_id": creation_id},
                )
            atomic_write_json(
                intent_path,
                {
                    "creation_id": creation_id,
                    "request_sha256": request_sha256,
                    "task_id": task_id,
                    "instance_id": instance_id,
                    "provider": provider,
                    "state": "PREPARED",
                    "prepared_at": utc_now(),
                },
            )
            if crash_hook:
                crash_hook("after_creation_intent")
            selected, cursor_before = self._select_pair(provider, events)
            assigned_instance = {
                **instance,
                "credential_pair_ref": selected.credential_pair_id,
                "credential_pair_revision": selected.revision,
            }
            self.store.contracts.validate("agent-instance", assigned_instance)
            event = {
                "event_id": f"evt_{uuid.uuid4().hex}",
                "event_type": "CREDENTIAL_PAIR_ASSIGNED",
                "creation_id": creation_id,
                "request_sha256": request_sha256,
                "task_id": task_id,
                "instance_id": instance_id,
                "provider": provider,
                "credential_pair_ref": selected.credential_pair_id,
                "credential_pair_revision": selected.revision,
                "key_id": selected.key_id,
                "pair_identity_sha256": pair_identity_digest(selected),
                "pair_integrity_hmac": self._pair_integrity_hmac(selected),
                "cursor_before": cursor_before,
                "cursor_after": cursor_before + 1,
                "initial_instance": assigned_instance,
                "actor": actor.as_dict(),
                "committed_at": utc_now(),
            }
            append_record(self.events_path, event)
            if crash_hook:
                crash_hook("after_assignment_event")
            self._materialize_assignment(event)
            if crash_hook:
                crash_hook("after_instance_snapshot")
            self._write_state_projection()
            atomic_write_json(intent_path, {**read_json(intent_path), "state": "COMMITTED"})
            return self._creation_result(event)

    def revoke_instance_creation(
        self,
        task_id: str,
        creation_id: str,
        *,
        revocation_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        """Compensate an unplanned instance creation without erasing its audit trail."""

        validate_identifier(task_id, "task_id")
        validate_identifier(creation_id, "creation_id")
        validate_identifier(revocation_id, "revocation_id")
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            events = recover_records(self.events_path)
            assigned = event_for_id(events, "creation_id", creation_id)
            revoked = revocation_for_creation(events, creation_id)
            if revoked is not None:
                if revoked["revocation_id"] != revocation_id or revoked["task_id"] != task_id:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The creation revocation identity conflicts with its committed event.",
                        {"creation_id": creation_id},
                    )
                chain = creation_assignment_chain(events, creation_id)
                self._validate_assignment_chain(chain)
                active_chain = active_assignment_chain(
                    events, revoked["task_id"], revoked["instance_id"]
                )
                if active_chain and active_chain[0]["creation_id"] == creation_id:
                    return self._recover_assignment_chain(chain)
                return revocation_summary(chain[0], revoked)
            intent_path = self._intent_path(creation_id)
            if assigned is None:
                if intent_path.exists():
                    intent = read_json(intent_path)
                    if intent.get("task_id") != task_id:
                        raise HarnessError(
                            "IDEMPOTENCY_CONFLICT",
                            "The creation intent belongs to another task.",
                            {"creation_id": creation_id},
                        )
                    atomic_write_json(
                        intent_path,
                        {
                            **intent,
                            "state": "REVOKED",
                            "revocation_id": revocation_id,
                            "revoked_at": utc_now(),
                        },
                    )
                return {
                    "task_id": task_id,
                    "creation_id": creation_id,
                    "status": "NOT_CREATED",
                }
            if assigned["task_id"] != task_id:
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The committed instance creation belongs to another task.",
                    {"creation_id": creation_id},
                )
            self._require_unplanned_creation(assigned)
            event = {
                "event_id": f"evt_{uuid.uuid4().hex}",
                "event_type": "CREDENTIAL_INSTANCE_CREATION_REVOKED",
                "revocation_id": revocation_id,
                "creation_id": creation_id,
                "assignment_event_id": assigned["event_id"],
                "task_id": task_id,
                "instance_id": assigned["instance_id"],
                "actor": actor.as_dict(),
                "committed_at": utc_now(),
            }
            append_record(self.events_path, event)
            chain = creation_assignment_chain([*events, event], creation_id)
            result = self._recover_assignment_chain(chain)
            self._write_state_projection()
            creation_intent = read_json(intent_path) if intent_path.exists() else {}
            atomic_write_json(
                intent_path,
                {
                    **creation_intent,
                    "creation_id": creation_id,
                    "task_id": task_id,
                    "instance_id": assigned["instance_id"],
                    "state": "REVOKED",
                    "revocation_id": revocation_id,
                    "revoked_at": event["committed_at"],
                },
            )
            return result

    def reassign_instance(
        self,
        task_id: str,
        instance_id: str,
        *,
        credential_pair_id: str,
        credential_pair_revision: int,
        idempotency_key: str,
        actor: Actor,
        crash_hook=None,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        validate_identifier(credential_pair_id, "credential_pair_id")
        if not idempotency_key or len(idempotency_key) > 128:
            self._invalid("The reassignment idempotency key is invalid.")
        request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "credential_pair_id": credential_pair_id,
            "credential_pair_revision": credential_pair_revision,
        }
        request_sha256 = digest_json(request)
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            events = recover_records(self.events_path)
            committed = event_for_id(events, "idempotency_key", idempotency_key)
            if committed is not None:
                self._check_event_request(committed, request_sha256, "credential reassignment")
                chain = active_assignment_chain(events, task_id, instance_id)
                if not chain:
                    raise HarnessError(
                        "INVALID_STATE_TRANSITION",
                        "The credential reassignment belongs to a revoked creation.",
                        {"instance_id": instance_id},
                    )
                self._recover_assignment_chain(chain)
                return self._assignment_summary(committed)
            current = self.store.instance.get(task_id, instance_id)
            if current is None:
                raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
            if current["status"] == "ARCHIVED":
                raise HarnessError(
                    "INVALID_STATE_TRANSITION", "An archived instance is read-only."
                )
            selected = self._resolve_pair(credential_pair_id, credential_pair_revision)
            event = {
                "event_id": f"evt_{uuid.uuid4().hex}",
                "event_type": "CREDENTIAL_PAIR_REASSIGNED",
                "idempotency_key": idempotency_key,
                "request_sha256": request_sha256,
                "task_id": task_id,
                "instance_id": instance_id,
                "provider": selected.provider,
                "credential_pair_ref": selected.credential_pair_id,
                "credential_pair_revision": selected.revision,
                "key_id": selected.key_id,
                "pair_identity_sha256": pair_identity_digest(selected),
                "pair_integrity_hmac": self._pair_integrity_hmac(selected),
                "actor": actor.as_dict(),
                "committed_at": utc_now(),
            }
            append_record(self.events_path, event)
            if crash_hook:
                crash_hook("after_reassignment_event")
            self._materialize_assignment(event)
            self._write_state_projection()
            return self._assignment_summary(event)

    def resolve_for_instance(self, task_id: str, instance_id: str) -> ResolvedCredential:
        assignment_path = self._assignment_path(task_id, instance_id)
        if not assignment_path.exists():
            self.recover()
        if not assignment_path.exists():
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "The instance has no committed credential assignment.",
                {"instance_id": instance_id},
            )
        assignment = read_json(assignment_path)
        resolved = self._resolve_pair(
            assignment["credential_pair_ref"], assignment["credential_pair_revision"]
        )
        instance = self.store.instance.get(task_id, instance_id)
        if (
            instance is None
            or instance.get("credential_pair_ref") != resolved.credential_pair_id
            or instance.get("credential_pair_revision") != resolved.revision
            or assignment["pair_identity_sha256"] != pair_identity_digest(resolved)
            or assignment["pair_integrity_hmac"] != self._pair_integrity_hmac(resolved)
            or assignment["key_id"] != resolved.key_id
        ):
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "The pinned credential identity does not match the instance assignment.",
                {"instance_id": instance_id},
            )
        return resolved

    def recover(self) -> list[dict[str, Any]]:
        events = recover_records(self.events_path)
        recovered: list[dict[str, Any]] = []
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            instance_keys: set[tuple[str, str]] = set()
            for event in events:
                if event.get("event_type") not in {
                    "CREDENTIAL_PAIR_ASSIGNED",
                    "CREDENTIAL_PAIR_REASSIGNED",
                    "CREDENTIAL_INSTANCE_CREATION_REVOKED",
                }:
                    continue
                instance_keys.add((event["task_id"], event["instance_id"]))
            for task_id, instance_id in sorted(instance_keys):
                chain = active_assignment_chain(events, task_id, instance_id)
                if not chain:
                    continue
                recovered.append(self._recover_assignment_chain(chain))
            self._write_state_projection()
        return recovered

    def _recover_assignment_chain(self, chain: list[dict[str, Any]]) -> dict[str, Any]:
        self._validate_assignment_chain(chain)
        initial = chain[0]
        final = chain[-1]
        task_id = final["task_id"]
        instance_id = final["instance_id"]
        if final["event_type"] == "CREDENTIAL_INSTANCE_CREATION_REVOKED":
            self._retire_revoked_creation(initial)
            return revocation_summary(initial, final)
        current = self.store.instance.get(task_id, instance_id)
        if current is None:
            self._materialize_assignment(initial)
            current = self.store.instance.get(task_id, instance_id)
        if current is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        committed_pairs = {
            (event["credential_pair_ref"], event["credential_pair_revision"])
            for event in chain
            if event["event_type"] != "CREDENTIAL_INSTANCE_CREATION_REVOKED"
        }
        current_pair = (
            current.get("credential_pair_ref"),
            current.get("credential_pair_revision"),
        )
        if current_pair not in committed_pairs:
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "An instance snapshot conflicts with its committed credential chain.",
                {"instance_id": instance_id},
            )
        self._materialize_assignment(final)
        return self._assignment_summary(final)

    def _validate_assignment_chain(self, chain: list[dict[str, Any]]) -> None:
        if not chain or chain[0]["event_type"] != "CREDENTIAL_PAIR_ASSIGNED":
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "The committed credential assignment chain is invalid.",
            )
        revocations = [
            event
            for event in chain
            if event["event_type"] == "CREDENTIAL_INSTANCE_CREATION_REVOKED"
        ]
        assignment_events = chain[:-1] if revocations else chain
        if (
            any(event["event_type"] == "CREDENTIAL_PAIR_ASSIGNED" for event in chain[1:])
            or len(revocations) > 1
            or (revocations and chain[-1] != revocations[0])
        ):
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "The committed credential assignment chain is invalid.",
            )
        task_id = chain[0]["task_id"]
        instance_id = chain[0]["instance_id"]
        initial = chain[0]["initial_instance"]
        if (
            initial.get("task_id") != task_id
            or initial.get("instance_id") != instance_id
            or initial.get("credential_pair_ref") != chain[0]["credential_pair_ref"]
            or initial.get("credential_pair_revision")
            != chain[0]["credential_pair_revision"]
        ):
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "The committed credential creation event is internally inconsistent.",
            )
        event_ids: set[str] = set()
        for event in assignment_events:
            if (
                event["task_id"] != task_id
                or event["instance_id"] != instance_id
                or event["event_id"] in event_ids
            ):
                raise HarnessError(
                    "CREDENTIAL_PAIR_INVALID",
                    "The committed credential assignment chain is invalid.",
                )
            event_ids.add(event["event_id"])
            resolved = self._resolve_pair(
                event["credential_pair_ref"], event["credential_pair_revision"]
            )
            if (
                event["provider"] != resolved.provider
                or event["key_id"] != resolved.key_id
                or event["pair_identity_sha256"] != pair_identity_digest(resolved)
                or event["pair_integrity_hmac"] != self._pair_integrity_hmac(resolved)
            ):
                raise HarnessError(
                    "CREDENTIAL_PAIR_INVALID",
                    "A committed credential assignment failed integrity validation.",
                    {"instance_id": instance_id},
                )
        if revocations:
            revoked = revocations[0]
            if (
                revoked["task_id"] != task_id
                or revoked["instance_id"] != instance_id
                or revoked["creation_id"] != chain[0]["creation_id"]
                or revoked["assignment_event_id"] != chain[0]["event_id"]
                or revoked["event_id"] in event_ids
            ):
                raise HarnessError(
                    "CREDENTIAL_PAIR_INVALID",
                    "The committed creation revocation is internally inconsistent.",
                )

    def _require_unplanned_creation(self, assigned: dict[str, Any]) -> None:
        plan = self.store.plan.get(assigned["task_id"], assigned["task_id"])
        if plan is not None and any(
            item["instance_id"] == assigned["instance_id"] for item in plan["instances"]
        ):
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A planned instance creation cannot be revoked.",
                {"instance_id": assigned["instance_id"]},
            )

    def _retire_revoked_creation(self, assigned: dict[str, Any]) -> None:
        self._require_unplanned_creation(assigned)
        task_id = assigned["task_id"]
        instance_id = assigned["instance_id"]
        current = self.store.instance.get(task_id, instance_id)
        if current is not None and (
            current.get("credential_pair_ref") != assigned["credential_pair_ref"]
            or current.get("credential_pair_revision")
            != assigned["credential_pair_revision"]
        ):
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "The revoked creation conflicts with the current instance snapshot.",
                {"instance_id": instance_id},
            )
        instance_path = self.store.instance.path(task_id, instance_id)
        instance_path.unlink(missing_ok=True)
        fsync_directory(instance_path.parent)
        assignment_path = self._assignment_path(task_id, instance_id)
        assignment_path.unlink(missing_ok=True)
        fsync_directory(assignment_path.parent)

    def _materialize_assignment(self, event: dict[str, Any]) -> None:
        task_id = event["task_id"]
        instance_id = event["instance_id"]
        if event["event_type"] == "CREDENTIAL_PAIR_ASSIGNED":
            current = self.store.instance.get(task_id, instance_id)
            if current is None:
                self.store.instance.put(
                    task_id,
                    instance_id,
                    deepcopy(event["initial_instance"]),
                    expected_revision=0,
                    actor=Actor("system", "credential_recovery"),
                    command="materialize_instance_creation",
                    idempotency_key=event["creation_id"],
                )
            elif (
                current.get("credential_pair_ref") != event["credential_pair_ref"]
                or current.get("credential_pair_revision")
                != event["credential_pair_revision"]
            ):
                raise HarnessError(
                    "CREDENTIAL_PAIR_INVALID",
                    "An instance snapshot conflicts with its committed creation assignment.",
                    {"instance_id": instance_id},
                )
        else:
            current = self.store.instance.get(task_id, instance_id)
            if current is None:
                raise HarnessError(
                    "INSTANCE_NOT_FOUND", "The requested instance does not exist."
                )
            matches = (
                current.get("credential_pair_ref") == event["credential_pair_ref"]
                and current.get("credential_pair_revision")
                == event["credential_pair_revision"]
            )
            if current["status"] == "ARCHIVED" and not matches:
                raise HarnessError(
                    "CREDENTIAL_PAIR_INVALID",
                    "An archived instance conflicts with its committed credential pair.",
                    {"instance_id": instance_id},
                )
            if not matches:
                self.store.update_instance_fields(
                    task_id,
                    instance_id,
                    {
                        "credential_pair_ref": event["credential_pair_ref"],
                        "credential_pair_revision": event["credential_pair_revision"],
                    },
                    actor=Actor("system", "credential_recovery"),
                    command="materialize_credential_reassignment",
                    idempotency_key=event["idempotency_key"],
                )
        assignment = {
            "task_id": task_id,
            "instance_id": instance_id,
            "credential_pair_ref": event["credential_pair_ref"],
            "credential_pair_revision": event["credential_pair_revision"],
            "provider": event["provider"],
            "key_id": event["key_id"],
            "pair_identity_sha256": event["pair_identity_sha256"],
            "pair_integrity_hmac": event["pair_integrity_hmac"],
            "assignment_event_id": event["event_id"],
            "assigned_at": event["committed_at"],
        }
        atomic_write_json(self._assignment_path(task_id, instance_id), assignment)

    def _select_pair(
        self, provider: str, events: list[dict[str, Any]]
    ) -> tuple[ResolvedCredential, int]:
        document = self._load_secret_document(required=True)
        history = {
            (item["credential_pair_id"], item["revision"]): item
            for item in document["pairs"]
        }
        enabled: list[CredentialPair] = []
        for active in document["active"]:
            raw = history[(active["credential_pair_id"], active["revision"])]
            pair = CredentialPair.model_validate({**raw, "enabled": active["enabled"]})
            if pair.enabled and pair.provider == provider:
                enabled.append(pair)
        if not enabled:
            raise HarnessError(
                "CREDENTIAL_PAIR_UNAVAILABLE",
                "No enabled complete credential pair is configured for this Provider.",
                {"provider": provider},
            )
        cursor = sum(
            1
            for item in events
            if item.get("event_type") == "CREDENTIAL_PAIR_ASSIGNED"
            and item.get("provider") == provider
        )
        return pair_to_resolved(enabled[cursor % len(enabled)]), cursor

    def _resolve_pair(self, pair_id: str, revision: int) -> ResolvedCredential:
        document = self._load_secret_document(required=True)
        raw = next(
            (
                item
                for item in document["pairs"]
                if item["credential_pair_id"] == pair_id and item["revision"] == revision
            ),
            None,
        )
        if raw is None:
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "The pinned credential pair revision no longer exists.",
                {"credential_pair_id": pair_id, "revision": revision},
            )
        return pair_to_resolved(CredentialPair.model_validate(raw))

    def _load_secret_document(self, *, required: bool) -> dict[str, Any]:
        if not self.secret_path.exists():
            if required:
                raise HarnessError(
                    "CREDENTIAL_PAIR_UNAVAILABLE",
                    "The credential pool has not been configured.",
                )
            return {"schema_version": "1.0", "active": [], "pairs": []}
        if os.name != "nt" and stat_mode(self.secret_path) & 0o077:
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "The credential pool secret file permissions are too broad.",
            )
        try:
            descriptor = open_regular_readonly(
                self.secret_path,
                trusted_root=self.store.layout.control_root,
            )
            with os.fdopen(descriptor, "rb") as stream:
                loaded = yaml.safe_load(stream.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            self._invalid("The credential pool secret document is invalid.")
        if not isinstance(loaded, dict):
            self._invalid("The credential pool secret document is invalid.")
        if "active" not in loaded and "pairs" in loaded:
            loaded = {
                "schema_version": "1.0",
                "active": [
                    {
                        "credential_pair_id": item["credential_pair_id"],
                        "revision": item["revision"],
                        "enabled": item.get("enabled", True),
                    }
                    for item in loaded["pairs"]
                ],
                "pairs": loaded["pairs"],
            }
        return self._validate_secret_document(loaded)

    def _validate_secret_document(self, loaded: dict[str, Any]) -> dict[str, Any]:
        if (
            set(loaded) != {"schema_version", "active", "pairs"}
            or loaded.get("schema_version") != "1.0"
            or not isinstance(loaded.get("active"), list)
            or not isinstance(loaded.get("pairs"), list)
        ):
            self._invalid("The credential pool secret document is invalid.")
        try:
            pairs = [CredentialPair.model_validate(item) for item in loaded["pairs"]]
        except ValidationError:
            self._invalid("A stored credential pair failed strict validation.")
        pair_keys = [(item.credential_pair_id, item.revision) for item in pairs]
        pair_key_set = set(pair_keys)
        if len(pair_keys) != len(pair_key_set):
            self._invalid("The credential pair history contains duplicate revisions.")
        active: list[dict[str, Any]] = []
        for item in loaded["active"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"credential_pair_id", "revision", "enabled"}
                or not isinstance(item.get("credential_pair_id"), str)
                or not isinstance(item.get("revision"), int)
                or isinstance(item.get("revision"), bool)
                or not isinstance(item.get("enabled"), bool)
            ):
                self._invalid("The active credential-pair list is invalid.")
            key = (item["credential_pair_id"], item["revision"])
            if key not in pair_key_set:
                self._invalid("An active credential pair has no immutable revision.")
            active.append(deepcopy(item))
        active_ids = [item["credential_pair_id"] for item in active]
        if len(active_ids) != len(set(active_ids)):
            self._invalid("The active credential-pair list contains duplicate ids.")
        return {
            "schema_version": "1.0",
            "active": active,
            "pairs": [item.model_dump(mode="json") for item in pairs],
        }

    def _write_state_projection(self) -> None:
        events = recover_records(self.events_path)
        cursor_by_provider: dict[str, int] = {}
        assignments: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.get("event_type") == "CREDENTIAL_PAIR_ASSIGNED":
                provider = event["provider"]
                cursor_by_provider[provider] = cursor_by_provider.get(provider, 0) + 1
            if event.get("event_type") in {
                "CREDENTIAL_PAIR_ASSIGNED",
                "CREDENTIAL_PAIR_REASSIGNED",
            }:
                key = f"{event['task_id']}:{event['instance_id']}"
                assignments[key] = self._assignment_summary(event)
            elif event.get("event_type") == "CREDENTIAL_INSTANCE_CREATION_REVOKED":
                key = f"{event['task_id']}:{event['instance_id']}"
                assignments.pop(key, None)
        atomic_write_json(
            self.state_path,
            {
                "schema_version": "1.0",
                "next_cursor_by_provider": cursor_by_provider,
                "assignments": assignments,
                "rebuilt_at": utc_now(),
            },
        )

    def _pair_integrity_hmac(self, pair: ResolvedCredential) -> str:
        key = self._integrity_key()
        payload = {
            "credential_pair_id": pair.credential_pair_id,
            "provider": pair.provider,
            "key_id": pair.key_id,
            "base_url": pair.base_url,
            "revision": pair.revision,
            "api_key_env": pair.api_key_env,
            "base_url_env": pair.base_url_env,
            "api_key": pair.api_key,
        }
        return hmac.new(key, canonical_json_bytes(payload), hashlib.sha256).hexdigest()

    def _integrity_key(self) -> bytes:
        if not self.integrity_key_path.exists():
            atomic_write_bytes(self.integrity_key_path, secrets.token_bytes(32), mode=0o600)
        if os.name != "nt" and stat_mode(self.integrity_key_path) & 0o077:
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "The credential integrity key permissions are too broad.",
            )
        try:
            descriptor = open_regular_readonly(
                self.integrity_key_path,
                trusted_root=self.store.layout.control_root,
            )
            with os.fdopen(descriptor, "rb") as stream:
                return stream.read()
        except OSError:
            self._invalid("The credential integrity key is invalid.")

    def _assignment_path(self, task_id: str, instance_id: str) -> Path:
        path = self.store.layout.control_root / "tasks" / task_id / "credentials"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path / f"{instance_id}.json"

    def _intent_path(self, creation_id: str) -> Path:
        self.intent_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256(creation_id.encode()).hexdigest()
        return self.intent_root / f"{digest}.json"

    @staticmethod
    def _check_event_request(event: dict[str, Any], digest: str, operation: str) -> None:
        if event["request_sha256"] != digest:
            raise HarnessError(
                "IDEMPOTENCY_CONFLICT",
                f"The idempotency identity was reused for another {operation} request.",
            )

    def _creation_result(self, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "creation_id": event["creation_id"],
            "instance": deepcopy(event["initial_instance"]),
            "credential": self._assignment_summary(event),
        }

    @staticmethod
    def _assignment_summary(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": event["task_id"],
            "instance_id": event["instance_id"],
            "credential_pair_id": event["credential_pair_ref"],
            "credential_pair_revision": event["credential_pair_revision"],
            "provider": event["provider"],
            "key_id": event["key_id"],
            "assignment_event_id": event["event_id"],
        }

    @staticmethod
    def _invalid(message: str) -> NoReturn:
        raise HarnessError("CREDENTIAL_PAIR_INVALID", message)


def pair_to_resolved(pair: CredentialPair) -> ResolvedCredential:
    return ResolvedCredential(
        credential_pair_id=pair.credential_pair_id,
        provider=pair.provider,
        key_id=pair.key_id,
        base_url=pair.base_url,
        revision=pair.revision,
        api_key_env=pair.api_key_env,
        base_url_env=pair.base_url_env,
        api_key=pair.api_key,
    )


def pair_identity_digest(pair: ResolvedCredential) -> str:
    return digest_json(
        {
            "credential_pair_id": pair.credential_pair_id,
            "provider": pair.provider,
            "key_id": pair.key_id,
            "base_url": pair.base_url,
            "revision": pair.revision,
            "api_key_env": pair.api_key_env,
            "base_url_env": pair.base_url_env,
        }
    )


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode
