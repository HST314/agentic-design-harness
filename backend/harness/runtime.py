"""Fail-fast checks for the host primitives required by the process supervisor."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path


def validate_runtime_platform() -> None:
    """Reject hosts where the native process backend cannot enforce isolation."""

    missing: list[str] = []
    if sys.platform.startswith("linux"):
        if os.name != "posix" or not Path("/proc/self/stat").is_file():
            missing.append("Linux process identity")
        if not hasattr(os, "killpg") or not hasattr(os, "setsid"):
            missing.append("Linux process-group control")
    elif sys.platform == "win32":
        if os.name != "nt" or not hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            missing.append("Windows process-group control")
        try:
            ctypes.WinDLL("kernel32", use_last_error=True)
        except (AttributeError, OSError):
            missing.append("Windows Job Object support")
    else:
        missing.append("a supported Linux or Windows kernel")
    if missing:
        detail = ", ".join(missing)
        raise RuntimeError("This release requires: " + detail + ".")
