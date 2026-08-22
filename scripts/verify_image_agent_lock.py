#!/usr/bin/env python3
"""Verify the P0 integration baseline and Image Agent release lock."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "agents" / "image-agent.lock.json"
DEFAULT_BASELINE = ROOT / "config" / "baselines" / "p0-integration.json"
IGNORED_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".requirements-installed",
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
IGNORED_SUFFIXES = (".egg-info", ".pyc", ".pyo")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def portable_file_bytes(path: Path) -> bytes:
    """Return bytes with only Git's cross-platform text EOL transform normalized."""

    content = path.read_bytes()
    if b"\0" in content:
        return content
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    return content.replace(b"\r\n", b"\n")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(portable_file_bytes(path)).hexdigest()


def canonical_digest(value: Any) -> str:
    content = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def validate_lock_shape(lock: dict[str, Any]) -> None:
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
    if set(lock) != expected:
        raise ValueError("Image Agent release lock fields are not canonical")
    if lock["schema_version"] != "1.0" or lock["agent"] != "image_agent_mvp":
        raise ValueError("Image Agent release lock identity is unsupported")
    if not isinstance(lock["revision"], str) or REVISION_PATTERN.fullmatch(
        lock["revision"]
    ) is None:
        raise ValueError("Image Agent lock revision is not a full commit hash")
    if not isinstance(lock["source_content_sha256"], str) or SHA256_PATTERN.fullmatch(
        lock["source_content_sha256"]
    ) is None:
        raise ValueError("Image Agent source digest is invalid")
    dependencies = lock["dependencies"]
    if not isinstance(dependencies, dict) or set(dependencies) != {
        "files",
        "lock_set_sha256",
        "runtime_tree_sha256",
    }:
        raise ValueError("Image Agent dependency lock fields are not canonical")
    for field in ("lock_set_sha256", "runtime_tree_sha256"):
        value = dependencies[field]
        if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError(f"Image Agent {field} is invalid")
    files = dependencies["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("Image Agent dependency lock is empty")
    for item in files:
        if not isinstance(item, dict) or set(item) != {"scope", "path", "sha256"}:
            raise ValueError("Image Agent dependency file record is invalid")
        if item["scope"] not in {"harness", "image_agent"}:
            raise ValueError("Image Agent dependency file scope is invalid")
        if not isinstance(item["path"], str) or Path(item["path"]).is_absolute():
            raise ValueError("Image Agent dependency file path is invalid")
        if not isinstance(item["sha256"], str) or SHA256_PATTERN.fullmatch(
            item["sha256"]
        ) is None:
            raise ValueError("Image Agent dependency file digest is invalid")


def ignored(name: str, parent: Path) -> bool:
    return (
        name in IGNORED_NAMES
        or name.endswith(IGNORED_SUFFIXES)
        or (parent == Path("frontend") and name == "data")
    )


def content_tree_sha256(root: Path) -> str:
    manifest: list[dict[str, str | int]] = []

    def append(relative_root: Path) -> None:
        current_path = root / relative_root
        for entry in sorted(os.scandir(current_path), key=lambda item: item.name):
            if ignored(entry.name, relative_root):
                continue
            relative = relative_root / entry.name
            if entry.is_symlink():
                raise ValueError(f"content tree contains an unsafe entry: {relative}")
            if entry.is_dir(follow_symlinks=False):
                append(relative)
                continue
            item_stat = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(item_stat.st_mode):
                raise ValueError(f"content tree contains an unsafe entry: {relative}")
            content = portable_file_bytes(Path(entry.path))
            manifest.append(
                {
                    "path": relative.as_posix(),
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )

    append(Path())
    return canonical_digest(manifest)


def git_output(root: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=not binary,
    )
    return result.stdout if binary else result.stdout.strip()


def git_content_tree_sha256(root: Path, revision: str) -> str:
    raw = git_output(root, "ls-tree", "-r", "-z", revision, binary=True)
    assert isinstance(raw, bytes)
    files: dict[tuple[str, ...], bytes] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ")
        relative = Path(raw_path.decode("utf-8"))
        if any(
            ignored(part, Path(*relative.parts[:index]))
            for index, part in enumerate(relative.parts)
        ):
            continue
        if object_type != "blob" or mode == "120000":
            raise ValueError(f"baseline contains an unsafe entry: {relative}")
        content = git_output(root, "cat-file", "blob", object_id, binary=True)
        assert isinstance(content, bytes)
        files[relative.parts] = content

    manifest: list[dict[str, str | int]] = []

    def append(prefix: tuple[str, ...]) -> None:
        children = sorted(
            {path[len(prefix)] for path in files if path[: len(prefix)] == prefix}
        )
        for child in children:
            candidate = (*prefix, child)
            content = files.get(candidate)
            if content is None:
                append(candidate)
                continue
            manifest.append(
                {
                    "path": Path(*candidate).as_posix(),
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )

    append(())
    return canonical_digest(manifest)


def dependency_lock_set_sha256(
    lock: dict[str, Any], image_agent_root: Path
) -> str:
    digest = hashlib.sha256()
    identities: list[tuple[str, str]] = []
    for item in lock["dependencies"]["files"]:
        scope = item["scope"]
        relative = item["path"]
        identity = (scope, relative)
        identities.append(identity)
        base = ROOT if scope == "harness" else image_agent_root
        path = base / relative
        content = portable_file_bytes(path)
        actual = hashlib.sha256(content).hexdigest()
        if actual != item["sha256"]:
            raise ValueError(f"dependency lock digest mismatch: {scope}:{relative}")
        digest.update(f"{scope}:{relative}".encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    if identities != sorted(identities) or len(identities) != len(set(identities)):
        raise ValueError("dependency lock files are not unique and canonical")
    return digest.hexdigest()


def verify_harness_baseline(baseline: dict[str, Any]) -> None:
    harness = baseline["repositories"]["harness"]
    revision = harness["revision"]
    actual_tree = git_output(ROOT, "rev-parse", f"{revision}^{{tree}}")
    if actual_tree != harness["git_tree"]:
        raise ValueError("Harness baseline Git tree does not match its record")
    if git_content_tree_sha256(ROOT, revision) != harness["source_content_sha256"]:
        raise ValueError("Harness baseline source digest does not match its record")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", revision, "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    digest = hashlib.sha256()
    paths: list[str] = []
    for item in harness["dependency_files"]:
        relative = item["path"]
        paths.append(relative)
        path = ROOT / relative
        if sha256_file(path) != item["sha256"]:
            raise ValueError(f"Harness dependency baseline drifted: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    if paths != sorted(paths) or digest.hexdigest() != harness["dependency_lock_set_sha256"]:
        raise ValueError("Harness dependency lock set is not canonical")
    for evidence in harness["gate_result"]["evidence"]:
        if not (ROOT / evidence).is_file():
            raise ValueError(f"Harness baseline gate evidence is missing: {evidence}")


def verify_cross_references(lock: dict[str, Any], baseline: dict[str, Any]) -> None:
    if baseline.get("schema_version") != "1.0":
        raise ValueError("P0 baseline version is unsupported")
    image = baseline["repositories"]["image_agent"]
    expected = {
        "revision": lock["revision"],
        "source_content_sha256": lock["source_content_sha256"],
        "dependency_lock_set_sha256": lock["dependencies"]["lock_set_sha256"],
        "runtime_dependency_tree_sha256": lock["dependencies"]["runtime_tree_sha256"],
    }
    for field, value in expected.items():
        if image.get(field) != value:
            raise ValueError(f"Image Agent baseline and release lock differ at {field}")
    harness_revision = baseline["repositories"]["harness"]["revision"]
    if image["gate_result"].get("accepted_by_harness_revision") != harness_revision:
        raise ValueError("Image Agent acceptance evidence names another Harness revision")
    for repository in baseline["repositories"].values():
        gate = repository.get("gate_result", {})
        if gate.get("status") != "PASSED":
            raise ValueError("P0 baseline contains a gate that did not pass")
        for evidence in gate.get("evidence", []):
            if not (ROOT / evidence).is_file():
                raise ValueError(f"P0 baseline gate evidence is missing: {evidence}")


def verify_submodule_reference(lock: dict[str, Any]) -> None:
    embedded_path = lock["embedded_path"]
    stage = str(git_output(ROOT, "ls-files", "--stage", "--", embedded_path))
    fields = stage.split(maxsplit=3)
    if len(fields) != 4 or fields[0] != "160000" or fields[2] != "0":
        raise ValueError("Image Agent embedded path is not a canonical Git submodule")
    if fields[1] != lock["revision"] or fields[3] != embedded_path:
        raise ValueError("Image Agent submodule pointer differs from the release lock")

    modules_path = ROOT / ".gitmodules"
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with modules_path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error) as exc:
        raise ValueError("cannot read canonical .gitmodules") from exc
    section = f'submodule "{embedded_path}"'
    if not parser.has_section(section):
        raise ValueError("Image Agent submodule declaration is missing")
    if set(parser[section]) != {"path", "url"}:
        raise ValueError("Image Agent submodule declaration is not canonical")
    if (
        parser[section]["path"] != embedded_path
        or parser[section]["url"] != lock["repository"]
    ):
        raise ValueError("Image Agent submodule origin differs from the release lock")


def verify_image_source(lock: dict[str, Any], baseline: dict[str, Any], root: Path) -> None:
    revision = str(git_output(root, "rev-parse", "HEAD"))
    if revision != lock["revision"]:
        raise ValueError("Image Agent checkout is not at the locked revision")
    if git_output(root, "status", "--porcelain"):
        raise ValueError("Image Agent checkout must be clean for lock verification")
    if content_tree_sha256(root) != lock["source_content_sha256"]:
        raise ValueError("Image Agent source content does not match the release lock")
    image = baseline["repositories"]["image_agent"]
    if git_output(root, "rev-parse", "HEAD^{tree}") != image["git_tree"]:
        raise ValueError("Image Agent Git tree does not match the P0 baseline")
    actual_lock_set = dependency_lock_set_sha256(lock, root)
    if actual_lock_set != lock["dependencies"]["lock_set_sha256"]:
        raise ValueError("Image Agent dependency lock set does not match the release lock")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--image-agent-root", type=Path)
    parser.add_argument("--print-revision", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        lock = load_object(args.lock)
        validate_lock_shape(lock)
        if args.print_revision:
            print(lock["revision"])
            return 0
        baseline = load_object(args.baseline)
        verify_cross_references(lock, baseline)
        verify_harness_baseline(baseline)
        verify_submodule_reference(lock)
        for item in lock["dependencies"]["files"]:
            if item["scope"] == "harness":
                path = ROOT / item["path"]
                if sha256_file(path) != item["sha256"]:
                    raise ValueError(f"Harness-side Image dependency drifted: {item['path']}")
        source_root = (
            args.image_agent_root.resolve()
            if args.image_agent_root is not None
            else (ROOT / lock["embedded_path"]).resolve()
        )
        verify_image_source(lock, baseline, source_root)
    except (KeyError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Image Agent lock verification failed: {exc}", file=sys.stderr)
        return 1
    print("P0 integration baseline, Image Agent submodule and source checkout verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
