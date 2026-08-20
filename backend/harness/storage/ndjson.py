"""Checksummed append-only NDJSON with conservative tail repair."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .atomic import canonical_json_bytes, digest_json, fsync_directory


class NdjsonCorruptionError(RuntimeError):
    pass


def _record_with_checksum(record: dict[str, Any]) -> dict[str, Any]:
    clean = {key: value for key, value in record.items() if key != "_record_checksum"}
    return {**clean, "_record_checksum": digest_json(clean)}


def append_record(path: Path, record: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = canonical_json_bytes(_record_with_checksum(record)) + b"\n"
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, mode)
    descriptor_open = True
    try:
        handle = os.fdopen(descriptor, "ab", closefd=True)
        descriptor_open = False
        with handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode)
        fsync_directory(path.parent)
    except BaseException:
        if descriptor_open:
            os.close(descriptor)
        raise


def _decode_verified(raw_line: bytes) -> dict[str, Any]:
    parsed = json.loads(raw_line)
    if not isinstance(parsed, dict):
        raise ValueError("NDJSON record must be an object")
    expected = parsed.pop("_record_checksum", None)
    if not isinstance(expected, str) or expected != digest_json(parsed):
        raise ValueError("NDJSON record checksum mismatch")
    return parsed


def recover_records(
    path: Path,
    warning_sink: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    records: list[dict[str, Any]] = []
    verified_end = 0
    offset = 0
    lines = raw.splitlines(keepends=True)
    for index, line in enumerate(lines):
        is_last = index == len(lines) - 1
        complete = line.endswith(b"\n")
        try:
            if not complete:
                raise ValueError("incomplete NDJSON tail")
            record = _decode_verified(line[:-1])
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            if not is_last:
                raise NdjsonCorruptionError(
                    f"interior NDJSON corruption at byte {offset}: {exc}"
                ) from exc
            with path.open("r+b") as handle:
                handle.truncate(verified_end)
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(path.parent)
            if warning_sink:
                warning_sink(
                    {
                        "type": "NDJSON_TAIL_TRUNCATED",
                        "file": path.name,
                        "truncated_from": len(raw),
                        "truncated_to": verified_end,
                        "reason": str(exc),
                    }
                )
            return records
        records.append(record)
        offset += len(line)
        verified_end = offset
    return records
