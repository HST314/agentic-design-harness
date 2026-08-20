from __future__ import annotations

import json
import logging
import stat
import tempfile
import unittest
from pathlib import Path

import yaml
from harness.core.config import HarnessSettings, load_settings
from harness.core.logging import JsonFormatter, redact
from harness.storage.atomic import atomic_write_json, atomic_write_yaml


class FoundationTests(unittest.TestCase):
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
            self.assertEqual(json.loads(json_path.read_text()), {"revision": 1})
            self.assertEqual(yaml.safe_load(yaml_path.read_text()), {"revision": 2})
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
