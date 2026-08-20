"""Runtime access to the frozen contracts/v1 source of truth."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .core.errors import HarnessError


class ContractRegistry:
    """Compile and validate every public schema without copying domain enums."""

    def __init__(self, contracts_root: Path) -> None:
        self.root = contracts_root
        self.schemas: dict[str, dict[str, Any]] = {}
        registry = Registry()
        for schema_path in sorted((self.root / "schemas").glob("*.schema.json")):
            document = json.loads(schema_path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(document)
            self.schemas[schema_path.name] = document
            registry = registry.with_resource(document["$id"], Resource.from_contents(document))
        self._registry = registry
        self._validators = {
            name: Draft202012Validator(
                schema,
                registry=self._registry,
                format_checker=FormatChecker(),
            )
            for name, schema in self.schemas.items()
        }

    def _validator_name(self, schema_name: str, payload: Any) -> str:
        normalized = (
            schema_name if schema_name.endswith(".schema.json") else f"{schema_name}.schema.json"
        )
        if not isinstance(payload, dict) or "schema_version" not in payload:
            return normalized
        version = str(payload["schema_version"])
        if normalized.endswith(f"-v{version}.schema.json") and normalized in self._validators:
            return normalized
        versioned = normalized.removesuffix(".schema.json") + f"-v{version}.schema.json"
        if versioned in self._validators:
            return versioned
        if version == "1.0":
            return normalized
        raise HarnessError(
            "SCHEMA_VERSION_UNSUPPORTED",
            "The declared contract version is not supported.",
            {"schema_version": version},
        )

    @property
    def ready(self) -> bool:
        return bool(self.schemas) and "common.schema.json" in self.schemas

    def validate(self, schema_name: str, payload: Any) -> None:
        normalized = self._validator_name(schema_name, payload)
        validator = self._validators.get(normalized)
        if validator is None:
            raise HarnessError(
                "VALIDATION_ERROR",
                "Unknown contract schema.",
                {"schema": normalized},
            )
        errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
        if errors:
            first = errors[0]
            path = ".".join(str(item) for item in first.absolute_path) or "$"
            raise HarnessError(
                "VALIDATION_ERROR",
                "Payload failed contract validation.",
                {
                    "schema": normalized,
                    "path": path,
                    "reason": str(first.validator),
                },
            )
