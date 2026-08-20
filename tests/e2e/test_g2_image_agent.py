from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path
from urllib.request import urlopen

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.storage.repository import utc_now

ROOT = Path(__file__).resolve().parents[2]
IMAGE_AGENT_ROOT = os.getenv("HARNESS_IMAGE_AGENT_ROOT")
IMAGE_AGENT_PYTHON = os.getenv("HARNESS_IMAGE_AGENT_PYTHON")
IMAGE_AGENT_DEPENDENCY_ROOT = os.getenv("HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT")


@unittest.skipUnless(
    IMAGE_AGENT_ROOT and IMAGE_AGENT_PYTHON and IMAGE_AGENT_DEPENDENCY_ROOT,
    "set all HARNESS_IMAGE_AGENT_* runtime paths for the G2 real-Agent gate",
)
class RealImageAgentG2Tests(unittest.TestCase):
    def test_offline_instance_launches_waits_and_opens_its_workbench(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime_root = Path(temporary)
            settings = HarnessSettings(
                control_root=runtime_root / "control-data",
                workspace_root=runtime_root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
                image_agent_root=Path(str(IMAGE_AGENT_ROOT)),
                image_agent_python=Path(str(IMAGE_AGENT_PYTHON)),
                image_agent_dependency_root=Path(str(IMAGE_AGENT_DEPENDENCY_ROOT)),
            )
            app = create_app(settings)
            instance_id = "i_g2_real_image"
            try:
                with TestClient(app) as client:
                    self._configure_credential(app)
                    self._create_task(client)
                    selected = self._import_brief(app)
                    saved = self._save_plan(client, selected)
                    started = client.post(
                        "/api/v1/tasks/t_g2_real_image/confirm-start",
                        json={
                            "operation_id": "start_g2_real_image",
                            "envelope": self._envelope(
                                "start-g2-real-image", saved["task_revision"]
                            ),
                        },
                    )
                    self.assertEqual(started.status_code, 200, started.text)
                    self.assertEqual(len(started.json()["launches"]), 1)

                    deadline = time.monotonic() + 30
                    detail = None
                    while time.monotonic() < deadline:
                        response = client.get(f"/api/v1/instances/{instance_id}")
                        self.assertEqual(response.status_code, 200, response.text)
                        detail = response.json()
                        if detail["instance"]["status"] in {
                            "WAITING_APPROVAL",
                            "FAILED",
                        }:
                            break
                        time.sleep(0.1)
                    assert detail is not None
                    self.assertEqual(detail["instance"]["status"], "WAITING_APPROVAL")
                    self.assertEqual(
                        detail["observation"]["step_id"], "waiting_clarification"
                    )
                    self.assertIn(
                        "answer_clarification", detail["observation"]["capabilities"]
                    )

                    link = client.get(f"/api/v1/instances/{instance_id}/ui-link")
                    self.assertEqual(link.status_code, 200, link.text)
                    ui_url = link.json()["ui_url"]
                    self.assertTrue(ui_url.startswith("http://127.0.0.1:"))
                    with urlopen(ui_url, timeout=5) as workbench:
                        page = workbench.read().decode("utf-8")
                    self.assertIn("Image Agent", page)
            finally:
                if app.state.container.store.instance.get(
                    "t_g2_real_image", instance_id
                ) is not None:
                    with suppress(Exception):
                        app.state.container.application.cancel_instance(
                            "t_g2_real_image", instance_id
                        )
                self._make_tree_removable(runtime_root)

    @staticmethod
    def _configure_credential(app) -> None:
        app.state.container.credentials.configure_pool(
            [
                {
                    "credential_pair_id": "cred_g2_real",
                    "provider": "fake",
                    "key_id": "key_g2_real",
                    "base_url": "https://offline.invalid/v1",
                    "api_key": "not-a-secret-g2-real",
                    "api_key_env": "FAKE_API_KEY",
                    "base_url_env": "FAKE_BASE_URL",
                    "revision": 1,
                    "enabled": True,
                }
            ]
        )

    def _create_task(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/tasks",
            json={
                "task_id": "t_g2_real_image",
                "title": "G2 real Image Agent",
                "goal": "Launch the real Image Agent in offline mode.",
                "master_owner": "master_default",
                "start_policy": "manual",
                "input_manifest": "inputs/manifests/g2.json",
                "envelope": self._envelope("create-g2-real-image", 0),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    @staticmethod
    def _import_brief(app) -> list[dict[str, str]]:
        assets = app.state.container.assets
        imported = assets.import_bytes(
            "t_g2_real_image",
            filename="brief.md",
            content=b"# Offline launch poster\nUse a clear centered subject.\n",
            description="Approved offline launch brief",
            source="g2_acceptance",
            idempotency_key="import-g2-real-brief",
        )
        selected = assets.select_inputs(
            "t_g2_real_image", [imported["asset_id"]], manifest_id="g2-real-inputs"
        )
        return selected["task_card_inputs"]

    def _save_plan(self, client: TestClient, inputs: list[dict[str, str]]) -> dict:
        response = client.put(
            "/api/v1/tasks/t_g2_real_image/plan",
            json={
                "stages": [
                    {
                        "stage_id": "s_image",
                        "task_id": "t_g2_real_image",
                        "type": "image",
                        "position": 1,
                        "depends_on": [],
                        "required": True,
                        "instance_ids": ["i_g2_real_image"],
                    }
                ],
                "instances": [
                    {
                        "instance_id": "i_g2_real_image",
                        "task_id": "t_g2_real_image",
                        "stage_id": "s_image",
                        "agent_type": "image",
                        "required": True,
                        "approval_mode": "human",
                        "config_revision": 1,
                        "credential_pair_ref": "pending_assignment",
                        "credential_pair_revision": 1,
                        "workspace_relpath": "instances/i_g2_real_image",
                        "task_card_relpath": "instances/i_g2_real_image/task-card.json",
                    }
                ],
                "task_cards": [
                    {
                        "schema_version": "1.1",
                        "card_id": "card_g2_real_image",
                        "revision": 1,
                        "task_id": "t_g2_real_image",
                        "stage_id": "s_image",
                        "instance_id": "i_g2_real_image",
                        "agent_type": "image",
                        "objective": "Create one offline launch poster.",
                        "instructions": ["Use only the approved brief."],
                        "input_assets": inputs,
                        "expected_deliveries": [
                            {
                                "kind": "image",
                                "role": "final_artwork",
                                "required": True,
                                "accepted_mime_types": ["image/png"],
                            }
                        ],
                        "parameters": {
                            "variants": 1,
                            "usage_context": "Internal acceptance review",
                        },
                        "created_at": utc_now(),
                    }
                ],
                "providers": {"i_g2_real_image": "fake"},
                "operation_id": "save_g2_real_image_plan",
                "envelope": self._envelope("save-g2-real-image-plan", 1),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def _envelope(key: str, revision: int) -> dict[str, object]:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "g2_acceptance",
            "expected_revision": revision,
        }

    @staticmethod
    def _make_tree_removable(root: Path) -> None:
        for current, directories, files in os.walk(root, topdown=False):
            for filename in files:
                path = Path(current) / filename
                if not path.is_symlink():
                    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            for dirname in directories:
                path = Path(current) / dirname
                if not path.is_symlink():
                    path.chmod(stat.S_IRWXU)
