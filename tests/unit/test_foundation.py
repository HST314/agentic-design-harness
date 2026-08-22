from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from harness.core.config import HarnessSettings, load_settings
from harness.core.logging import JsonFormatter, redact
from harness.runtime import validate_runtime_platform
from harness.services.configuration import SupervisorConfig
from harness.storage.atomic import atomic_write_json, atomic_write_yaml


class FoundationTests(unittest.TestCase):
    def test_supervisor_default_allows_cold_cross_platform_agent_start(self) -> None:
        self.assertEqual(SupervisorConfig().startup_timeout_seconds, 60)

    def test_yaml_and_environment_configuration_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "harness.yaml"
            config.write_text("port: 18100\nlog_level: warning\n", encoding="utf-8")
            settings = load_settings(
                root,
                {
                    "HARNESS_CONFIG": str(config),
                    "HARNESS_PORT": "18101",
                    "HARNESS_WORKSPACE_ROOT": "runtime-workspace",
                },
            )
            self.assertEqual(settings.port, 18101)
            self.assertEqual(settings.log_level, "WARNING")
            self.assertEqual(settings.workspace_root, root / "runtime-workspace")

    def test_atomic_json_and_yaml_replace_with_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            json_path = root / "state.json"
            yaml_path = root / "state.yaml"
            atomic_write_json(json_path, {"revision": 1})
            atomic_write_yaml(yaml_path, {"revision": 2})
            self.assertEqual(
                json.loads(json_path.read_text(encoding="utf-8")), {"revision": 1}
            )
            self.assertEqual(
                yaml.safe_load(yaml_path.read_text(encoding="utf-8")), {"revision": 2}
            )
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(json_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(yaml_path.stat().st_mode), 0o600)

    def test_log_redaction_handles_keys_and_values(self) -> None:
        value = redact(
            {
                "api_key": "not-visible",
                "message": "Author" + "ization: " + "Bear" + "er abcdefghijklmnopqrstuvwxyz",
            }
        )
        self.assertEqual(value["api_key"], "[REDACTED]")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", value["message"])
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "ready", (), None)
        record.fields = {"cookie": "hidden"}
        self.assertNotIn("hidden", JsonFormatter().format(record))

    def test_settings_reject_invalid_log_level(self) -> None:
        with self.assertRaises(ValueError):
            HarnessSettings(log_level="verbose")

    def test_image_agent_path_defaults_to_embedded_without_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            embedded = root / "agents" / "image_agent_mvp"
            external = root / "external_image_agent"
            embedded.mkdir(parents=True)
            external.mkdir()
            selected = load_settings(root, {})
            self.assertEqual(selected.image_agent_path_mode, "embedded_only")
            self.assertEqual(selected.image_agent_root, embedded)

            embedded.rmdir()
            no_fallback = load_settings(root, {})
            self.assertEqual(no_fallback.image_agent_root, embedded)

            external_only = load_settings(
                root,
                {
                    "HARNESS_IMAGE_AGENT_PATH_MODE": "external_only",
                    "HARNESS_IMAGE_AGENT_ROOT": "external_image_agent",
                },
            )
            self.assertEqual(external_only.image_agent_root, external)
            with self.assertRaisesRegex(ValueError, "explicit image_agent_root"):
                load_settings(root, {"HARNESS_IMAGE_AGENT_PATH_MODE": "external_only"})

    def test_delivery_bundle_migration_modes_expose_explicit_write_targets(self) -> None:
        self.assertEqual(HarnessSettings().delivery_bundle_write_targets, (False, True))
        self.assertEqual(
            HarnessSettings(
                delivery_bundle_migration_mode="legacy_only"
            ).delivery_bundle_write_targets,
            (True, False),
        )
        self.assertEqual(
            HarnessSettings(
                delivery_bundle_migration_mode="dual_write"
            ).delivery_bundle_write_targets,
            (True, True),
        )
        self.assertEqual(
            HarnessSettings(
                delivery_bundle_migration_mode="bundle_only"
            ).delivery_bundle_write_targets,
            (False, True),
        )

    def test_runtime_preflight_accepts_supported_host_and_rejects_unknown_kernel(self) -> None:
        validate_runtime_platform()
        with patch("harness.runtime.sys.platform", "darwin"), self.assertRaisesRegex(
            RuntimeError, "supported Linux or Windows kernel"
        ):
            validate_runtime_platform()
