"""Persistent idempotent command result registry."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..core.errors import HarnessError
from .atomic import atomic_write_json, digest_json, read_json
from .locks import FileLock


class IdempotencyRepository:
    def __init__(self, root: Path, lock_timeout_seconds: float) -> None:
        self.root = root
        self.lock_timeout_seconds = lock_timeout_seconds

    def _path(self, scope: str, key: str) -> Path:
        filename = hashlib.sha256(f"{scope}\0{key}".encode()).hexdigest()
        return self.root / f"{filename}.json"

    @staticmethod
    def request_digest(command: str, payload: dict[str, Any]) -> str:
        return digest_json({"command": command, "payload": payload})

    def lookup(
        self,
        scope: str,
        key: str,
        command: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        path = self._path(scope, key)
        if not path.exists():
            return None
        record = read_json(path)
        expected = self.request_digest(command, payload)
        if record["request_sha256"] != expected:
            raise HarnessError(
                "IDEMPOTENCY_CONFLICT",
                "The idempotency key was already used for a different request.",
                {"scope": scope, "command": command},
            )
        return record["result"]

    def remember(
        self,
        scope: str,
        key: str,
        command: str,
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(scope, key)
        with FileLock(self.root / ".lock", self.lock_timeout_seconds):
            existing = self.lookup(scope, key, command, payload)
            if existing is not None:
                return existing
            atomic_write_json(
                path,
                {
                    "scope": scope,
                    "key": key,
                    "command": command,
                    "request_sha256": self.request_digest(command, payload),
                    "result": result,
                },
            )
        return result
