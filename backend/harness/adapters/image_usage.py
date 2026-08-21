"""Strict mapping from Image Agent usage observations to Harness contracts."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, NoReturn

from ..core.errors import HarnessError
from ..storage.atomic import digest_json
from ..storage.layout import validate_identifier

_CURSOR = re.compile(r"^usage_([1-9][0-9]*)_[0-9a-f]{16}$")
_CALL_TYPES = frozenset({"reasoning_llm", "vision_language_model", "text_to_image_model"})
_USAGE_BASES = frozenset({"tokens", "image_units", "mixed"})
_TOKEN_FIELDS = frozenset(
    {"input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens"}
)
_RAW_USAGE_COUNTERS = frozenset(
    {
        "accepted_prediction_tokens",
        "audio_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cached_input_tokens",
        "cached_tokens",
        "completion_tokens",
        "image_tokens",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "reasoning_tokens",
        "rejected_prediction_tokens",
        "text_tokens",
        "total_tokens",
    }
)
_RAW_USAGE_DETAIL_OBJECTS = frozenset(
    {
        "completion_tokens_details",
        "input_tokens_details",
        "output_tokens_details",
        "prompt_tokens_details",
    }
)
_RAW_USAGE_DETAIL_COUNTERS = frozenset(
    {
        "accepted_prediction_tokens",
        "audio_tokens",
        "cached_tokens",
        "image_tokens",
        "reasoning_tokens",
        "rejected_prediction_tokens",
        "text_tokens",
    }
)
_SENSITIVE_KEYS = ("api_key", "apikey", "authorization", "access_token", "secret", "cookie")
_SENSITIVE_VALUE = re.compile(
    r"(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,})", re.I
)


def usage_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    match = _CURSOR.fullmatch(cursor)
    if match is None:
        _protocol_error("The Image usage cursor is malformed.")
    return int(match.group(1))


def map_usage_page(
    page: dict[str, Any],
    *,
    previous: int,
    task_id: str,
    instance_id: str,
    credential_pair_ref: str | None,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Validate one producer page and map it without estimating missing facts."""

    items = page.get("items")
    next_cursor = page.get("next_cursor")
    has_more = page.get("has_more")
    if (
        not isinstance(items, list)
        or type(next_cursor) is not int
        or next_cursor < previous
        or type(has_more) is not bool
        or len(items) > 500
    ):
        _protocol_error("The Image usage page is malformed.")
    events: list[dict[str, Any]] = []
    sequence = previous
    for item in items:
        if not isinstance(item, dict):
            _protocol_error("The Image usage observation is malformed.")
        current = item.get("sequence")
        if type(current) is not int or current <= sequence:
            _protocol_error("The Image usage sequence is malformed.")
        sequence = current
        events.append(
            _map_usage_item(
                item,
                task_id=task_id,
                instance_id=instance_id,
                credential_pair_ref=credential_pair_ref,
            )
        )
    if (items and next_cursor != sequence) or (not items and next_cursor != previous):
        _protocol_error("The Image usage cursor is inconsistent.")
    if has_more and (not items or next_cursor <= previous):
        _protocol_error("The Image usage cursor did not advance.")
    return events, next_cursor, has_more


def _map_usage_item(
    item: dict[str, Any],
    *,
    task_id: str,
    instance_id: str,
    credential_pair_ref: str | None,
) -> dict[str, Any]:
    usage_id = item.get("usage_id")
    request_id = item.get("request_id")
    provider_request_id = item.get("provider_request_id")
    provider = item.get("provider")
    model = item.get("model")
    call_type = item.get("call_type")
    usage_basis = item.get("usage_basis")
    if not isinstance(usage_id, str):
        _protocol_error("The Image usage id is malformed.")
    validate_identifier(usage_id, "usage_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
        _protocol_error("The Image usage request id is malformed.")
    if provider_request_id is not None and (
        not isinstance(provider_request_id, str) or not 1 <= len(provider_request_id) <= 256
    ):
        _protocol_error("The Image provider request id is malformed.")
    if not isinstance(provider, str) or not 1 <= len(provider) <= 128:
        _protocol_error("The Image usage provider is malformed.")
    if not isinstance(model, str) or not 1 <= len(model) <= 256:
        _protocol_error("The Image usage model is malformed.")
    if call_type not in _CALL_TYPES or usage_basis not in _USAGE_BASES:
        _protocol_error("The Image usage accounting type is malformed.")
    token_usage = _token_usage(item.get("token_usage"))
    billing_units = _billing_units(item.get("billing_units"))
    raw_usage = _raw_usage(item.get("raw_usage"))
    if usage_basis == "tokens" and (token_usage is None or billing_units):
        _protocol_error("The Image token usage basis is inconsistent.")
    if usage_basis == "image_units" and (token_usage is not None or not billing_units):
        _protocol_error("The Image billing-unit usage basis is inconsistent.")
    if usage_basis == "mixed" and (token_usage is None or not billing_units):
        _protocol_error("The Image mixed usage basis is inconsistent.")
    tokens = token_usage or {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }
    sequence = int(item["sequence"])
    return {
        "schema_version": "1.1",
        "event_id": f"usage_{sequence}_{digest_json([instance_id, usage_id])[:16]}",
        "task_id": task_id,
        "instance_id": instance_id,
        "agent_type": "image",
        "request_id": request_id,
        "provider_request_id": provider_request_id,
        "provider": provider,
        "model": model,
        "call_type": call_type,
        "usage_basis": usage_basis,
        "credential_pair_ref": credential_pair_ref,
        **tokens,
        "billing_units": billing_units,
        "raw_usage": raw_usage,
        "occurred_at": _utc_timestamp(item.get("timestamp")),
    }


def _token_usage(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _TOKEN_FIELDS:
        _protocol_error("The Image token usage observation is malformed.")
    if any(type(item) is not int or item < 0 for item in value.values()):
        _protocol_error("The Image token usage observation is malformed.")
    if value["total_tokens"] != value["input_tokens"] + value["output_tokens"]:
        _protocol_error("The Image token totals are inconsistent.")
    if value["cached_input_tokens"] > value["input_tokens"]:
        _protocol_error("The Image cached token count is inconsistent.")
    if value["reasoning_tokens"] > value["output_tokens"]:
        _protocol_error("The Image reasoning token count is inconsistent.")
    return {key: int(value[key]) for key in _TOKEN_FIELDS}


def _billing_units(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 64:
        _protocol_error("The Image billing units are malformed.")
    units: list[dict[str, Any]] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"unit", "quantity", "attributes"}
            or not isinstance(item["unit"], str)
            or not 1 <= len(item["unit"]) <= 64
            or type(item["quantity"]) is not int
            or item["quantity"] < 1
            or not isinstance(item["attributes"], dict)
            or not _safe_json(item)
        ):
            _protocol_error("The Image billing units are malformed.")
        units.append(item)
    return units


def _raw_usage(value: Any) -> dict[str, Any]:
    """Accept only the producer protocol's explicit accounting fields."""

    if not isinstance(value, dict) or not set(value).issubset(
        _RAW_USAGE_COUNTERS | _RAW_USAGE_DETAIL_OBJECTS
    ):
        _protocol_error("The Image raw usage observation is malformed.")
    metrics: dict[str, Any] = {}
    for key, item in value.items():
        if key in _RAW_USAGE_COUNTERS:
            if type(item) is not int or item < 0:
                _protocol_error("The Image raw usage observation is malformed.")
            metrics[key] = item
            continue
        if (
            not isinstance(item, dict)
            or not set(item).issubset(_RAW_USAGE_DETAIL_COUNTERS)
            or any(type(counter) is not int or counter < 0 for counter in item.values())
        ):
            _protocol_error("The Image raw usage observation is malformed.")
        metrics[key] = dict(item)
    return metrics


def _safe_json(value: dict[str, Any]) -> bool:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return False
    if len(encoded) > 16 * 1024:
        return False

    def contains_sensitive_key(item: Any) -> bool:
        if isinstance(item, dict):
            return any(
                any(word in str(key).lower() for word in _SENSITIVE_KEYS)
                or contains_sensitive_key(child)
                for key, child in item.items()
            )
        if isinstance(item, list):
            return any(contains_sensitive_key(child) for child in item)
        if isinstance(item, str):
            return _SENSITIVE_VALUE.search(item) is not None
        return False

    return not contains_sensitive_key(value)


def _utc_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        _protocol_error("The Image usage timestamp is malformed.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _protocol_error("The Image usage timestamp is malformed.")
    if parsed.tzinfo is None:
        _protocol_error("The Image usage timestamp is malformed.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _protocol_error(message: str) -> NoReturn:
    raise HarnessError("VALIDATION_ERROR", message)
