"""Redacted, zero-cost checks for the legacy configuration projection."""

from __future__ import annotations

from typing import Any

from ..core.errors import HarnessError
from ..storage.repository import utc_now
from .configuration import IMAGE_STATE_ROLES, ConfigurationService
from .credentials import CredentialPoolService


class SettingsDiagnosticsService:
    """Validate saved Ark configuration without performing Provider work."""

    def __init__(
        self,
        configuration: ConfigurationService,
        credentials: CredentialPoolService,
    ) -> None:
        self.configuration = configuration
        self.credentials = credentials

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
