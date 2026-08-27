"""Validated PPT Agent release lock."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

from ..core.errors import HarnessError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?$")


@dataclass(frozen=True, slots=True)
class PptAgentReleaseLock:
    repository: str
    revision: str
    package_version: str
    contract_version: str
    embedded_path: str
    source_content_sha256: str
    dependency_lock_set_sha256: str


def load_ppt_agent_lock(path: Path) -> PptAgentReleaseLock:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _invalid("The PPT Agent release lock is missing or invalid JSON.", path)
    if not isinstance(document, dict):
        _invalid("The PPT Agent release lock must be an object.", path)
    expected = {
        "schema_version",
        "agent",
        "repository",
        "revision",
        "package_version",
        "contract_version",
        "embedded_path",
        "source_content_sha256",
        "dependencies",
    }
    if (
        set(document) != expected
        or document.get("schema_version") != "1.1"
        or document.get("agent") != "ppt-agent"
    ):
        _invalid("The PPT Agent release lock identity is unsupported.", path)
    repository = _string(document.get("repository"), "repository", path)
    parsed = urlsplit(repository)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        _invalid("The PPT Agent repository URL is not an approved HTTPS origin.", path)
    dependencies = document.get("dependencies")
    if not isinstance(dependencies, dict) or set(dependencies) != {"files", "lock_set_sha256"}:
        _invalid("The PPT Agent dependency lock is invalid.", path)
    files = dependencies.get("files")
    if not isinstance(files, list) or not files:
        _invalid("The PPT Agent dependency lock is empty.", path)
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"scope", "path", "sha256"}
            or item.get("scope") != "ppt_agent"
        ):
            _invalid("The PPT Agent dependency record is invalid.", path)
        _relative(item.get("path"), "dependency path", path)
        _matching(item.get("sha256"), _SHA256, "dependency sha256", path)
    return PptAgentReleaseLock(
        repository=repository,
        revision=_matching(document.get("revision"), _REVISION, "revision", path),
        package_version=_matching(
            document.get("package_version"), _VERSION, "package version", path
        ),
        contract_version=_matching(
            document.get("contract_version"), _VERSION, "contract version", path
        ),
        embedded_path=_relative(document.get("embedded_path"), "embedded path", path),
        source_content_sha256=_matching(
            document.get("source_content_sha256"), _SHA256, "source digest", path
        ),
        dependency_lock_set_sha256=_matching(
            dependencies.get("lock_set_sha256"), _SHA256, "dependency lock digest", path
        ),
    )


def _string(value: Any, label: str, path: Path) -> str:
    if not isinstance(value, str) or not value:
        _invalid(f"The PPT Agent {label} is invalid.", path)
    return value


def _matching(value: Any, pattern: re.Pattern[str], label: str, path: Path) -> str:
    text = _string(value, label, path)
    if pattern.fullmatch(text) is None:
        _invalid(f"The PPT Agent {label} is invalid.", path)
    return text


def _relative(value: Any, label: str, path: Path) -> str:
    text = _string(value, label, path)
    candidate = Path(text)
    if (
        candidate.is_absolute()
        or "\\" in text
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        _invalid(f"The PPT Agent {label} must be a normalized relative path.", path)
    return candidate.as_posix()


def _invalid(message: str, path: Path) -> NoReturn:
    raise HarnessError("ADAPTER_UNAVAILABLE", message, {"lock_path": str(path)})
