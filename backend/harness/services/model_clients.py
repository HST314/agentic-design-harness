"""Typed OpenAI-compatible text and vision model boundaries for Ark."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..core.config_kernel import ConfigSnapshot, ModelDefinition

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_TOOL_ARGUMENT_BYTES = 256 * 1024


class ModelClientFailure(RuntimeError):
    """A credential-safe, provider-independent model failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    total_tokens: int
    raw_usage: dict[str, int]


@dataclass(frozen=True, slots=True)
class ModelResult:
    request_id: str
    provider_request_id: str | None
    provider: str
    model: str
    call_type: str
    output: dict[str, Any] | None
    tool_calls: tuple[ToolCall, ...]
    usage: ModelUsage


class TextModelClient(Protocol):
    def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        response_schema: dict[str, Any],
        idempotency_key: str,
    ) -> ModelResult: ...


class VisionModelClient(Protocol):
    def inspect_image(
        self,
        *,
        image_bytes: bytes,
        media_type: str,
        prompt: str,
        response_schema: dict[str, Any],
        idempotency_key: str,
    ) -> ModelResult: ...


class ModelClientFactory(Protocol):
    def text(
        self, snapshot: ConfigSnapshot, model_id: str, *, timeout_seconds: float
    ) -> TextModelClient: ...

    def vision(
        self, snapshot: ConfigSnapshot, model_id: str, *, timeout_seconds: float
    ) -> VisionModelClient: ...


class OpenAICompatibleProviderAdapter:
    """Bind one task snapshot to Ark's OpenAI-compatible chat endpoint."""

    def text(
        self, snapshot: ConfigSnapshot, model_id: str, *, timeout_seconds: float
    ) -> TextModelClient:
        return _OpenAICompatibleChatClient(
            snapshot,
            _resolve_model(snapshot, "text_models", model_id),
            timeout_seconds=timeout_seconds,
            call_type="reasoning_llm",
        )

    def vision(
        self, snapshot: ConfigSnapshot, model_id: str, *, timeout_seconds: float
    ) -> VisionModelClient:
        return _OpenAICompatibleChatClient(
            snapshot,
            _resolve_model(snapshot, "vlm_models", model_id),
            timeout_seconds=timeout_seconds,
            call_type="vision_language_model",
        )


class _OpenAICompatibleChatClient:
    def __init__(
        self,
        snapshot: ConfigSnapshot,
        model: ModelDefinition,
        *,
        timeout_seconds: float,
        call_type: str,
    ) -> None:
        self._model = model
        self._provider = snapshot.providers.providers[model.provider]
        self._timeout_seconds = timeout_seconds
        self._call_type = call_type

    def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        response_schema: dict[str, Any],
        idempotency_key: str,
    ) -> ModelResult:
        payload: dict[str, Any] = {
            "model": self._model.model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": True,
                    "schema": response_schema,
                },
            },
            **dict(self._model.parameters),
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": definition} for definition in tools
            ]
            payload["tool_choice"] = "auto"
        return self._request(payload, idempotency_key)

    def inspect_image(
        self,
        *,
        image_bytes: bytes,
        media_type: str,
        prompt: str,
        response_schema: dict[str, Any],
        idempotency_key: str,
    ) -> ModelResult:
        if media_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ModelClientFailure("MODEL_INPUT_INVALID", "Unsupported vision image type.")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload: dict[str, Any] = {
            "model": self._model.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                        },
                    ],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "asset_visual_analysis",
                    "strict": True,
                    "schema": response_schema,
                },
            },
            **dict(self._model.parameters),
        }
        return self._request(payload, idempotency_key)

    def _request(self, payload: dict[str, Any], idempotency_key: str) -> ModelResult:
        if not idempotency_key or len(idempotency_key) > 256:
            raise ModelClientFailure("MODEL_INPUT_INVALID", "Invalid model idempotency key.")
        request_id = "model_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
        encoded_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode()
        if len(encoded_payload) > _MAX_REQUEST_BYTES:
            raise ModelClientFailure(
                "MODEL_INPUT_INVALID", "Model request exceeded the size limit."
            )
        request = Request(
            f"{self._provider.base_url}/chat/completions",
            data=encoded_payload,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._provider.api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise ModelClientFailure(
                "MODEL_PROVIDER_ERROR",
                f"Model provider rejected the request with HTTP {exc.code}.",
            ) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise ModelClientFailure(
                "MODEL_PROVIDER_UNAVAILABLE", "Model provider is temporarily unavailable."
            ) from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ModelClientFailure(
                "MODEL_PROVIDER_ERROR", "Model provider response exceeded the size limit."
            )
        try:
            response_payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelClientFailure(
                "MODEL_PROVIDER_ERROR", "Model provider returned invalid JSON."
            ) from exc
        return self._parse_response(response_payload, request_id)

    def _parse_response(self, payload: Any, request_id: str) -> ModelResult:
        if not isinstance(payload, dict):
            self._invalid("Model provider response must be an object.")
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            self._invalid("Model provider response must contain exactly one choice.")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            self._invalid("Model provider response is missing its assistant message.")
        tool_calls = self._parse_tool_calls(message.get("tool_calls", []))
        output = None
        if not tool_calls:
            content = self._text_content(message.get("content"))
            try:
                parsed = json.loads(content)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ModelClientFailure(
                    "MODEL_OUTPUT_INVALID", "Model returned invalid structured output."
                ) from exc
            if not isinstance(parsed, dict):
                self._invalid_output("Model structured output must be an object.")
            output = cast(dict[str, Any], parsed)
        usage = self._usage(payload.get("usage"))
        provider_request_id = payload.get("id")
        if provider_request_id is not None and not isinstance(provider_request_id, str):
            self._invalid("Model provider returned an invalid request id.")
        return ModelResult(
            request_id=request_id,
            provider_request_id=cast(str | None, provider_request_id),
            provider=self._model.provider,
            model=self._model.model,
            call_type=self._call_type,
            output=output,
            tool_calls=tool_calls,
            usage=usage,
        )

    def _parse_tool_calls(self, raw_calls: Any) -> tuple[ToolCall, ...]:
        if raw_calls is None:
            return ()
        if not isinstance(raw_calls, list) or len(raw_calls) > 32:
            self._invalid("Model provider returned invalid tool calls.")
        parsed: list[ToolCall] = []
        for raw in raw_calls:
            function = raw.get("function") if isinstance(raw, dict) else None
            call_id = raw.get("id") if isinstance(raw, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not name
                or not isinstance(arguments, str)
                or len(arguments.encode()) > _MAX_TOOL_ARGUMENT_BYTES
            ):
                self._invalid("Model provider returned a malformed tool call.")
            try:
                decoded = json.loads(cast(str, arguments))
            except json.JSONDecodeError as exc:
                raise ModelClientFailure(
                    "MODEL_OUTPUT_INVALID", "Model returned invalid tool arguments."
                ) from exc
            if not isinstance(decoded, dict):
                self._invalid_output("Model tool arguments must be an object.")
            parsed.append(
                ToolCall(
                    cast(str, call_id),
                    cast(str, name),
                    cast(dict[str, Any], decoded),
                )
            )
        return tuple(parsed)

    @staticmethod
    def _text_content(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [
                item.get("text")
                for item in value
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if parts and all(isinstance(item, str) for item in parts):
                return "".join(cast(list[str], parts))
        raise ModelClientFailure("MODEL_PROVIDER_ERROR", "Model response content is invalid.")

    @staticmethod
    def _usage(value: Any) -> ModelUsage:
        raw = value if isinstance(value, dict) else {}

        def integer(name: str, nested: str | None = None) -> int:
            candidate: Any = raw
            if nested is not None:
                candidate = raw.get(nested, {})
            candidate = candidate.get(name, 0) if isinstance(candidate, dict) else 0
            return candidate if isinstance(candidate, int) and candidate >= 0 else 0

        input_tokens = integer("prompt_tokens")
        output_tokens = integer("completion_tokens")
        total_tokens = integer("total_tokens")
        if total_tokens != input_tokens + output_tokens:
            total_tokens = input_tokens + output_tokens
        cached = min(input_tokens, integer("cached_tokens", "prompt_tokens_details"))
        reasoning = min(output_tokens, integer("reasoning_tokens", "completion_tokens_details"))
        safe_raw = {
            key: item
            for key, item in raw.items()
            if isinstance(key, str) and isinstance(item, int) and item >= 0
        }
        return ModelUsage(
            input_tokens,
            output_tokens,
            cached,
            reasoning,
            total_tokens,
            safe_raw,
        )

    @staticmethod
    def _invalid(message: str) -> NoReturn:
        raise ModelClientFailure("MODEL_PROVIDER_ERROR", message)

    @staticmethod
    def _invalid_output(message: str) -> NoReturn:
        raise ModelClientFailure("MODEL_OUTPUT_INVALID", message)


def _resolve_model(
    snapshot: ConfigSnapshot, category: str, model_id: str
) -> ModelDefinition:
    models = cast(tuple[ModelDefinition, ...], getattr(snapshot.model_list, category))
    model = next((item for item in models if item.id == model_id), None)
    if model is None:
        raise ModelClientFailure(
            "MODEL_CONFIGURATION_INVALID",
            f'Model id "{model_id}" is not registered in {category}.',
        )
    return model
