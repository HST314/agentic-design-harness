"""Bounded local file locks for the single-control-process persistence model."""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

from ..core.errors import HarnessError


class FileLock:
    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._descriptor: int | None = None

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> FileLock:
        if self._descriptor is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._descriptor = descriptor
                    return self
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise HarnessError(
                            "REVISION_CONFLICT",
                            "Timed out waiting for the state writer lock.",
                            {"lock": self.path.name},
                        ) from None
                    time.sleep(0.01)
        except BaseException:
            os.close(descriptor)
            raise

    def release(self) -> None:
        if self._descriptor is None:
            return
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        self._descriptor = None

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()
