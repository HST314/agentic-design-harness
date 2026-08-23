#!/usr/bin/env python3
"""Validate the current documentation set without network access."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOC_ROOT = ROOT / "docs"
REQUIRED_ROOT_DOCS = {"README.md", "QUICKSTART.md", "CONTRIBUTING.md"}
REQUIRED_DOCS = {
    "README.md",
    "configuration-architecture-refactor-v1.md",
    "configuration.md",
    "contracts.md",
    "image-agent-integration.md",
    "master-api.md",
    "operations.md",
    "troubleshooting.md",
    "user-guide.md",
}
FORBIDDEN_PATHS = {
    "backend/README.md",
    "contracts/README.md",
    "docs/agent-workbench.md",
    "docs/contract-versioning.md",
    "docs/getting-started.md",
    "docs/master-api-guide.md",
    "docs/master-gateway.md",
    "docs/rfc-v0.2.md",
    "docs/rfc-v0.3-workbench-design.md",
    "docs/single-machine-capacity-slo.md",
    "docs/verification",
    "frontend/README.md",
}
FORBIDDEN_REFERENCES = tuple(sorted(FORBIDDEN_PATHS | {"docs/archive"}))
REQUIRED_QUICKSTART_COMMANDS = (
    "py -3 scripts/dev.py",
    "python3 scripts/dev.py",
    "python3 scripts/dev.py setup",
    "python3 scripts/dev.py doctor",
    "python3 scripts/dev.py start",
)
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
JSON_FENCE_PATTERN = re.compile(r"^```json[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
HEADING_PATTERN = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


class DocumentationError(ValueError):
    """One or more documentation invariants failed."""


def markdown_files() -> list[Path]:
    roots = [ROOT / name for name in sorted(REQUIRED_ROOT_DOCS)]
    return roots + sorted(DOC_ROOT.rglob("*.md"))


def github_slug(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("`", "").strip().lower()
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    return re.sub(r"[ \t]+", "-", value)


def anchors(path: Path) -> set[str]:
    counts: dict[str, int] = {}
    values: set[str] = set()
    text = path.read_text(encoding="utf-8")
    for heading in HEADING_PATTERN.findall(text):
        base = github_slug(heading)
        count = counts.get(base, 0)
        counts[base] = count + 1
        values.add(base if count == 0 else f"{base}-{count}")
    values.update(re.findall(r"\bid=[\"']([^\"']+)[\"']", text))
    return values


def validate_document_set() -> None:
    actual_root = {name for name in REQUIRED_ROOT_DOCS if (ROOT / name).is_file()}
    if actual_root != REQUIRED_ROOT_DOCS:
        raise DocumentationError(
            "missing root documentation: " + ", ".join(sorted(REQUIRED_ROOT_DOCS - actual_root))
        )
    actual_docs = {path.name for path in DOC_ROOT.glob("*.md")}
    if actual_docs != REQUIRED_DOCS:
        missing = sorted(REQUIRED_DOCS - actual_docs)
        unexpected = sorted(actual_docs - REQUIRED_DOCS)
        raise DocumentationError(
            f"documentation set drifted; missing={missing}, unexpected={unexpected}"
        )
    present_forbidden = [
        relative
        for relative in FORBIDDEN_PATHS
        if (ROOT / relative).is_file()
        or ((ROOT / relative).is_dir() and any((ROOT / relative).rglob("*")))
    ]
    if present_forbidden:
        raise DocumentationError(
            "retired documentation still exists: " + ", ".join(present_forbidden)
        )


def validate_links(paths: list[Path]) -> int:
    checked = 0
    anchor_cache: dict[Path, set[str]] = {}
    for source in paths:
        text = source.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("//"):
                continue
            relative = unquote(parsed.path)
            destination = source if not relative else (source.parent / relative).resolve()
            if not destination.is_relative_to(ROOT):
                raise DocumentationError(
                    f"{source.relative_to(ROOT)} links outside the repository: {target}"
                )
            if not destination.exists():
                raise DocumentationError(f"{source.relative_to(ROOT)} has a broken link: {target}")
            if parsed.fragment and destination.suffix.lower() == ".md":
                available = anchor_cache.setdefault(destination, anchors(destination))
                if unquote(parsed.fragment).lower() not in available:
                    raise DocumentationError(
                        f"{source.relative_to(ROOT)} links to a missing heading: {target}"
                    )
            checked += 1
    return checked


def validate_json_blocks(paths: list[Path]) -> int:
    checked = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for index, block in enumerate(JSON_FENCE_PATTERN.findall(text), start=1):
            try:
                json.loads(block)
            except json.JSONDecodeError as exc:
                raise DocumentationError(
                    f"{path.relative_to(ROOT)} JSON block {index} is invalid: {exc}"
                ) from exc
            checked += 1
    return checked


def load_object(relative: str) -> dict[str, object]:
    path = ROOT / relative
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DocumentationError(f"cannot read {relative}: {exc}") from exc
    if not isinstance(value, dict):
        raise DocumentationError(f"{relative} must contain one JSON object")
    return value


def validate_commands_and_references(paths: list[Path]) -> int:
    quickstart = (ROOT / "QUICKSTART.md").read_text(encoding="utf-8")
    missing = [command for command in REQUIRED_QUICKSTART_COMMANDS if command not in quickstart]
    if missing:
        raise DocumentationError("QUICKSTART is missing commands: " + ", ".join(missing))
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    stale = [reference for reference in FORBIDDEN_REFERENCES if reference in combined]
    if stale:
        raise DocumentationError("documentation references retired content: " + ", ".join(stale))
    for relative in (
        "scripts/dev.py",
        "scripts/check_docs.py",
        "scripts/verify_image_agent_lock.py",
    ):
        if not (ROOT / relative).is_file():
            raise DocumentationError(f"documented command entry is missing: {relative}")
    return len(REQUIRED_QUICKSTART_COMMANDS)


def validate_versions() -> int:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    matched = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    if matched is None:
        raise DocumentationError("pyproject.toml does not declare a project version")
    project_version = matched.group(1)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if f"| 当前版本 | `{project_version}` |" not in readme:
        raise DocumentationError("README project version differs from pyproject.toml")

    catalog = load_object("contracts/v1/catalogs/schema-versions.json")
    document_versions = catalog.get("document_versions")
    if not isinstance(document_versions, dict):
        raise DocumentationError("schema version catalog is malformed")
    contracts = (DOC_ROOT / "contracts.md").read_text(encoding="utf-8")
    labels = {
        "default": "默认业务对象",
        "task-card": "TaskCard",
        "token-usage-event": "TokenUsageEvent",
        "delivery-bundle-candidate": "DeliveryBundleCandidate",
        "bundle-manifest": "BundleManifest",
    }
    for key, label in labels.items():
        item = document_versions.get(key)
        if not isinstance(item, dict) or not isinstance(item.get("current"), str):
            raise DocumentationError(f"schema catalog is missing {key}")
        if f"| {label} | `{item['current']}` |" not in contracts:
            raise DocumentationError(f"docs/contracts.md has a stale {label} version")
    return 1 + len(labels)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="write a machine-readable PASS report")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        validate_document_set()
        paths = markdown_files()
        report = {
            "schema_version": "documentation-check.v1",
            "status": "PASSED",
            "checked": {
                "markdown_files": len(paths),
                "local_links": validate_links(paths),
                "json_blocks": validate_json_blocks(paths),
                "documented_commands": validate_commands_and_references(paths),
                "version_bindings": validate_versions(),
            },
        }
        if args.report is not None:
            destination = args.report if args.report.is_absolute() else ROOT / args.report
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except DocumentationError as exc:
        print(f"Documentation check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
