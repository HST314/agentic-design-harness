from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.services.configuration import (
    IMAGE_STATE_ROLES,
    ConfigurationService,
    GlobalConfigBody,
    ImageModelConfig,
    ModelBinding,
)
from harness.services.credentials import CredentialPoolService
from harness.services.settings_diagnostics import SettingsDiagnosticsService
from pydantic import ValidationError
from runtime_helpers import build_store


class SettingsDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = build_store(self.root)
        self.store.start()
        self.configuration = ConfigurationService(self.store)
        base = GlobalConfigBody()
        self.configuration.initialize(
            GlobalConfigBody(
                image_provider="ark",
                image_runtime_policy=base.image_runtime_policy.model_copy(
                    update={"offline_mode": False, "default_output_size": "1024x1024"}
                ),
                image_model_config=ImageModelConfig(
                    model_config_id="ark_test",
                    state_bindings=[
                        ModelBinding(
                            state=state,
                            model_role=role,
                            provider="ark",
                            model=f"ark-{role}",
                        )
                        for state, role in IMAGE_STATE_ROLES.items()
                    ],
                ),
                supervisor=base.supervisor,
            )
        )
        self.credentials = CredentialPoolService(self.store)
        self.secret = "test-provider-value-that-must-stay-redacted"
        self.credentials.configure_pool(
            [
                {
                    "credential_pair_id": "ark_primary",
                    "provider": "ark",
                    "key_id": "ark_key_primary",
                    "base_url": "https://ark.example.invalid/api/v3",
                    "api_key": self.secret,
                    "api_key_env": "ARK_API_KEY",
                    "base_url_env": "ARK_BASE_URL",
                    "revision": 1,
                    "enabled": True,
                }
            ]
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_preflight_is_zero_cost_and_returns_only_redacted_credentials(self) -> None:
        diagnostics = SettingsDiagnosticsService(
            self.configuration,
            self.credentials,
        )
        preflight = diagnostics.preflight(1)
        self.assertEqual(preflight["status"], "READY")
        self.assertFalse(preflight["paid_request_performed"])
        self.assertEqual(preflight["credential_pairs"][0]["key_tail"], "cted")
        self.assertNotIn("api_key", preflight["credential_pairs"][0])

    def test_model_routes_require_all_six_role_correct_states(self) -> None:
        with self.assertRaises(ValidationError):
            ImageModelConfig(
                model_config_id="incomplete",
                state_bindings=[
                    ModelBinding(
                        state="intake_clarify",
                        model_role="reasoning_llm",
                        provider="ark",
                        model="ark-reasoning",
                    )
                ],
            )


if __name__ == "__main__":
    unittest.main()
