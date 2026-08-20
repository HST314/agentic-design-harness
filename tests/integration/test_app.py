from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.storage.repository import utc_now

ROOT = Path(__file__).resolve().parents[2]


class ApplicationTests(unittest.TestCase):
    def test_lifecycle_health_readiness_and_contract_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
            )
            app = create_app(settings)
            with TestClient(app) as client:
                health = client.get("/healthz")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()["status"], "ok")
                self.assertEqual(client.get("/readyz").json(), {"status": "ready"})
                invalid = client.post("/api/v1/contracts/main-task/validate", json={"payload": {}})
                self.assertEqual(invalid.status_code, 422)
                self.assertEqual(invalid.json()["error"]["code"], "VALIDATION_ERROR")
                self.assertNotIn("traceback", invalid.text.lower())
            self.assertFalse(app.state.container.store.writer_lease.acquired)

    def test_g2_task_plan_and_instance_read_apis_use_application_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
            )
            app = create_app(settings)
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/tasks",
                    json={
                        "task_id": "t_api_g2",
                        "title": "G2 API task",
                        "goal": "Open one offline Image workspace.",
                        "master_owner": "master_default",
                        "start_policy": "manual",
                        "input_manifest": "inputs/manifests/input.json",
                        "envelope": self._envelope("create-api-g2", 0),
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                self.assertEqual(created.json()["revision"], 1)
                container = app.state.container
                container.credentials.configure_pool(
                    [
                        {
                            "credential_pair_id": "cred_api_g2",
                            "provider": "fake",
                            "key_id": "key_api_g2",
                            "base_url": "https://provider.invalid/v1",
                            "api_key": "not-a-secret-api-g2",
                            "api_key_env": "FAKE_API_KEY",
                            "base_url_env": "FAKE_BASE_URL",
                            "revision": 1,
                            "enabled": True,
                        }
                    ]
                )
                imported = container.assets.import_bytes(
                    "t_api_g2",
                    filename="brief.md",
                    content=b"# API brief\n",
                    description="Verified API brief",
                    source="test",
                    idempotency_key="import-api-g2",
                )
                selected = container.assets.select_inputs(
                    "t_api_g2", [imported["asset_id"]], manifest_id="api-g2-inputs"
                )
                plan = client.put(
                    "/api/v1/tasks/t_api_g2/plan",
                    json={
                        "stages": [
                            {
                                "stage_id": "s_image",
                                "task_id": "t_api_g2",
                                "type": "image",
                                "position": 1,
                                "depends_on": [],
                                "required": True,
                                "instance_ids": ["i_api_g2"],
                            }
                        ],
                        "instances": [
                            {
                                "instance_id": "i_api_g2",
                                "task_id": "t_api_g2",
                                "stage_id": "s_image",
                                "agent_type": "image",
                                "required": True,
                                "approval_mode": "human",
                                "config_revision": 1,
                                "credential_pair_ref": "pending_assignment",
                                "credential_pair_revision": 1,
                                "workspace_relpath": "instances/i_api_g2",
                                "task_card_relpath": "instances/i_api_g2/task-card.json",
                            }
                        ],
                        "task_cards": [
                            {
                                "schema_version": "1.1",
                                "card_id": "card_api_g2",
                                "revision": 1,
                                "task_id": "t_api_g2",
                                "stage_id": "s_image",
                                "instance_id": "i_api_g2",
                                "agent_type": "image",
                                "objective": "Create an API launch poster.",
                                "instructions": ["Use the verified API brief."],
                                "input_assets": selected["task_card_inputs"],
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
                                    "usage_context": "Internal API review",
                                },
                                "created_at": utc_now(),
                            }
                        ],
                        "providers": {"i_api_g2": "fake"},
                        "operation_id": "save_api_g2_plan",
                        "envelope": self._envelope("save-api-g2-plan", 1),
                    },
                )
                self.assertEqual(plan.status_code, 200, plan.text)
                self.assertEqual(plan.json()["task"]["status"], "AWAITING_START_CONFIRMATION")

                listing = client.get("/api/v1/tasks")
                self.assertEqual(listing.status_code, 200)
                self.assertEqual(listing.json()["items"][0]["task_id"], "t_api_g2")
                detail = client.get("/api/v1/tasks/t_api_g2")
                self.assertEqual(detail.json()["plan"]["instances"][0]["instance_id"], "i_api_g2")
                instance = client.get("/api/v1/instances/i_api_g2?refresh=false")
                self.assertEqual(instance.status_code, 200, instance.text)
                self.assertEqual(instance.json()["instance"]["agent_type"], "image")
                adapters = client.get("/api/v1/adapters")
                self.assertEqual(
                    adapters.json()["items"],
                    [
                        {"agent_type": "image", "available": True},
                        {"agent_type": "ppt", "available": False},
                    ],
                )

    @staticmethod
    def _envelope(key: str, expected_revision: int) -> dict[str, object]:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "api_tester",
            "expected_revision": expected_revision,
        }
