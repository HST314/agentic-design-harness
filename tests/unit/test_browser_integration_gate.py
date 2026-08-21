from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_browser_integration import (
    assert_redacted_file,
    browser_executable_candidates,
    child_process_environment,
    load_real_provider_environment,
    publish_evidence,
    validate_real_provider_environment,
)


class BrowserIntegrationGateTests(unittest.TestCase):
    def test_crlf_dotenv_aliases_load_without_control_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "provider.env"
            env_file.write_bytes(
                b"MODEL_BASE_URL=https://provider.example/v1\r\n"
                b"MODEL_API_KEY=test-key\r\n"
                b"HARNESS_REAL_PROVIDER_TEXT_MODEL=text-model\r\n"
                b"HARNESS_REAL_PROVIDER_IMAGE_MODEL=image-model\r\n"
                b"HARNESS_REAL_PROVIDER_VLM_MODEL=vlm-model\r\n"
                b"UNRELATED_SECRET=must-not-be-loaded\r\n"
            )
            environment: dict[str, str] = {}

            load_real_provider_environment(env_file, environment)
            configuration = validate_real_provider_environment(environment)

            self.assertEqual(configuration.base_url, "https://provider.example/v1")
            self.assertEqual(configuration.api_key, "test-key")
            self.assertNotIn("UNRELATED_SECRET", environment)
            self.assertTrue(all("\r" not in value for value in environment.values()))

            child_environment = child_process_environment(
                {**environment, "PATH": "/usr/bin", "MODEL_API_KEY": "legacy-key"}
            )
            self.assertEqual(child_environment, {"PATH": "/usr/bin"})

    def test_explicit_environment_wins_over_dotenv_and_control_characters_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "provider.env"
            env_file.write_text(
                "HARNESS_REAL_PROVIDER_API_KEY=file-key\n"
                "HARNESS_REAL_PROVIDER_BASE_URL=https://provider.example/v1\n",
                encoding="utf-8",
            )
            environment = {
                "HARNESS_REAL_PROVIDER_API_KEY": "explicit-key",
                "HARNESS_REAL_PROVIDER_TEXT_MODEL": "text-model",
                "HARNESS_REAL_PROVIDER_IMAGE_MODEL": "image-model",
                "HARNESS_REAL_PROVIDER_VLM_MODEL": "vlm-model",
            }
            load_real_provider_environment(env_file, environment)
            self.assertEqual(environment["HARNESS_REAL_PROVIDER_API_KEY"], "explicit-key")

            environment["HARNESS_REAL_PROVIDER_TEXT_MODEL"] = "text\x01model"
            with self.assertRaisesRegex(RuntimeError, "ASCII control character"):
                validate_real_provider_environment(environment)

    def test_browser_candidates_are_revision_pinned(self) -> None:
        candidates = browser_executable_candidates(Path("/browser-cache"), "1234")
        self.assertTrue(candidates)
        self.assertTrue(all("1234" in str(candidate) for candidate in candidates))
        self.assertTrue(any("chromium_headless_shell-1234" in str(path) for path in candidates))
        self.assertTrue(any("chromium-1234" in str(path) for path in candidates))

    def test_evidence_is_published_only_after_exact_value_redaction_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            leaked = root / "leaked.json"
            leaked.write_text('{"value":"real-secret"}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "redaction scan"):
                assert_redacted_file(leaked, ("real-secret",))
            self.assertNotIn("real-secret", leaked.read_text(encoding="utf-8"))

            staging = root / "staging.json"
            destination = root / "evidence.json"
            staging.write_text(
                json.dumps(
                    {
                        "schema_version": "real-provider-browser-evidence.v2",
                        "execution": {"result": "PASSED"},
                    }
                ),
                encoding="utf-8",
            )
            publish_evidence(staging, destination, ("real-secret",))
            self.assertFalse(staging.exists())
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8"))["execution"]["result"],
                "PASSED",
            )


if __name__ == "__main__":
    unittest.main()
