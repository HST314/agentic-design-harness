"""Race-safe reads for committed task assets."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from ..core.errors import HarnessError
from .asset_browser import committed_browser_event
from .asset_files import detect_mime_stream, stream_digest

_MANIFEST_LIMIT_BYTES = 1024 * 1024


@dataclass(slots=True)
class OpenedCommittedAsset:
    relative_path: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    stream: BinaryIO

    def close(self) -> None:
        self.stream.close()

    def __enter__(self) -> OpenedCommittedAsset:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def open_committed_asset(
    workspace: Path,
    relative_path: str,
    events: list[dict[str, Any]],
) -> OpenedCommittedAsset:
    """Open beneath a trusted root once, then verify and read that same inode."""

    normalized, event = committed_browser_event(relative_path, events)
    manifest = event["manifest"]
    stream = _open_beneath(workspace, normalized.parts, manifest["asset_id"])
    try:
        filename = normalized.name
        mime_type = detect_mime_stream(stream, filename)
        size_bytes, sha256 = stream_digest(stream)
        if normalized.as_posix() == manifest["relative_path"]:
            if (mime_type, size_bytes, sha256) != (
                manifest["mime_type"],
                manifest["size_bytes"],
                manifest["sha256"],
            ):
                _corrupted(manifest["asset_id"])
        else:
            if size_bytes > _MANIFEST_LIMIT_BYTES:
                _corrupted(manifest["asset_id"])
            try:
                document = json.loads(stream.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                _corrupted(manifest["asset_id"])
            if document != manifest:
                _corrupted(manifest["asset_id"])
            stream.seek(0)
        return OpenedCommittedAsset(
            relative_path=normalized.as_posix(),
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            stream=stream,
        )
    except BaseException:
        stream.close()
        raise


def _open_beneath(root: Path, parts: tuple[str, ...], asset_id: str) -> BinaryIO:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        current_fd = os.open(root, directory_flags)
    except OSError:
        raise HarnessError(
            "PATH_OUTSIDE_TASK_ROOT", "The trusted task root could not be opened."
        ) from None
    try:
        for index, part in enumerate(parts):
            flags = file_flags if index == len(parts) - 1 else directory_flags
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError:
                _corrupted(asset_id)
            os.close(current_fd)
            current_fd = next_fd
        metadata = os.fstat(current_fd)
        if not stat.S_ISREG(metadata.st_mode):
            _corrupted(asset_id)
        stream = os.fdopen(current_fd, "rb")
        current_fd = -1
        return stream
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _corrupted(asset_id: str) -> NoReturn:
    raise HarnessError(
        "ASSET_CORRUPTED",
        "The asset no longer matches its committed manifest.",
        {"asset_id": asset_id},
    )
