"""Native process identity and process-tree control for Linux and Windows."""

from __future__ import annotations

import ctypes
import hashlib
import os
import signal
import subprocess
import time
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any, ClassVar


def wrapper_spawn_options(source_descriptor: int | None) -> dict[str, Any]:
    """Return mutually valid ``Popen`` options for the current kernel."""

    if os.name == "nt":
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
            "close_fds": True,
        }
    descriptors = () if source_descriptor is None else (source_descriptor,)
    return {
        "start_new_session": True,
        "close_fds": True,
        "pass_fds": descriptors,
    }


def process_start_identity(pid: int) -> str | None:
    """Return a PID-reuse-safe identity for one live process."""

    if pid <= 0:
        return None
    if os.name == "nt":
        creation = _windows_process_creation(pid)
        if creation is None:
            return None
        return hashlib.sha256(f"windows:{pid}:{creation}".encode()).hexdigest()
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split()
        if len(fields) < 20 or fields[0] == "Z":
            return None
        boot_path = Path("/proc/sys/kernel/random/boot_id")
        boot_id = boot_path.read_text(encoding="utf-8").strip()
        return hashlib.sha256(f"{boot_id}:{fields[19]}".encode()).hexdigest()
    except (OSError, ValueError):
        return None


def process_group_contains(group_pid: int, child_pid: int) -> bool:
    """Verify the direct worker/child ownership relationship."""

    if process_start_identity(group_pid) is None or process_start_identity(child_pid) is None:
        return False
    if os.name == "nt":
        return _windows_parent_pid(child_pid) == group_pid
    try:
        return os.getpgid(group_pid) == group_pid and os.getpgid(child_pid) == group_pid
    except ProcessLookupError:
        return False


def process_tree_exists(group_pid: int) -> bool:
    if os.name == "nt":
        return process_start_identity(group_pid) is not None
    try:
        os.killpg(group_pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_process_tree(group_pid: int, grace_seconds: float) -> None:
    """Gracefully stop, then forcibly remove, one isolated process tree."""

    if os.name == "nt":
        if process_start_identity(group_pid) is None:
            return
        ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break is not None:
            with suppress(OSError):
                os.kill(group_pid, ctrl_break)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if process_start_identity(group_pid) is None:
                return
            time.sleep(0.02)
        _windows_terminate(group_pid)
        return
    try:
        os.killpg(group_pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not process_tree_exists(group_pid):
            return
        time.sleep(0.02)
    try:
        os.killpg(group_pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def force_kill_process_tree(group_pid: int) -> None:
    """Crash one isolated tree immediately; intended for recovery tests and gates."""

    if os.name == "nt":
        _windows_terminate(group_pid)
        return
    try:
        os.killpg(group_pid, signal.SIGKILL)
    except ProcessLookupError:
        return


if os.name == "nt":
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259
    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _ProcessEntry32W(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [  # pyright: ignore[reportIncompatibleVariableOverride]
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
    _KERNEL32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _KERNEL32.GetProcessTimes.restype = wintypes.BOOL
    _KERNEL32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _KERNEL32.TerminateProcess.restype = wintypes.BOOL
    _KERNEL32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _KERNEL32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _KERNEL32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    ]
    _KERNEL32.Process32FirstW.restype = wintypes.BOOL
    _KERNEL32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    ]
    _KERNEL32.Process32NextW.restype = wintypes.BOOL


def _windows_process_creation(pid: int) -> int | None:
    if os.name != "nt":
        return None
    handle = _KERNEL32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not _KERNEL32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != _STILL_ACTIVE:
            return None
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not _KERNEL32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        _KERNEL32.CloseHandle(handle)


def _windows_parent_pid(pid: int) -> int | None:
    if os.name != "nt":
        return None
    snapshot = _KERNEL32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        return None
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        found = _KERNEL32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID)
            found = _KERNEL32.Process32NextW(snapshot, ctypes.byref(entry))
        return None
    finally:
        _KERNEL32.CloseHandle(snapshot)


def _windows_terminate(pid: int) -> None:
    if os.name != "nt":
        return
    handle = _KERNEL32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return
    try:
        _KERNEL32.TerminateProcess(handle, 1)
    finally:
        _KERNEL32.CloseHandle(handle)
