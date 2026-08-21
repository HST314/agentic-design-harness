"""Opaque keyset pagination shared by versioned list endpoints."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from typing import Any, Literal, NoReturn

from ..core.errors import HarnessError

SortOrder = Literal["asc", "desc"]


def paginate(
    items: list[dict[str, Any]],
    *,
    scope: str,
    fields: tuple[str, ...],
    limit: int,
    cursor: str | None,
    order: SortOrder,
) -> dict[str, Any]:
    """Return a deterministic keyset page without exposing filesystem offsets."""

    if not fields or limit < 1 or limit > 200:
        raise HarnessError("VALIDATION_ERROR", "The pagination parameters are invalid.")
    reverse = order == "desc"
    ordered = sorted(items, key=lambda item: _key(item, fields), reverse=reverse)
    if cursor is not None:
        after = _decode_cursor(cursor, scope, order, len(fields))
        comparator = (lambda key: key < after) if reverse else (lambda key: key > after)
        ordered = [item for item in ordered if comparator(_key(item, fields))]
    has_more = len(ordered) > limit
    page_items = ordered[:limit]
    next_cursor = None
    if has_more and page_items:
        next_cursor = _encode_cursor(scope, order, _key(page_items[-1], fields))
    return {
        "items": deepcopy(page_items),
        "page": {
            "limit": limit,
            "order": order,
            "has_more": has_more,
            "next_cursor": next_cursor,
        },
    }


def _key(item: dict[str, Any], fields: tuple[str, ...]) -> tuple[str | int, ...]:
    values: list[str | int] = []
    for field in fields:
        value = item.get(field)
        if not isinstance(value, str | int) or isinstance(value, bool):
            raise HarnessError(
                "INTERNAL_ERROR",
                "A list item is missing its stable pagination key.",
            )
        values.append(value)
    return tuple(values)


def _encode_cursor(scope: str, order: SortOrder, key: tuple[str | int, ...]) -> str:
    raw = json.dumps(
        {"v": 1, "scope": scope, "order": order, "key": list(key)},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(
    value: str,
    scope: str,
    order: SortOrder,
    key_length: int,
) -> tuple[str | int, ...]:
    if not value or len(value) > 2048:
        _invalid_cursor()
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode("ascii"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        _invalid_cursor()
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("scope") != scope
        or payload.get("order") != order
        or not isinstance(payload.get("key"), list)
        or len(payload["key"]) != key_length
        or any(
            not isinstance(item, str | int) or isinstance(item, bool)
            for item in payload["key"]
        )
    ):
        _invalid_cursor()
    return tuple(payload["key"])


def _invalid_cursor() -> NoReturn:
    raise HarnessError("VALIDATION_ERROR", "The pagination cursor is invalid.")
