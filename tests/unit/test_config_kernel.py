from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

import yaml
from harness.core.config_kernel import ConfigurationError, load_config_snapshot
from pydantic import ValidationError

from scripts.dev import ConfigCheckFailed, DevelopmentLauncher, main

ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILES = ("provider.yaml", "model_list.yaml", "runtime.yaml")


class ConfigKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for filename in CONFIG_FILES:
            shutil.copyfile(ROOT / filename, self.root / filename)
        self.write_env()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_env(
        self,
        *,
        api_key: str = "unit-test-secret",
        base_url: str = "https://ark.example.test/api/v3",
    ) -> None:
        (self.root / ".env").write_text(
            f"ARK_API_KEY={api_key}\nARK_BASE_URL={base_url}\n",
            encoding="utf-8",
        )

    def read_yaml(self, filename: str) -> dict[str, object]:
        return yaml.safe_load((self.root / filename).read_text(encoding="utf-8"))

    def write_yaml(self, filename: str, value: dict[str, object]) -> None:
        (self.root / filename).write_text(
            yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def assert_config_error(self, fragment: str) -> ConfigurationError:
        with self.assertRaises(ConfigurationError) as raised:
            load_config_snapshot(self.root, {})
        self.assertIn(fragment, str(raised.exception))
        return raised.exception

    def test_valid_configuration_builds_secret_safe_immutable_snapshot(self) -> None:
        snapshot = load_config_snapshot(self.root, {})

        self.assertEqual(snapshot.runtime.server.port, 18080)
        self.assertTrue(snapshot.revision.startswith("cfg_"))
        self.assertNotIn("unit-test-secret", repr(snapshot))
        with self.assertRaises(ValidationError):
            snapshot.revision = "changed"
        with self.assertRaises(TypeError):
            snapshot.providers.providers["ark"] = snapshot.providers.providers["ark"]
        with self.assertRaises(TypeError):
            snapshot.model_list.text_models[0].parameters["temperature"] = 0.2

    def test_revision_changes_when_a_secret_rotates_without_exposing_it(self) -> None:
        first = load_config_snapshot(self.root, {})
        self.write_env(api_key="rotated-unit-test-secret")
        second = load_config_snapshot(self.root, {})

        self.assertNotEqual(first.revision, second.revision)
        self.assertNotIn("rotated-unit-test-secret", second.model_dump_json())

    def test_process_environment_can_supply_a_deployment_secret(self) -> None:
        (self.root / ".env").write_text(
            "ARK_BASE_URL=https://ark.example.test/api/v3\n", encoding="utf-8"
        )

        snapshot = load_config_snapshot(self.root, {"ARK_API_KEY": "injected-secret"})

        self.assertEqual(
            snapshot.providers.providers["ark"].api_key.get_secret_value(),
            "injected-secret",
        )

    def test_plaintext_provider_secret_is_rejected_and_never_rendered(self) -> None:
        provider = self.read_yaml("provider.yaml")
        provider["providers"]["ark"]["api_key"] = "plaintext-should-not-leak"
        self.write_yaml("provider.yaml", provider)

        error = self.assert_config_error("plaintext secrets are forbidden")
        self.assertNotIn("plaintext-should-not-leak", str(error))

    def test_restricted_env_parser_rejects_shell_and_nested_expansion(self) -> None:
        (self.root / ".env").write_text(
            "export ARK_API_KEY=bad\n"
            "ARK_API_KEY=$(secret-command)\n"
            "ARK_BASE_URL=${NESTED_URL}\n",
            encoding="utf-8",
        )

        error = self.assert_config_error("without shell syntax")
        rendered = str(error)
        self.assertIn("command substitution", rendered)
        self.assertNotIn("secret-command", rendered)

    def test_partial_or_default_environment_replacement_is_rejected(self) -> None:
        provider = self.read_yaml("provider.yaml")
        provider["providers"]["ark"]["base_url"] = "https://${ARK_HOST}/api/v3"
        self.write_yaml("provider.yaml", provider)

        self.assert_config_error("one complete ${ENV_NAME} replacement")

    def test_yaml_schema_is_strict_and_reports_unknown_fields(self) -> None:
        runtime = self.read_yaml("runtime.yaml")
        runtime["server"]["port"] = "18080"
        runtime["server"]["unexpected"] = True
        self.write_yaml("runtime.yaml", runtime)

        error = self.assert_config_error("runtime.yaml: server.port")
        self.assertIn("runtime.yaml: server.unexpected", str(error))

    def test_yaml_parse_error_reports_line_and_column(self) -> None:
        (self.root / "runtime.yaml").write_text(
            "schema_version: '1.0'\nserver: [\n", encoding="utf-8"
        )

        error = self.assert_config_error("invalid YAML")
        self.assertRegex(str(error), r"runtime.yaml: line \d+, column \d+")

    def test_model_ids_are_globally_unique(self) -> None:
        models = self.read_yaml("model_list.yaml")
        models["image_models"][0]["id"] = models["text_models"][0]["id"]
        self.write_yaml("model_list.yaml", models)

        self.assert_config_error("model ids must be globally unique")

    def test_runtime_unknown_model_reference_is_rejected(self) -> None:
        runtime = self.read_yaml("runtime.yaml")
        runtime["models"]["master"] = "missing-text-model"
        self.write_yaml("runtime.yaml", runtime)

        self.assert_config_error('unknown text model id "missing-text-model"')

    def test_runtime_wrong_model_category_is_rejected(self) -> None:
        runtime = self.read_yaml("runtime.yaml")
        runtime["models"]["image_generation"] = "ark-vlm-primary"
        self.write_yaml("runtime.yaml", runtime)

        self.assert_config_error("belongs to vlm_models, expected image_models")

    def test_runtime_reference_requires_position_capabilities(self) -> None:
        models = self.read_yaml("model_list.yaml")
        models["text_models"][0]["capabilities"] = ["structured_output"]
        self.write_yaml("model_list.yaml", models)

        self.assert_config_error("lacks required capabilities: tool_calling")

    def test_sensitive_model_parameters_are_rejected(self) -> None:
        models = self.read_yaml("model_list.yaml")
        models["text_models"][0]["parameters"] = {"api_key": "not-allowed"}
        self.write_yaml("model_list.yaml", models)

        error = self.assert_config_error("must not contain secret")
        self.assertNotIn("not-allowed", str(error))

    def test_non_ark_and_offline_providers_are_rejected(self) -> None:
        provider = self.read_yaml("provider.yaml")
        provider["providers"]["fake"] = {
            "base_url": "http://localhost:9999",
            "api_key": "${FAKE_KEY}",
        }
        self.write_yaml("provider.yaml", provider)
        (self.root / ".env").write_text(
            (self.root / ".env").read_text(encoding="utf-8") + "FAKE_KEY=fake\n",
            encoding="utf-8",
        )

        self.assert_config_error("P0 only supports the ark provider")

    def test_bad_provider_url_is_rejected(self) -> None:
        self.write_env(base_url="https://user:password@ark.example.test/api/v3?debug=1")

        error = self.assert_config_error("without credentials, query, or fragment")
        self.assertNotIn("password", str(error))

    def test_multiple_independent_errors_are_reported_together(self) -> None:
        (self.root / ".env").write_text("ARK_BASE_URL=ftp://invalid\n", encoding="utf-8")
        runtime = self.read_yaml("runtime.yaml")
        runtime["models"]["master"] = "missing-text-model"
        runtime["models"]["image_generation"] = "ark-vlm-primary"
        self.write_yaml("runtime.yaml", runtime)

        error = self.assert_config_error("environment variable ARK_API_KEY is missing")
        rendered = str(error)
        self.assertIn("unknown text model", rendered)
        self.assertIn("expected image_models", rendered)
        self.assertIn("HTTP(S) service root", rendered)

    def test_loader_does_not_create_runtime_or_state_directories(self) -> None:
        load_config_snapshot(self.root, {})

        self.assertFalse((self.root / ".runtime").exists())
        self.assertFalse((self.root / "control-data").exists())
        self.assertFalse((self.root / "workspace").exists())

    def test_config_check_command_is_local_and_zero_cost(self) -> None:
        self.assertEqual(main(["config-check", "--root", str(self.root)]), 0)

        self.assertFalse((self.root / ".runtime").exists())
        self.assertFalse((self.root / "control-data").exists())

    def test_config_check_uses_installed_environment_when_available(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(ROOT / ".test-deps"), str(ROOT / "backend"))
        )
        launcher = DevelopmentLauncher(root=self.root)

        with (
            patch.object(
                DevelopmentLauncher,
                "venv_python",
                new_callable=PropertyMock,
                return_value=Path(sys.executable),
            ),
            patch("scripts.dev.command_succeeds", return_value=True),
            patch.object(
                DevelopmentLauncher,
                "backend_environment",
                return_value=environment,
            ),
        ):
            checked = launcher.config_check()

        self.assertEqual(checked.backend_port, 18080)
        self.assertTrue(checked.revision.startswith("cfg_"))

    def test_startup_fails_before_doctor_or_any_state_initialization(self) -> None:
        (self.root / ".env").unlink()
        launcher = DevelopmentLauncher(root=self.root)

        with self.assertRaises(ConfigCheckFailed):
            launcher.start()

        self.assertFalse((self.root / ".runtime").exists())
        self.assertFalse((self.root / "control-data").exists())
        self.assertFalse((self.root / "workspace").exists())


if __name__ == "__main__":
    unittest.main()
