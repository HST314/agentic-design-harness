from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.core.config_kernel import load_config_snapshot

ROOT = Path(__file__).resolve().parents[2]


class SystemSettingsTests(unittest.TestCase):
    def test_preview_publish_and_conflict_are_secret_free_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_root = root / "config"
            config_root.mkdir()
            for filename in (
                "provider.yaml",
                "model_list.yaml",
                "runtime.yaml",
                "image_agent_runtime.yaml",
            ):
                shutil.copyfile(ROOT / "config" / filename, config_root / filename)
            (root / ".env").write_text(
                "ARK_API_KEY=system-settings-test-secret\n"
                "ARK_BASE_URL=https://ark.example.test/api/v3\n",
                encoding="utf-8",
            )
            lock_path = root / "agents" / "image-agent.lock.json"
            lock_path.parent.mkdir()
            shutil.copyfile(ROOT / "agents" / "image-agent.lock.json", lock_path)
            snapshot = load_config_snapshot(root)
            settings = HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
                image_agent_root=ROOT / "agents" / "image_agent_mvp",
                image_agent_lock_path=lock_path,
                image_agent_dependency_root=root / "missing-image-dependencies",
                config_snapshot=snapshot,
            )
            app = create_app(settings)

            with TestClient(app) as client:
                current = client.get("/api/v1/system-settings")
                self.assertEqual(current.status_code, 200, current.text)
                body = current.json()
                self.assertNotIn("system-settings-test-secret", current.text)
                self.assertEqual(
                    body["image_agent_settings"]["category_constraint"]["release"],
                    "off",
                )
                changed = dict(body["image_agent_settings"])
                changed["candidate_concurrency"] = 4
                request = {
                    "base_revision": body["revision"],
                    "harness_settings": body["harness_settings"],
                    "image_agent_settings": changed,
                }
                preview = client.post(
                    "/api/v1/system-settings/preview", json=request
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                preview_body = preview.json()
                self.assertEqual(
                    [change["field"] for change in preview_body["changes"]],
                    ["image_agent_settings.candidate_concurrency"],
                )

                published = client.post(
                    "/api/v1/system-settings/publish",
                    json={
                        **request,
                        "preview_id": preview_body["preview_id"],
                        "actor_id": "settings_test_operator",
                    },
                )
                self.assertEqual(published.status_code, 200, published.text)
                self.assertEqual(published.json()["status"], "PUBLISHED")
                self.assertEqual(published.json()["distribution"]["updated"], 0)
                saved = yaml.safe_load(
                    (config_root / "image_agent_runtime.yaml").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(saved["candidate_concurrency"], 4)

                conflict = client.post(
                    "/api/v1/system-settings/preview", json=request
                )
                self.assertEqual(conflict.status_code, 409, conflict.text)
                self.assertEqual(
                    conflict.json()["error"]["code"], "SETTINGS_REVISION_CONFLICT"
                )
                self.assertIn(
                    "latest_image_agent_settings",
                    conflict.json()["error"]["details"],
                )


if __name__ == "__main__":
    unittest.main()
