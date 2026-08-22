"""Descriptor-safe staging for finalized Image Agent delivery bytes."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, UnidentifiedImageError

from ..core.errors import HarnessError
from ..services.asset_files import (
    detect_mime,
    detect_mime_stream,
    file_digest,
    stream_digest,
)
from ..storage.atomic import fsync_directory, set_permissions
from ..storage.safe_open import open_regular_readonly

_PNG_PARAMETERS = {
    "format": "PNG",
    "color_mode": "RGB",
    "compress_level": 9,
    "optimize": False,
}
_MAX_IMAGE_PIXELS = 40_000_000


def stage_final_delivery(
    project_root: Path,
    relative: PurePosixPath,
    destination: Path,
    *,
    expected_sha256: str,
) -> None:
    if os.name == "nt":
        _stage_final_delivery_portable(
            project_root,
            relative,
            destination,
            expected_sha256=expected_sha256,
        )
        return
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
            raise HarnessError("ASSET_CORRUPTED", "The finalized Image delivery digest is invalid.")
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
            set_permissions(destination, 0o640)
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


def _stage_final_delivery_portable(
    project_root: Path,
    relative: PurePosixPath,
    destination: Path,
    *,
    expected_sha256: str,
) -> None:
    source_path = project_root.joinpath(*relative.parts)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.stage.tmp")
    source_fd = temporary_fd = -1
    try:
        source_fd = open_regular_readonly(source_path, trusted_root=project_root)
        digest = hashlib.sha256()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(source_fd, "rb") as source, os.fdopen(temporary_fd, "wb") as output:
            source_fd = temporary_fd = -1
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected_sha256:
            raise HarnessError("ASSET_CORRUPTED", "The finalized Image delivery digest is invalid.")
        if destination.exists():
            current_fd = open_regular_readonly(destination, trusted_root=destination.parent)
            with os.fdopen(current_fd, "rb") as current:
                current_digest = hashlib.sha256(current.read()).hexdigest()
            if current_digest != expected_sha256:
                raise HarnessError(
                    "ASSET_CORRUPTED", "The staged Image delivery changed after collection."
                )
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, destination)
            set_permissions(destination, 0o640)
    except HarnessError:
        raise
    except OSError:
        raise HarnessError(
            "ASSET_VALIDATION_FAILED",
            "The finalized Image delivery could not be staged safely.",
        ) from None
    finally:
        temporary.unlink(missing_ok=True)
        for descriptor in (temporary_fd, source_fd):
            if descriptor >= 0:
                os.close(descriptor)


def normalize_image_delivery(
    source: Path,
    *,
    accepted_mime_types: tuple[str, ...],
) -> dict[str, Any]:
    """Return contract-compatible bytes, deriving a deterministic PNG when allowed."""

    source_fd = -1
    temporary: Path | None = None
    try:
        source_fd = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise OSError("delivery is not a regular file")
        with os.fdopen(source_fd, "rb") as source_stream:
            source_fd = -1
            source_mime = detect_mime_stream(source_stream, source.name)
            source_size, source_sha256 = stream_digest(source_stream)
            if source_mime in accepted_mime_types:
                return {
                    "path": source,
                    "mime_type": source_mime,
                    "size_bytes": source_size,
                    "sha256": source_sha256,
                    "derivation": None,
                }
            if source_mime != "image/jpeg" or "image/png" not in accepted_mime_types:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "The finalized Image delivery does not match its output MIME contract.",
                    {
                        "actual_mime_type": source_mime,
                        "accepted_mime_types": list(accepted_mime_types),
                    },
                )

            destination = source.with_name(f"{source.stem}.contract.png")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".derive.tmp",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            set_permissions(temporary, 0o600, descriptor=descriptor)
            with os.fdopen(descriptor, "w+b") as output_stream:
                with Image.open(source_stream) as opened:
                    width, height = opened.size
                    if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
                        raise HarnessError(
                            "ASSET_VALIDATION_FAILED",
                            "The finalized Image delivery dimensions are unsafe.",
                            {"width": width, "height": height},
                        )
                    opened.load()
                    verified_source = stream_digest(source_stream)
                    if verified_source != (source_size, source_sha256):
                        raise HarnessError(
                            "ASSET_CORRUPTED",
                            "The finalized JPEG delivery changed during conversion.",
                        )
                    normalized = opened.convert(_PNG_PARAMETERS["color_mode"])
                    normalized.save(
                        output_stream,
                        format=_PNG_PARAMETERS["format"],
                        compress_level=_PNG_PARAMETERS["compress_level"],
                        optimize=_PNG_PARAMETERS["optimize"],
                    )
                output_stream.flush()
                os.fsync(output_stream.fileno())

        assert temporary is not None
        set_permissions(temporary, 0o640)
        derived_size, derived_sha256 = file_digest(temporary)
        if detect_mime(temporary, destination.name) != "image/png":
            raise HarnessError(
                "ASSET_VALIDATION_FAILED",
                "The derived Image delivery is not a valid PNG.",
            )
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            current_size, current_sha256 = file_digest(destination)
            if (current_size, current_sha256) != (derived_size, derived_sha256):
                raise HarnessError(
                    "ASSET_CORRUPTED",
                    "The derived Image delivery changed after conversion.",
                ) from None
        else:
            set_permissions(destination, 0o640)
            fsync_directory(destination.parent)
        return {
            "path": destination,
            "mime_type": "image/png",
            "size_bytes": derived_size,
            "sha256": derived_sha256,
            "derivation": {
                "source_sha256": source_sha256,
                "source_mime_type": source_mime,
                "derived_sha256": derived_sha256,
                "derived_mime_type": "image/png",
                "transform": "jpeg_to_png",
                "parameters": dict(_PNG_PARAMETERS),
            },
        }
    except HarnessError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise HarnessError(
            "ASSET_VALIDATION_FAILED",
            "The finalized JPEG delivery could not be converted safely.",
        ) from None
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
