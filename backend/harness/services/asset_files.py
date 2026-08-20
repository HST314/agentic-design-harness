"""No-follow file inspection helpers for registered assets."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from typing import BinaryIO


def file_digest(path: Path) -> tuple[int, str]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        return stream_digest(handle)


def detect_mime(path: Path, filename: str) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        return detect_mime_stream(handle, filename)


def stream_digest(handle: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    handle.seek(0)
    while chunk := handle.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    handle.seek(0)
    return size, digest.hexdigest()


def detect_mime_stream(handle: BinaryIO, filename: str) -> str:
    handle.seek(0)
    try:
        header = handle.read(8192)
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return "image/webp"
        if header.startswith(b"%PDF-"):
            return "application/pdf"
        if header.startswith(b"PK\x03\x04"):
            try:
                handle.seek(0)
                with zipfile.ZipFile(handle) as archive:
                    names = set(archive.namelist())
                if "ppt/presentation.xml" in names:
                    return (
                        "application/vnd.openxmlformats-officedocument."
                        "presentationml.presentation"
                    )
                if "word/document.xml" in names:
                    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            except (OSError, zipfile.BadZipFile):
                return "application/octet-stream"
            return "application/octet-stream"
        try:
            text = header.decode("utf-8")
        except UnicodeDecodeError:
            return "application/octet-stream"
        suffix = Path(filename).suffix.lower()
        if suffix == ".json":
            try:
                handle.seek(0)
                json.loads(handle.read().decode("utf-8"))
                return "application/json"
            except (UnicodeDecodeError, json.JSONDecodeError):
                return "application/octet-stream"
        if suffix in {".md", ".markdown"}:
            return "text/markdown"
        if "\x00" not in text:
            return "text/plain"
        return "application/octet-stream"
    finally:
        handle.seek(0)


def kind_for_mime(mime_type: str) -> str:
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.endswith("presentationml.presentation"):
        return "presentation"
    if mime_type in {"application/pdf", "application/json", "text/markdown", "text/plain"}:
        return "document"
    return "other"
