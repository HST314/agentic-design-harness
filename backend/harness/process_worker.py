"""Standalone persistent child wrapper that redacts process output.

This file deliberately has no package imports. It survives a Harness restart,
forwards termination to the Agent child and keeps credential-shaped output out
of instance logs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import BinaryIO

_COMMON_SECRET = re.compile(
    rb"(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,})",
    re.IGNORECASE,
)
_MAX_LOG_BYTES = 10 * 1024 * 1024
_MAX_LINE_BYTES = 1024 * 1024
_READ_BYTES = 64 * 1024


def _process_identity(pid: int) -> str | None:
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
    inherited_fds = tuple(spec.get("inherited_fds", ()))
    process = subprocess.Popen(
        spec["command"],
        cwd=spec["cwd"],
        env=spec["environment"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        pass_fds=inherited_fds,
    )
    for descriptor in inherited_fds:
        os.close(descriptor)
    _write_handshake(Path(spec["handshake_path"]), process.pid)

    def forward_signal(signum: int, _: object) -> None:
        if process.poll() is None:
            process.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)
    redactions = [item.encode("utf-8") for item in spec["redactions"]]
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
    return_code = process.wait()
    for thread in threads:
        thread.join()
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
