"""Descriptor-safe staging for finalized Image Agent delivery bytes."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path

from ..core.errors import HarnessError


def stage_final_delivery(
    project_root: Path,
    relative: Path,
    destination: Path,
    *,
    expected_sha256: str,
) -> None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    root_fd = -1
    delivery_fd = -1
    source_fd = -1
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.stage.tmp")
    temporary_fd = -1
    try:
        root_fd = os.open(project_root, directory_flags)
        delivery_fd = os.open(relative.parts[0], directory_flags, dir_fd=root_fd)
        source_fd = os.open(relative.parts[1], file_flags, dir_fd=delivery_fd)
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise OSError("delivery is not a regular file")
        digest = hashlib.sha256()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(source_fd, "rb") as source, os.fdopen(temporary_fd, "wb") as output:
            source_fd = -1
            temporary_fd = -1
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected_sha256:
            raise HarnessError(
                "ASSET_CORRUPTED", "The finalized Image delivery digest is invalid."
            )
        if destination.exists():
            current_fd = os.open(destination, file_flags)
            if not stat.S_ISREG(os.fstat(current_fd).st_mode):
                os.close(current_fd)
                raise OSError("staged delivery is not a regular file")
            with os.fdopen(current_fd, "rb") as current:
                current_digest = hashlib.sha256(current.read()).hexdigest()
            if current_digest != expected_sha256:
                raise HarnessError(
                    "ASSET_CORRUPTED", "The staged Image delivery changed after collection."
                )
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, destination)
            os.chmod(destination, 0o640)
    except HarnessError:
        raise
    except OSError:
        raise HarnessError(
            "ASSET_VALIDATION_FAILED",
            "The finalized Image delivery could not be staged safely.",
        ) from None
    finally:
        temporary.unlink(missing_ok=True)
        for descriptor in (temporary_fd, source_fd, delivery_fd, root_fd):
            if descriptor >= 0:
                os.close(descriptor)
