"""Cross-platform no-link file inspection helpers.

POSIX callers can use descriptor-relative traversal for the strongest race
resistance.  Windows does not expose ``dir_fd`` traversal through Python, so
this module validates every path component, rejects NTFS reparse points, and
checks that the opened handle still describes the inspected file.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def is_link_or_reparse(path: Path, metadata: os.stat_result | None = None) -> bool:
    """Return true for symbolic links and Windows junction/reparse entries."""

    details = metadata or path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(details, "st_file_attributes", 0)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag)


def open_regular_readonly(path: Path, *, trusted_root: Path | None = None) -> int:
    """Open one regular file after rejecting link-like path components.

    The returned descriptor is bound to the same file metadata inspected just
    before ``open``.  This is the portable fallback used on Windows, where
    Python does not support descriptor-relative path traversal.
    """

    candidate = path.absolute()
    root = trusted_root.absolute() if trusted_root is not None else candidate.parent
    _validate_beneath(root, candidate)
    before = candidate.lstat()
    if is_link_or_reparse(candidate, before) or not stat.S_ISREG(before.st_mode):
        raise OSError("path is not a regular no-link file")
    open_flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(candidate, open_flags)
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode):
            raise OSError("file changed while it was opened")
        if os.name == "nt":
            comparison = os.open(candidate, open_flags)
            try:
                if not os.path.sameopenfile(descriptor, comparison):
                    raise OSError("file changed while it was opened")
            finally:
                os.close(comparison)
            if is_link_or_reparse(candidate):
                raise OSError("path became a link or reparse point")
        elif _identity(before) != _identity(after):
            raise OSError("file changed while it was opened")
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        if os.path.commonpath((str(resolved_root), str(resolved_candidate))) != str(
            resolved_root
        ):
            raise OSError("path leaves its trusted root")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_beneath(root: Path, candidate: Path) -> None:
    resolved_root = root.resolve(strict=True)
    if is_link_or_reparse(root):
        raise OSError("trusted root is a link or reparse point")
    resolved_candidate = candidate.resolve(strict=True)
    try:
        inside = os.path.commonpath((str(resolved_root), str(resolved_candidate)))
    except ValueError:
        raise OSError("path leaves its trusted root") from None
    if inside != str(resolved_root):
        raise OSError("path leaves its trusted root")
    # Windows resolution can expand an 8.3 trusted-root alias. Walk upward from
    # the caller's spelling so every actual component is still checked without
    # requiring that spelling to share the resolved root's textual prefix.
    current = candidate
    while True:
        metadata = current.lstat()
        if is_link_or_reparse(current, metadata):
            raise OSError("path contains a link or reparse point")
        if current.resolve(strict=True) == resolved_root:
            break
        parent = current.parent
        if parent == current:
            raise OSError("path leaves its trusted root")
        current = parent


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    # Windows path stat derives execute bits from the filename extension, while
    # fstat has no filename context. Both sides are separately required to be
    # regular files, so mode is not a stable part of the file identity.
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
