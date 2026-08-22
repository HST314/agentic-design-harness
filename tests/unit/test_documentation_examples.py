from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness.services.configuration import IMAGE_STATE_ROLES, ImageModelConfig
from harness.services.credentials import CredentialPair

ROOT = Path(__file__).resolve().parents[2]


class DocumentationExampleTests(unittest.TestCase):
    def test_ark_credential_example_matches_the_runtime_model(self) -> None:
        value = json.loads(
            (ROOT / "config/examples/ark-credential-pair.json").read_text(encoding="utf-8")
        )

        credential = CredentialPair.model_validate(value)

        self.assertEqual(credential.provider, "ark")
        self.assertNotEqual(credential.api_key_env, credential.base_url_env)

    def test_ark_six_state_routing_matches_the_runtime_model(self) -> None:
        value = json.loads(
            (ROOT / "config/examples/ark-image-model-routing.json").read_text(
                encoding="utf-8"
            )
        )

        routing = ImageModelConfig.model_validate(value)

        self.assertEqual(
            {item.state: item.model_role for item in routing.state_bindings},
            IMAGE_STATE_ROLES,
        )
        self.assertEqual({item.provider for item in routing.state_bindings}, {"ark"})


if __name__ == "__main__":
    unittest.main()
