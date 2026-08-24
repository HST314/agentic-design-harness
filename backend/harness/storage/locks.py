"""Bounded local file locks for the single-control-process persistence model."""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path

from ..core.errors import HarnessError

if os.name == "nt":
    import msvcrt
else:
    import fcntl


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
        if os.name == "nt" and os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                try:
                    self._lock(descriptor)
                    self._descriptor = descriptor
                    return self
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise HarnessError(
                            "REVISION_CONFLICT",
                            f'Timed out waiting for lock "{self.path.name}".',
                            {
                                "lock": self.path.name,
                                "waited_seconds": self.timeout_seconds,
                            },
                        ) from None
                    time.sleep(0.01)
        except BaseException:
            os.close(descriptor)
            raise

    def release(self) -> None:
        if self._descriptor is None:
            return
        descriptor = self._descriptor
        try:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            self._descriptor = None

    @staticmethod
    def _lock(descriptor: int) -> None:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()
