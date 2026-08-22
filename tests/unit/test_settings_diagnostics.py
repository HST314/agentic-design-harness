from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from harness.core.errors import HarnessError
from harness.services.configuration import (
    IMAGE_STATE_ROLES,
    ConfigurationService,
    GlobalConfigBody,
    ImageModelConfig,
    ModelBinding,
)
from harness.services.credentials import CredentialPoolService
from harness.services.settings_diagnostics import SettingsDiagnosticsService
from harness.storage.repository import Actor
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

    def test_preflight_is_zero_cost_and_paid_smoke_is_redacted_and_idempotent(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def provider_request(credential, model, size):
            calls.append((credential.credential_pair_id, model, size))
            return {"data": [{"url": "https://signed.example.invalid/result"}]}

        diagnostics = SettingsDiagnosticsService(
            self.configuration,
            self.credentials,
            self.store.layout.control_root,
            provider_request,
        )
        preflight = diagnostics.preflight(1)
        self.assertEqual(preflight["status"], "READY")
        self.assertFalse(preflight["paid_request_performed"])
        self.assertEqual(calls, [])

        result = diagnostics.run_paid_smoke(
            expected_config_revision=1,
            credential_pair_id="ark_primary",
            credential_pair_revision=1,
            operation_id="smoke_once",
            actor=Actor("human", "tester"),
        )
        replay = diagnostics.run_paid_smoke(
            expected_config_revision=1,
            credential_pair_id="ark_primary",
            credential_pair_revision=1,
            operation_id="smoke_once",
            actor=Actor("human", "tester"),
        )
        self.assertEqual(result, replay)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["paid_request_performed"])
        self.assertGreaterEqual(result["duration_ms"], 0)
        serialized = json.dumps(result)
        self.assertNotIn(self.secret, serialized)
        self.assertNotIn("signed.example", serialized)
        self.assertNotIn("https://ark.example.invalid/api/v3", serialized)

    def test_unknown_paid_outcome_is_never_automatically_replayed(self) -> None:
        calls = 0

        def provider_request(_credential, _model, _size):
            nonlocal calls
            calls += 1
            raise URLError("connection closed after request")

        diagnostics = SettingsDiagnosticsService(
            self.configuration,
            self.credentials,
            self.store.layout.control_root,
            provider_request,
        )
        with self.assertRaises(HarnessError) as failed:
            diagnostics.run_paid_smoke(
                expected_config_revision=1,
                credential_pair_id="ark_primary",
                credential_pair_revision=1,
                operation_id="smoke_unknown",
                actor=Actor("human", "tester"),
            )
        self.assertEqual(failed.exception.code, "PROVIDER_DIAGNOSTIC_FAILED")
        with self.assertRaises(HarnessError) as replay:
            diagnostics.run_paid_smoke(
                expected_config_revision=1,
                credential_pair_id="ark_primary",
                credential_pair_revision=1,
                operation_id="smoke_unknown",
                actor=Actor("human", "tester"),
            )
        self.assertEqual(replay.exception.code, "INVALID_STATE_TRANSITION")
        self.assertEqual(calls, 1)

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
