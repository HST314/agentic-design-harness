"""Dependency-free Landlock write allowlist used by managed child processes."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, NoReturn

_CREATE_RULESET = 444
_ADD_RULE = 445
_RESTRICT_SELF = 446
_VERSION = 1
_PATH_BENEATH = 1
_NO_NEW_PRIVS = 38
_WRITE_RIGHTS_V1 = sum(1 << bit for bit in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12))
_REFER = 1 << 13
_TRUNCATE = 1 << 14


class _RulesetAttr(ctypes.Structure):
    _fields_: ClassVar[Any] = [
        ("handled_access_fs", ctypes.c_uint64)
    ]


class _PathBeneathAttr(ctypes.Structure):
    _fields_: ClassVar[Any] = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def landlock_abi() -> int:
    if os.name != "posix" or platform.system() != "Linux":
        return 0
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(_CREATE_RULESET, None, 0, _VERSION)
    return int(result) if result >= 1 else 0


def require_write_sandbox() -> None:
    if landlock_abi() < 3:
        _unavailable("Landlock ABI 3 or newer is required for a fail-closed write boundary.")


def apply_write_sandbox(writable_roots: Sequence[str | Path]) -> None:
    """Deny filesystem mutation everywhere except beneath explicit safe roots."""

    require_write_sandbox()
    handled = _WRITE_RIGHTS_V1 | _REFER | _TRUNCATE
    libc = ctypes.CDLL(None, use_errno=True)
    ruleset_attr = _RulesetAttr(handled)
    ruleset_fd = libc.syscall(
        _CREATE_RULESET, ctypes.byref(ruleset_attr), ctypes.sizeof(ruleset_attr), 0
    )
    if ruleset_fd < 0:
        _system_error("landlock_create_ruleset")
    try:
        for root in writable_roots:
            descriptor = os.open(root, os.O_PATH | os.O_CLOEXEC | os.O_DIRECTORY)
            try:
                rule = _PathBeneathAttr(handled, descriptor)
                if libc.syscall(_ADD_RULE, ruleset_fd, _PATH_BENEATH, ctypes.byref(rule), 0) < 0:
                    _system_error("landlock_add_rule")
            finally:
                os.close(descriptor)
        if libc.prctl(_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            _system_error("prctl(PR_SET_NO_NEW_PRIVS)")
        if libc.syscall(_RESTRICT_SELF, ruleset_fd, 0) < 0:
            _system_error("landlock_restrict_self")
    finally:
        os.close(ruleset_fd)


def _system_error(operation: str) -> NoReturn:
    value = ctypes.get_errno()
    raise OSError(value or errno.EPERM, f"{operation} failed")


def _unavailable(message: str) -> NoReturn:
    raise RuntimeError(message)
