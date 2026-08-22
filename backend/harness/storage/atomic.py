"""Atomic file replacement primitives used by all snapshots and indexes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows cannot open a directory through ``os.open``.  The file data
        # itself is flushed before ``os.replace``; NTFS then provides atomic
        # name replacement, which is the strongest stdlib guarantee available.
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def set_permissions(path: Path, mode: int, *, descriptor: int | None = None) -> None:
    """Apply the platform's available private/read-only file protection."""

    if descriptor is not None and hasattr(os, "fchmod"):
        os.fchmod(descriptor, mode)
        return
    os.chmod(path, mode)


def atomic_write_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Commit bytes in the destination directory without exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    descriptor_open = True
    try:
        set_permissions(temporary_path, mode, descriptor=descriptor)
        handle = os.fdopen(descriptor, "wb", closefd=True)
        descriptor_open = False
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        set_permissions(path, mode)
        fsync_directory(path.parent)
    except BaseException:
        if descriptor_open:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n", mode)


def atomic_write_yaml(path: Path, value: Any, mode: int = 0o600) -> None:
    content = yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    ).encode("utf-8")
    atomic_write_bytes(path, content, mode)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
