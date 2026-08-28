"""Single-process durable start-operation worker."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from ..core.errors import HarnessError


class StartOperationRunner:
    """Wake promptly for new work and periodically recover persisted operations."""

    def __init__(
        self,
        run_pending: Callable[[], None],
        *,
        interval_seconds: float = 0.25,
        thread_name: str = "harness-start-operations",
    ):
        self._run_pending = run_pending
        self._interval_seconds = interval_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_name = thread_name
        self._logger = logging.getLogger("harness.start_operations")

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=self._thread_name,
            daemon=True,
        )
        self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_pending()
            except HarnessError as exc:
                self._logger.exception(
                    "start_operation_scan_failed",
                    extra={
                        "fields": {
                            "error_code": exc.code,
                            "error_message": exc.message,
                            "error_details": exc.details,
                        }
                    },
                )
            except Exception as exc:
                self._logger.exception(
                    "start_operation_scan_failed",
                    extra={
                        "fields": {
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                        }
                    },
                )
            self._wake.wait(self._interval_seconds)
            self._wake.clear()
