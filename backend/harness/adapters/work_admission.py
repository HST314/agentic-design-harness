"""Durable file boundary used to close an Agent's work admission."""

from __future__ import annotations

from pathlib import Path

from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, read_json


def initialize_work_admission(path: Path, instance_id: str) -> None:
    if not path.exists():
        atomic_write_json(
            path,
            {
                "schema_version": "1.0",
                "instance_id": instance_id,
                "quiesced": False,
                "quiesce_operation_id": None,
            },
            mode=0o600,
        )
    _read_validated(path, instance_id)


def write_work_admission(
    path: Path,
    instance_id: str,
    operation_id: str,
    *,
    quiesced: bool,
) -> None:
    admission = _read_validated(path, instance_id)
    admission.update(
        {"quiesced": quiesced, "quiesce_operation_id": operation_id}
    )
    atomic_write_json(path, admission, mode=0o600)


def _read_validated(path: Path, instance_id: str) -> dict[str, object]:
    admission = read_json(path)
    if (
        admission.get("instance_id") != instance_id
        or not isinstance(admission.get("quiesced"), bool)
    ):
        raise HarnessError(
            "PROCESS_START_FAILED", "The Agent work-admission file is invalid."
        )
    return admission
