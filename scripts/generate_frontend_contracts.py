#!/usr/bin/env python3
"""Generate deterministic TypeScript types from the frozen JSON contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts" / "v1"
TARGET = ROOT / "frontend" / "src" / "api" / "generated-contracts.ts"
ROOT_TYPES = (
    ("main-task.schema.json", "ContractMainTask"),
    ("stage.schema.json", "ContractStage"),
    ("work-item.schema.json", "ContractWorkItem"),
    ("task-intake.schema.json", "ContractTaskIntake"),
    ("master-message.schema.json", "ContractMasterMessage"),
    ("plan-proposal.schema.json", "ContractPlanProposal"),
    (
        "task-navigation-metadata.schema.json",
        "ContractTaskNavigationMetadata",
    ),
    ("agent-instance.schema.json", "ContractAgentInstance"),
    ("approval-request.schema.json", "ContractApprovalRequest"),
    ("delivery.schema.json", "ContractDelivery"),
    ("asset-manifest.schema.json", "ContractAssetManifest"),
    ("inbox-item.schema.json", "ContractInboxItem"),
    ("token-usage-event-v1.1.schema.json", "ContractTokenUsageEvent"),
    ("task-card-v1.1.schema.json", "ContractTaskCard"),
)
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"contract is not an object: {path}")
    return value


def _resolve_ref(current: Path, reference: str) -> tuple[Path, dict[str, Any]]:
    file_name, _, fragment = reference.partition("#")
    path = current if not file_name else (current.parent / file_name).resolve()
    value: Any = _load(path)
    if fragment:
        for token in fragment.removeprefix("/").split("/"):
            key = token.replace("~1", "/").replace("~0", "~")
            value = value[key]
    if not isinstance(value, dict):
        raise ValueError(f"schema reference is not an object: {reference}")
    return path, value


def _literal(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return json.dumps(value, ensure_ascii=False)


def _property(name: str) -> str:
    return name if _IDENTIFIER.fullmatch(name) else json.dumps(name, ensure_ascii=False)


def _array_item(value: str) -> str:
    return f"({value})" if " | " in value else value


def _ts_type(schema: dict[str, Any], current: Path, seen: frozenset[str] = frozenset()) -> str:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        marker = f"{current}:{reference}"
        if marker in seen:
            return "unknown"
        path, resolved = _resolve_ref(current, reference)
        return _ts_type(resolved, path, seen | {marker})
    if "const" in schema:
        return _literal(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return " | ".join(_literal(item) for item in enum)
    alternatives = schema.get("oneOf") or schema.get("anyOf")
    properties = schema.get("properties")
    schema_type = schema.get("type")
    if isinstance(alternatives, list) and alternatives and (
        schema_type == "object" or isinstance(properties, dict)
    ):
        base = dict(schema)
        base.pop("oneOf", None)
        base.pop("anyOf", None)
        variants = " | ".join(
            _ts_type(item, current, seen)
            for item in alternatives
            if isinstance(item, dict)
        )
        return f"{_ts_type(base, current, seen)} & ({variants})"
    if isinstance(alternatives, list) and alternatives:
        values = dict.fromkeys(
            _ts_type(item, current, seen)
            for item in alternatives
            if isinstance(item, dict)
        )
        return " | ".join(values) or "unknown"
    intersections = schema.get("allOf")
    if (
        schema_type is None
        and not isinstance(properties, dict)
        and isinstance(intersections, list)
    ):
        values = [
            _ts_type(item, current, seen)
            for item in intersections
            if isinstance(item, dict)
        ]
        useful = list(dict.fromkeys(item for item in values if item != "unknown"))
        return " & ".join(useful) or "unknown"
    if isinstance(schema_type, list):
        values = dict.fromkeys(
            _ts_type({**schema, "type": item}, current, seen) for item in schema_type
        )
        return " | ".join(values)
    if schema_type == "string":
        return "string"
    if schema_type in {"integer", "number"}:
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "null":
        return "null"
    if schema_type == "array":
        item_schema = schema.get("items", {})
        item_type = (
            _ts_type(item_schema, current, seen)
            if isinstance(item_schema, dict)
            else "unknown"
        )
        return f"{_array_item(item_type)}[]"
    if schema_type == "object" or isinstance(properties, dict):
        if isinstance(properties, dict) and properties:
            required = set(schema.get("required", []))
            lines = []
            for name, value in properties.items():
                if not isinstance(value, dict):
                    continue
                optional = "" if name in required else "?"
                lines.append(
                    f"  {_property(name)}{optional}: {_ts_type(value, current, seen)};"
                )
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict):
                lines.append(f"  [key: string]: {_ts_type(additional, current, seen)};")
            return "{\n" + "\n".join(lines) + "\n}"
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            return f"Record<string, {_ts_type(additional, current, seen)}>"
        return "Record<string, unknown>"
    return "unknown"


def _source_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(CONTRACTS.rglob("*.json")):
        digest.update(path.relative_to(CONTRACTS).as_posix().encode("utf-8"))
        digest.update(b"\0")
        normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        digest.update(normalized.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def generate() -> str:
    lines = [
        "/* This file is generated by scripts/generate_frontend_contracts.py. */",
        "/* Do not edit by hand; run `make frontend-contracts`. */",
        "",
        f'export const CONTRACT_SOURCE_SHA256 = "{_source_digest()}" as const;',
        "",
    ]
    for file_name, type_name in ROOT_TYPES:
        path = CONTRACTS / "schemas" / file_name
        lines.append(f"export type {type_name} = {_ts_type(_load(path), path)};")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    generated = generate()
    if args.stdout:
        sys.stdout.write(generated)
        return 0
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != generated:
            print(
                "frontend contract types are stale; run make frontend-contracts",
                file=sys.stderr,
            )
            return 1
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
