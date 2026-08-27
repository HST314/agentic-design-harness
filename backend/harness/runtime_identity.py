"""Shared, dependency-free identity primitives for the Image runtime."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_IGNORED_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".requirements-installed",
        ".requirements-installed.json",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
        "playwright-report",
        "test-results",
    }
)
_IGNORED_SUFFIXES = (".egg-info", ".pyc", ".pyo")
_DEPENDENCY_IGNORED_ROOT_NAMES = frozenset({"bin", "Scripts"})
_DEPENDENCY_IGNORED_NAMES = frozenset({"RECORD"})
_INTERPRETER_PROBE = """
import json
import platform
import sys

print(json.dumps({
    "implementation": sys.implementation.name,
    "cache_tag": sys.implementation.cache_tag,
    "version": platform.python_version(),
    "executable": sys.executable,
    "is_virtual_environment": sys.prefix != sys.base_prefix,
}, sort_keys=True))
"""
_RUNTIME_PACKAGE_PROBE = r"""
import importlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path

source_root = Path(sys.argv[1]).resolve()
dependency_root = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(source_root))
sys.path.insert(0, str(dependency_root))
expected_roots = {
    "fastapi": dependency_root,
    "httpx": dependency_root,
    "openai": dependency_root,
    "PIL": dependency_root,
    "portalocker": dependency_root,
    "pydantic": dependency_root,
    "uvicorn": dependency_root,
    "yaml": dependency_root,
    "main_front": source_root,
}
imports = {}
for module_name, expected_root in expected_roots.items():
    try:
        module = importlib.import_module(module_name)
        origin = Path(module.__file__).resolve()
    except Exception as exc:
        print(json.dumps({
            "error": "import_failed",
            "module": module_name,
            "error_type": type(exc).__name__,
        }, sort_keys=True))
        raise SystemExit(2)
    try:
        origin.relative_to(expected_root)
    except ValueError:
        print(json.dumps({
            "error": "import_outside_isolated_runtime",
            "module": module_name,
            "origin": str(origin),
            "expected_root": str(expected_root),
        }, sort_keys=True))
        raise SystemExit(2)
    imports[module_name] = str(origin)

def normalized_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()

distributions = {}
for distribution in importlib.metadata.distributions(path=[str(dependency_root)]):
    name = distribution.metadata.get("Name")
    if name:
        distributions[normalized_name(name)] = distribution.version
print(json.dumps({
    "python_executable": sys.executable,
    "imports": imports,
    "distributions": distributions,
}, sort_keys=True))
"""
_PPT_RUNTIME_PACKAGE_PROBE = r"""
import importlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path

source_root = Path(sys.argv[1]).resolve()
dependency_root = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(source_root))
sys.path.insert(0, str(dependency_root))
expected_roots = {
    "fastapi": dependency_root,
    "html5lib": dependency_root,
    "openai": dependency_root,
    "pydantic": dependency_root,
    "tinycss2": dependency_root,
    "uvicorn": dependency_root,
    "yaml": dependency_root,
    "main_front": source_root,
}
imports = {}
for module_name, expected_root in expected_roots.items():
    try:
        module = importlib.import_module(module_name)
        origin = Path(module.__file__).resolve()
        origin.relative_to(expected_root)
    except Exception as exc:
        print(json.dumps({
            "error": "import_outside_isolated_runtime",
            "module": module_name,
            "error_type": type(exc).__name__,
        }, sort_keys=True))
        raise SystemExit(2)
    imports[module_name] = str(origin)

def normalized_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()

distributions = {}
for distribution in importlib.metadata.distributions(path=[str(dependency_root)]):
    name = distribution.metadata.get("Name")
    if name:
        distributions[normalized_name(name)] = distribution.version
print(json.dumps({
    "python_executable": sys.executable,
    "imports": imports,
    "distributions": distributions,
}, sort_keys=True))
"""


class RuntimeIdentityError(RuntimeError):
    """A runtime tree or interpreter identity could not be inspected safely."""


@dataclass(frozen=True, slots=True)
class PythonInterpreterIdentity:
    implementation: str
    cache_tag: str
    version: str
    executable: str
    is_virtual_environment: bool

    def as_dict(self) -> dict[str, str | bool]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RuntimePackageIdentity:
    python_executable: str
    imports: dict[str, str]
    distributions: dict[str, str]

    def as_dict(self) -> dict[str, str | dict[str, str]]:
        return asdict(self)


def inspect_python_interpreter(
    command: Sequence[str | Path],
) -> PythonInterpreterIdentity:
    """Read identity from the interpreter that will actually execute a workload."""

    normalized = [str(part) for part in command]
    if not normalized:
        raise RuntimeIdentityError("Python interpreter command is empty.")
    try:
        completed = subprocess.run(
            [*normalized, "-I", "-c", _INTERPRETER_PROBE],
            check=True,
            capture_output=True,
            text=True,
        )
        document: Any = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RuntimeIdentityError(
            "Python interpreter identity could not be inspected."
        ) from exc
    if not isinstance(document, dict) or set(document) != {
        "implementation",
        "cache_tag",
        "version",
        "executable",
        "is_virtual_environment",
    }:
        raise RuntimeIdentityError("Python interpreter returned an invalid identity.")
    values = (
        document["implementation"],
        document["cache_tag"],
        document["version"],
        document["executable"],
    )
    if not all(isinstance(value, str) and value for value in values) or not isinstance(
        document["is_virtual_environment"], bool
    ):
        raise RuntimeIdentityError("Python interpreter returned an invalid identity.")
    return PythonInterpreterIdentity(
        implementation=document["implementation"],
        cache_tag=document["cache_tag"],
        version=document["version"],
        executable=document["executable"],
        is_virtual_environment=document["is_virtual_environment"],
    )


def inspect_runtime_packages(
    interpreter: Path,
    *,
    source_root: Path,
    dependency_root: Path,
) -> RuntimePackageIdentity:
    """Import the Image runtime with its real interpreter and verify import roots."""

    try:
        completed = subprocess.run(
            [
                str(interpreter),
                "-I",
                "-c",
                _RUNTIME_PACKAGE_PROBE,
                str(source_root),
                str(dependency_root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeIdentityError(
            "The Image Agent interpreter could not run the import probe."
        ) from exc
    try:
        document: Any = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeIdentityError(
            "The Image Agent import probe returned no usable diagnostics."
        ) from exc
    if completed.returncode != 0:
        module = document.get("module") if isinstance(document, dict) else None
        reason = document.get("error") if isinstance(document, dict) else None
        suffix = f" ({module}: {reason})" if module and reason else ""
        raise RuntimeIdentityError(
            "The Image Agent packages are not importable from the isolated dependency "
            f"directory{suffix}. Run scripts/dev.py setup --force."
        )
    if not isinstance(document, dict) or set(document) != {
        "python_executable",
        "imports",
        "distributions",
    }:
        raise RuntimeIdentityError("The Image Agent import probe returned invalid data.")
    python_executable = document["python_executable"]
    imports = document["imports"]
    distributions = document["distributions"]
    if (
        not isinstance(python_executable, str)
        or not python_executable
        or not isinstance(imports, dict)
        or not isinstance(distributions, dict)
        or not all(
            isinstance(name, str)
            and isinstance(value, str)
            and name
            and value
            for collection in (imports, distributions)
            for name, value in collection.items()
        )
    ):
        raise RuntimeIdentityError("The Image Agent import probe returned invalid data.")
    return RuntimePackageIdentity(
        python_executable=python_executable,
        imports=imports,
        distributions=distributions,
    )


def inspect_ppt_runtime_packages(
    interpreter: Path,
    *,
    source_root: Path,
    dependency_root: Path,
) -> RuntimePackageIdentity:
    """Verify PPT imports and distribution metadata against isolated roots."""

    try:
        completed = subprocess.run(
            [
                str(interpreter),
                "-I",
                "-c",
                _PPT_RUNTIME_PACKAGE_PROBE,
                str(source_root),
                str(dependency_root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeIdentityError(
            "The PPT Agent interpreter could not run the import probe."
        ) from exc
    try:
        document: Any = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeIdentityError(
            "The PPT Agent import probe returned no usable diagnostics."
        ) from exc
    if completed.returncode != 0:
        module = document.get("module") if isinstance(document, dict) else None
        reason = document.get("error") if isinstance(document, dict) else None
        suffix = f" ({module}: {reason})" if module and reason else ""
        raise RuntimeIdentityError(
            "The PPT Agent packages are not importable from the isolated dependency "
            f"directory{suffix}."
        )
    if not isinstance(document, dict) or set(document) != {
        "python_executable",
        "imports",
        "distributions",
    }:
        raise RuntimeIdentityError("The PPT Agent import probe returned invalid data.")
    python_executable = document["python_executable"]
    imports = document["imports"]
    distributions = document["distributions"]
    if (
        not isinstance(python_executable, str)
        or not python_executable
        or not isinstance(imports, dict)
        or not isinstance(distributions, dict)
        or not all(
            isinstance(name, str) and isinstance(value, str) and name and value
            for collection in (imports, distributions)
            for name, value in collection.items()
        )
    ):
        raise RuntimeIdentityError("The PPT Agent import probe returned invalid data.")
    return RuntimePackageIdentity(python_executable, imports, distributions)


def runtime_platform_identity() -> str:
    """Return a diagnostic platform identity without restricting supported hosts."""

    system = platform.system().lower() or "unknown"
    machine = platform.machine().lower() or "unknown"
    return f"{system}-{machine}"


def content_tree_sha256(
    root: Path,
    *,
    ignored_names: Collection[str] = (),
    ignored_root_names: Collection[str] = (),
    normalize_text_eol: bool = True,
) -> str:
    """Hash runtime-relevant files by portable relative path and content."""

    manifest: list[dict[str, str | int]] = []
    _append_content_manifest(
        root,
        Path(),
        manifest,
        frozenset(ignored_names),
        frozenset(ignored_root_names),
        normalize_text_eol,
    )
    encoded = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def dependency_tree_sha256(root: Path) -> str:
    """Hash importable dependencies, excluding platform install metadata."""

    return content_tree_sha256(
        root,
        ignored_names=_DEPENDENCY_IGNORED_NAMES,
        ignored_root_names=_DEPENDENCY_IGNORED_ROOT_NAMES,
        normalize_text_eol=False,
    )


def _portable_file_bytes(path: Path) -> bytes:
    content = path.read_bytes()
    if b"\0" in content:
        return content
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    return content.replace(b"\r\n", b"\n")


def _ignored(name: str, relative: Path) -> bool:
    return (
        name in _IGNORED_NAMES
        or name.endswith(_IGNORED_SUFFIXES)
        or (relative == Path("frontend") and name == "data")
    )


def _append_content_manifest(
    root: Path,
    relative: Path,
    manifest: list[dict[str, str | int]],
    ignored_names: frozenset[str],
    ignored_root_names: frozenset[str],
    normalize_text_eol: bool,
) -> None:
    current = root / relative
    try:
        entries = sorted(os.scandir(current), key=lambda item: item.name)
    except OSError as exc:
        raise RuntimeIdentityError("Runtime content tree cannot be inspected.") from exc
    for entry in entries:
        if (
            entry.name in ignored_names
            or (not relative.parts and entry.name in ignored_root_names)
            or _ignored(entry.name, relative)
        ):
            continue
        path = Path(entry.path)
        item_relative = relative / entry.name
        if entry.is_symlink():
            raise RuntimeIdentityError(
                f"Runtime content tree contains a symbolic link: {item_relative}"
            )
        if entry.is_dir(follow_symlinks=False):
            _append_content_manifest(
                root,
                item_relative,
                manifest,
                ignored_names,
                ignored_root_names,
                normalize_text_eol,
            )
            continue
        try:
            item_stat = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(item_stat.st_mode):
                raise RuntimeIdentityError(
                    f"Runtime content tree contains a special file: {item_relative}"
                )
            content = (
                _portable_file_bytes(path) if normalize_text_eol else path.read_bytes()
            )
        except OSError as exc:
            raise RuntimeIdentityError(
                f"Runtime content file cannot be inspected: {item_relative}"
            ) from exc
        manifest.append(
            {
                "path": item_relative.as_posix(),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
