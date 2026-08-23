"""Bounded, source-citing asset tools exposed to the in-process Master."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..core.errors import HarnessError
from .asset_understanding import AssetUnderstandingService
from .assets import AssetService


class AssetToolRegistry:
    def __init__(
        self, assets: AssetService, understanding: AssetUnderstandingService
    ) -> None:
        self.assets = assets
        self.understanding = understanding

    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        identifier = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,127}$"}
        return [
            {
                "name": "list_assets",
                "description": "List task assets with parse status, page count and summary.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
            },
            {
                "name": "read_asset_blocks",
                "description": "Read cited blocks by id or an inclusive page range.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["asset_id"],
                    "properties": {
                        "asset_id": identifier,
                        "block_ids": {
                            "type": "array",
                            "maxItems": 100,
                            "items": identifier,
                            "uniqueItems": True,
                        },
                        "page_start": {"type": "integer", "minimum": 1},
                        "page_end": {"type": "integer", "minimum": 1},
                    },
                },
            },
            {
                "name": "search_asset",
                "description": "Search extracted text and return source-located matching blocks.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["asset_id", "query"],
                    "properties": {
                        "asset_id": identifier,
                        "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    },
                },
            },
            {
                "name": "inspect_asset_region",
                "description": "Use VLM for one visual page region and return its citation.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["asset_id", "page", "bbox", "question"],
                    "properties": {
                        "asset_id": identifier,
                        "page": {
                            "oneOf": [{"type": "null"}, {"type": "integer", "minimum": 1}]
                        },
                        "bbox": {
                            "type": "array",
                            "items": {"type": "number", "minimum": 0, "maximum": 1},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "question": {"type": "string", "minLength": 1, "maxLength": 4000},
                    },
                },
            },
            {
                "name": "get_asset_warnings",
                "description": "Return parse, scan quality, truncation and protection warnings.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["asset_id"],
                    "properties": {"asset_id": identifier},
                },
            },
        ]

    def execute(
        self,
        task_id: str,
        name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if name == "list_assets":
            self._exact(arguments, set())
            return {"assets": self._list_assets(task_id)}
        if name == "read_asset_blocks":
            return self._read_blocks(task_id, arguments)
        if name == "search_asset":
            self._exact(arguments, {"asset_id", "query"})
            return self._search(task_id, arguments["asset_id"], arguments["query"])
        if name == "inspect_asset_region":
            self._exact(arguments, {"asset_id", "page", "bbox", "question"})
            return self.understanding.inspect_region(
                task_id,
                self._string(arguments["asset_id"], "asset_id", 128),
                page=arguments["page"],
                bbox=arguments["bbox"],
                question=self._string(arguments["question"], "question", 4000),
                idempotency_key=idempotency_key,
            )
        if name == "get_asset_warnings":
            self._exact(arguments, {"asset_id"})
            asset_id = self._string(arguments["asset_id"], "asset_id", 128)
            document = self.understanding.understand(task_id, asset_id)
            return {"asset_id": asset_id, "warnings": deepcopy(document["warnings"])}
        raise HarnessError("VALIDATION_ERROR", "Master requested an unknown asset tool.")

    def _list_assets(self, task_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        inputs = [
            item
            for item in self.assets.list_assets(task_id)
            if item["manifest"]["producer_instance_id"] is None
        ]
        documents = {
            item["asset_id"]: item
            for item in self.understanding.prepare(
                task_id, [item["manifest"]["asset_id"] for item in inputs]
            )
        }
        for item in inputs:
            manifest = item["manifest"]
            document = documents[manifest["asset_id"]]
            rows.append(
                {
                    "asset_id": manifest["asset_id"],
                    "filename": manifest["relative_path"].rsplit("/", 1)[-1],
                    "media_type": document["media_type"],
                    "page_count": document["page_count"],
                    "status": document["status"],
                    "summary": document["summary"],
                }
            )
        return rows

    def _read_blocks(self, task_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = {"asset_id", "block_ids", "page_start", "page_end"}
        if set(arguments) - allowed or "asset_id" not in arguments:
            raise HarnessError("VALIDATION_ERROR", "Invalid read_asset_blocks arguments.")
        asset_id = self._string(arguments["asset_id"], "asset_id", 128)
        block_ids = arguments.get("block_ids")
        page_start = arguments.get("page_start")
        page_end = arguments.get("page_end")
        by_ids = block_ids is not None
        by_pages = page_start is not None or page_end is not None
        if by_ids == by_pages:
            raise HarnessError(
                "VALIDATION_ERROR", "Select block_ids or one complete page range."
            )
        document = self.understanding.understand(task_id, asset_id)
        if by_ids:
            if (
                not isinstance(block_ids, list)
                or len(block_ids) > 100
                or not all(isinstance(item, str) for item in block_ids)
            ):
                raise HarnessError("VALIDATION_ERROR", "Invalid block id selection.")
            selected = [item for item in document["blocks"] if item["block_id"] in block_ids]
        else:
            if (
                not isinstance(page_start, int)
                or not isinstance(page_end, int)
                or page_start < 1
                or page_end < page_start
                or page_end - page_start > 99
            ):
                raise HarnessError("VALIDATION_ERROR", "Invalid page range.")
            selected = [
                item
                for item in document["blocks"]
                if item["page"] is not None and page_start <= item["page"] <= page_end
            ]
        return {"asset_id": asset_id, "blocks": deepcopy(selected[:100])}

    def _search(self, task_id: str, asset_id: Any, query: Any) -> dict[str, Any]:
        asset_id = self._string(asset_id, "asset_id", 128)
        query = self._string(query, "query", 500).strip()
        if not query:
            raise HarnessError("VALIDATION_ERROR", "The asset search query is empty.")
        document = self.understanding.understand(task_id, asset_id)
        terms = [item for item in query.casefold().split() if item]
        matches = [
            item
            for item in document["blocks"]
            if all(term in item["text"].casefold() for term in terms)
        ]
        return {"asset_id": asset_id, "query": query, "matches": deepcopy(matches[:20])}

    @staticmethod
    def _exact(arguments: dict[str, Any], expected: set[str]) -> None:
        if set(arguments) != expected:
            raise HarnessError("VALIDATION_ERROR", "Master asset tool arguments are invalid.")

    @staticmethod
    def _string(value: Any, field: str, limit: int) -> str:
        if not isinstance(value, str) or not value or len(value) > limit:
            raise HarnessError("VALIDATION_ERROR", f"Invalid {field}.")
        return value
