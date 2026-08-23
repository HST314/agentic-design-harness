"""Standalone persistent child wrapper that redacts process output.

This file deliberately has no package imports. It survives a Harness restart,
forwards termination to the Agent child and keeps credential-shaped output out
of instance logs.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, cast

_COMMON_SECRET = re.compile(
    rb"(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,})",
    re.IGNORECASE,
)
_MAX_LOG_BYTES = 10 * 1024 * 1024
_MAX_LINE_BYTES = 1024 * 1024
_READ_BYTES = 64 * 1024


def _process_identity(pid: int) -> str | None:
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


def _windows_process_creation(pid: int) -> int | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != 259:
            return None
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def _assign_kill_on_close_job(process: subprocess.Popen[bytes]) -> object | None:
    """Put the Windows child tree in a job owned by this persistent wrapper."""

    if os.name != "nt":
        return None

    class IoCounters(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [  # pyright: ignore[reportIncompatibleVariableOverride]
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [  # pyright: ignore[reportIncompatibleVariableOverride]
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [  # pyright: ignore[reportIncompatibleVariableOverride]
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(information), ctypes.sizeof(information)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "SetInformationJobObject failed")
    process_handle = cast(Any, process)._handle
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process_handle))):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "AssignProcessToJobObject failed")
    return job


def _close_windows_handle(handle: object | None) -> None:
    if os.name == "nt" and handle is not None:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


def _write_handshake(path: Path, pid: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "wrapper_pid": os.getpid(),
                "wrapper_start_identity": _process_identity(os.getpid()),
                "child_pid": pid,
                "child_start_identity": _process_identity(pid),
            },
            handle,
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _relay(source: BinaryIO, destination: Path, redactions: list[bytes]) -> None:
    descriptor = os.open(destination, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    output = os.fdopen(descriptor, "ab", buffering=0)

    def clean(value: bytes) -> bytes:
        for secret in redactions:
            if secret:
                value = value.replace(secret, b"[REDACTED]")
        return _COMMON_SECRET.sub(b"[REDACTED]", value)

    def write(value: bytes) -> None:
        nonlocal output
        value = clean(value)
        if output.tell() + len(value) > _MAX_LOG_BYTES:
            output.close()
            rotated = destination.with_suffix(destination.suffix + ".1")
            rotated.unlink(missing_ok=True)
            os.replace(destination, rotated)
            next_descriptor = os.open(
                destination, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600
            )
            output = os.fdopen(next_descriptor, "ab", buffering=0)
        output.write(value)

    pending = b""
    read_chunk = getattr(source, "read1", source.read)
    try:
        while chunk := read_chunk(_READ_BYTES):
            pending += chunk
            while True:
                newline = pending.find(b"\n")
                if newline >= 0:
                    write(pending[: newline + 1])
                    pending = pending[newline + 1 :]
                    continue
                if len(pending) <= _MAX_LINE_BYTES:
                    break
                split_at = _MAX_LINE_BYTES
                # Never split a known credential. The maximum API-key length is
                # bounded by validation, so this scan is small and deterministic.
                for secret in redactions:
                    if not secret:
                        continue
                    start = pending.rfind(secret, 0, split_at + len(secret))
                    if 0 <= start < split_at < start + len(secret):
                        split_at = start
                if split_at == 0:
                    split_at = _MAX_LINE_BYTES
                write(pending[:split_at])
                pending = pending[split_at:]
        if pending:
            write(pending)
    finally:
        if not output.closed:
            output.close()


def main() -> int:
    if len(sys.argv) != 2:
        return 64
    spec_path = Path(sys.argv[1])
    try:
        descriptor = os.open(spec_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            spec = json.load(handle)
    finally:
        spec_path.unlink(missing_ok=True)
    environment = dict(spec["environment"])
    redactions: list[bytes] = []
    for name in spec.get("secret_environment_names", ()):
        value = os.environ.pop(name, None)
        if value is None:
            raise RuntimeError(f"required child secret environment variable is missing: {name}")
        environment[name] = value
        redactions.append(value.encode("utf-8"))
    inherited_fds = tuple(spec.get("inherited_fds", ())) if os.name != "nt" else ()
    if os.name == "nt":
        process = subprocess.Popen(
            spec["command"],
            cwd=spec["cwd"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    else:
        process = subprocess.Popen(
            spec["command"],
            cwd=spec["cwd"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=inherited_fds,
        )
    try:
        windows_job = _assign_kill_on_close_job(process)
    except BaseException:
        process.terminate()
        process.wait()
        raise
    for descriptor in inherited_fds:
        os.close(descriptor)
    _write_handshake(Path(spec["handshake_path"]), process.pid)

    def forward_signal(signum: int, _: object) -> None:
        if process.poll() is None:
            if os.name == "nt":
                process.terminate()
            else:
                process.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)
    break_signal = getattr(signal, "SIGBREAK", None)
    if break_signal is not None:
        signal.signal(break_signal, forward_signal)
    assert process.stdout is not None
    assert process.stderr is not None
    threads = [
        threading.Thread(
            target=_relay,
            args=(process.stdout, Path(spec["stdout_path"]), redactions),
            daemon=False,
        ),
        threading.Thread(
            target=_relay,
            args=(process.stderr, Path(spec["stderr_path"]), redactions),
            daemon=False,
        ),
    ]
    for thread in threads:
        thread.start()
    try:
        return_code = process.wait()
        for thread in threads:
            thread.join()
        return return_code
    finally:
        _close_windows_handle(windows_job)


if __name__ == "__main__":
    raise SystemExit(main())
