"""Fail-fast checks for the host primitives required by the process supervisor."""

from __future__ import annotations

import os
import sys
from importlib.util import find_spec
from pathlib import Path


def validate_runtime_platform() -> None:
    """Reject hosts where isolation and stale-PID checks cannot be enforced."""

    missing: list[str] = []
    if os.name != "posix":
        missing.append("POSIX process semantics")
    if not sys.platform.startswith("linux"):
        missing.append("Linux /proc semantics")
    if not Path("/proc/self/stat").is_file():
        missing.append("/proc/self/stat")
    if not hasattr(os, "killpg") or not hasattr(os, "setsid"):
        missing.append("process-group control")
    if find_spec("fcntl") is None:
        missing.append("fcntl locking")
    if missing:
        detail = ", ".join(missing)
        raise RuntimeError(
            "This release requires a Linux/POSIX host with: " + detail + "."
        )
