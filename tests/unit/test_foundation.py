from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from harness.core.config import HarnessSettings, load_settings
from harness.core.logging import JsonFormatter, redact
from harness.runtime import validate_runtime_platform
from harness.storage.atomic import atomic_write_json, atomic_write_yaml


class FoundationTests(unittest.TestCase):
    def test_process_settings_are_derived_from_root_configuration_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository_root = Path(__file__).resolve().parents[2]
            for filename in ("provider.yaml", "model_list.yaml", "runtime.yaml"):
                shutil.copyfile(repository_root / filename, root / filename)
            (root / ".env").write_text(
                "ARK_API_KEY=test-secret\n"
                "ARK_BASE_URL=https://ark.example.test/api/v3\n",
                encoding="utf-8",
            )
            runtime = yaml.safe_load((root / "runtime.yaml").read_text(encoding="utf-8"))
            runtime["server"]["port"] = 19001
            runtime["server"]["log_level"] = "WARNING"
            (root / "runtime.yaml").write_text(
                yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            settings = load_settings(
                root,
                {
                    "HARNESS_PORT": "19999",
                    "HARNESS_WORKSPACE_ROOT": "ignored-workspace",
                },
            )
            self.assertEqual(settings.port, 19001)
            self.assertEqual(settings.log_level, "WARNING")
            self.assertEqual(settings.workspace_root, root / "workspace")

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

    def test_image_agent_path_resolves_from_the_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            embedded = root / "agents" / "image_agent_mvp"
            embedded.mkdir(parents=True)
            selected = HarnessSettings().resolve_from(root)
            self.assertEqual(selected.image_agent_root, embedded)

    def test_runtime_preflight_accepts_supported_host_and_rejects_unknown_kernel(self) -> None:
        validate_runtime_platform()
        with patch("harness.runtime.sys.platform", "darwin"), self.assertRaisesRegex(
            RuntimeError, "supported Linux or Windows kernel"
        ):
            validate_runtime_platform()
