"""Redacted settings checks and explicitly confirmed Ark image smoke runs."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import Actor, utc_now
from .configuration import IMAGE_STATE_ROLES, ConfigurationService
from .credentials import CredentialPoolService, ResolvedCredential

ProviderRequest = Callable[[ResolvedCredential, str, str], dict[str, Any]]


class SettingsDiagnosticsService:
    """Validate saved Ark configuration without accidentally performing paid work."""

    def __init__(
        self,
        configuration: ConfigurationService,
        credentials: CredentialPoolService,
        control_root: Path,
        provider_request: ProviderRequest | None = None,
    ) -> None:
        self.configuration = configuration
        self.credentials = credentials
        self.provider_request = provider_request or self._request_ark_image
        self.intent_root = control_root / "diagnostics" / "paid-smoke-intents"
        self.intent_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path = control_root / "locks" / "settings-diagnostics.lock"

    def preflight(self, expected_config_revision: int) -> dict[str, Any]:
        config = self.configuration.get_global()
        if config is None:
            raise HarnessError("VALIDATION_ERROR", "Global configuration is not initialized.")
        if config["revision"] != expected_config_revision:
            raise HarnessError(
                "REVISION_CONFLICT",
                "The global configuration changed before diagnostics ran.",
                {
                    "expected_revision": expected_config_revision,
                    "actual_revision": config["revision"],
                },
            )
        bindings = {
            item["state"]: item for item in config["image_model_config"]["state_bindings"]
        }
        provider = config["image_provider"]
        role_correct = set(bindings) == set(IMAGE_STATE_ROLES) and all(
            bindings[state]["model_role"] == role
            for state, role in IMAGE_STATE_ROLES.items()
        )
        consistent = all(item["provider"] == provider for item in bindings.values())
        enabled_credentials = [
            item
            for item in self.credentials.list_redacted()
            if item["enabled"] and item["provider"] == provider
        ]
        checks = [
            self._check(
                "provider",
                provider == "ark",
                "Provider 已设置为 Ark。",
                "将 Image Provider 和六条模型路由统一设置为 ark。",
            ),
            self._check(
                "six_state_routes",
                role_correct,
                "六个 Image 工作流状态及模型能力完整。",
                "补齐六个状态, 并按 reasoning、文生图和 VLM 能力绑定模型。",
            ),
            self._check(
                "provider_consistency",
                consistent,
                "六条模型路由与 Image Provider 一致。",
                "将所有模型路由的 Provider 改为当前 Image Provider。",
            ),
            self._check(
                "credential_pair",
                bool(enabled_credentials),
                "已找到启用的完整凭据对。",
                "保存至少一个启用的 Ark Key Pair 后重新预检。",
            ),
            {
                "check_id": "cost_safety",
                "status": "PASS",
                "message": "本次预检未向 Provider 发送请求, 也不会产生图片费用。",
                "recovery": None,
            },
        ]
        return {
            "schema_version": "1.0",
            "status": "READY"
            if all(item["status"] == "PASS" for item in checks)
            else "BLOCKED",
            "config_revision": config["revision"],
            "provider": provider,
            "model_config_id": config["image_model_config"]["model_config_id"],
            "credential_pairs": enabled_credentials,
            "checks": checks,
            "paid_request_performed": False,
            "checked_at": utc_now(),
        }

    def run_paid_smoke(
        self,
        *,
        expected_config_revision: int,
        credential_pair_id: str,
        credential_pair_revision: int,
        operation_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        validate_identifier(operation_id, "operation_id")
        validate_identifier(credential_pair_id, "credential_pair_id")
        preflight = self.preflight(expected_config_revision)
        if preflight["status"] != "READY":
            raise HarnessError(
                "VALIDATION_ERROR",
                "Configuration preflight must pass before a paid smoke run.",
            )
        config = self.configuration.get_global()
        assert config is not None
        binding = next(
            item
            for item in config["image_model_config"]["state_bindings"]
            if item["state"] == "initial_candidate_generation"
        )
        resolved = self.credentials.resolve_active_pair(
            credential_pair_id, credential_pair_revision
        )
        if resolved.provider != "ark" or binding["provider"] != "ark":
            raise HarnessError(
                "CREDENTIAL_PAIR_INVALID",
                "The paid smoke run requires one active Ark credential pair.",
            )
        request_identity = {
            "config_revision": expected_config_revision,
            "credential_pair_id": credential_pair_id,
            "credential_pair_revision": credential_pair_revision,
            "model": binding["model"],
        }
        request_sha256 = digest_json(request_identity)
        intent_path = self.intent_root / f"{operation_id}.json"
        with FileLock(self.lock_path, self.configuration.store.lock_timeout_seconds):
            if intent_path.exists():
                existing = read_json(intent_path)
                if existing.get("request_sha256") != request_sha256:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The diagnostics operation id was reused for another smoke request.",
                    )
                if existing.get("state") == "COMMITTED":
                    return deepcopy(existing["result"])
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "The previous paid smoke outcome is unknown; use a new explicit "
                    "confirmation to run again.",
                )
            intent = {
                "schema_version": "1.0",
                "operation_id": operation_id,
                "request_sha256": request_sha256,
                "request": request_identity,
                "actor": actor.as_dict(),
                "state": "PREPARED",
                "prepared_at": utc_now(),
                "result": None,
            }
            atomic_write_json(intent_path, intent, mode=0o600)
            started_at = time.monotonic()
            try:
                provider_result = self.provider_request(
                    resolved,
                    binding["model"],
                    config["image_runtime_policy"]["default_output_size"],
                )
            except (HTTPError, URLError, TimeoutError, ValueError, OSError):
                atomic_write_json(
                    intent_path,
                    {**intent, "state": "OUTCOME_UNKNOWN", "failed_at": utc_now()},
                    mode=0o600,
                )
                raise HarnessError(
                    "PROVIDER_DIAGNOSTIC_FAILED",
                    "Ark did not return a verifiable smoke result. "
                    "No automatic retry was attempted.",
                ) from None
            duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
            images = provider_result.get("data")
            if (
                not isinstance(images, list)
                or not images
                or not all(
                    isinstance(item, dict)
                    and isinstance(item.get("url"), str)
                    and bool(item["url"])
                    for item in images
                )
            ):
                atomic_write_json(
                    intent_path,
                    {**intent, "state": "OUTCOME_UNKNOWN", "failed_at": utc_now()},
                    mode=0o600,
                )
                raise HarnessError(
                    "PROVIDER_DIAGNOSTIC_FAILED",
                    "Ark returned no image result for the paid smoke request.",
                )
            result = {
                "schema_version": "1.0",
                "status": "PASSED",
                "config_revision": expected_config_revision,
                "provider": "ark",
                "model": binding["model"],
                "credential_pair": resolved.safe_summary() | {"enabled": True},
                "generated_count": len(images),
                "duration_ms": duration_ms,
                "paid_request_performed": True,
                "completed_at": utc_now(),
            }
            atomic_write_json(
                intent_path,
                {**intent, "state": "COMMITTED", "result": result},
                mode=0o600,
            )
            return deepcopy(result)

    @staticmethod
    def _check(
        check_id: str,
        passed: bool,
        success: str,
        recovery: str,
    ) -> dict[str, Any]:
        return {
            "check_id": check_id,
            "status": "PASS" if passed else "BLOCKED",
            "message": success if passed else recovery,
            "recovery": None if passed else recovery,
        }

    @staticmethod
    def _request_ark_image(
        credential: ResolvedCredential,
        model: str,
        size: str,
    ) -> dict[str, Any]:
        endpoint = f"{credential.base_url.rstrip('/')}/images/generations"
        request = Request(
            endpoint,
            data=json.dumps(
                {
                    "model": model,
                    "prompt": "A minimal black square centered on a white background.",
                    "size": size,
                    "response_format": "url",
                    "n": 1,
                    "watermark": False,
                }
            ).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {credential.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=180) as response:
            raw = response.read(2_000_000)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Provider response is not an object")
        return payload
