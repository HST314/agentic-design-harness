from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.storage.repository import utc_now
from runtime_helpers import build_config_snapshot

ROOT = Path(__file__).resolve().parents[2]


class GeneralAgentIntegrationTests(unittest.TestCase):
    def test_task_card_start_publishes_chat_with_master_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = build_config_snapshot(
                supervisor_port_start=19300,
                supervisor_port_end=19320,
                supervisor_startup_timeout=10,
            )
            app = create_app(
                HarnessSettings(
                    control_root=root / "control-data",
                    workspace_root=root / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    general_agent_root=ROOT / "agents" / "general-agent",
                    config_snapshot=snapshot,
                )
            )
            with TestClient(app) as client:
                task_id = "task_general_flow"
                instance_id = "instance_general_flow"
                created = client.post(
                    "/api/v1/tasks",
                    json={
                        "task_id": task_id,
                        "title": "General flow",
                        "goal": "Run a managed general task.",
                        "master_owner": "master_default",
                        "start_policy": "manual",
                        "input_manifest": "inputs/manifests/input.json",
                        "envelope": self._envelope("create-general-flow", 0),
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                imported = app.state.container.assets.import_bytes(
                    task_id,
                    filename="本地说明.md",
                    content="# 中文说明\n请使用克制的版式。\n".encode("gb18030"),
                    description="中文本地任务资料",
                    source="user_upload",
                    idempotency_key="general-gb18030-input",
                )
                app.state.container.assets.select_inputs(
                    task_id, [imported["asset_id"]], manifest_id="selected_general"
                )
                app.state.container.asset_understanding.prepare(task_id, [imported["asset_id"]])
                planned = client.put(
                    f"/api/v1/tasks/{task_id}/plan",
                    json={
                        "stages": [
                            {
                                "stage_id": "stage_general_flow",
                                "task_id": task_id,
                                "type": "general",
                                "position": 1,
                                "depends_on": [],
                                "required": True,
                                "instance_ids": [instance_id],
                            }
                        ],
                        "instances": [
                            {
                                "instance_id": instance_id,
                                "task_id": task_id,
                                "stage_id": "stage_general_flow",
                                "agent_type": "general",
                                "required": True,
                                "approval_mode": "human",
                                "config_revision": 1,
                                "workspace_relpath": f"instances/{instance_id}",
                                "task_card_relpath": f"instances/{instance_id}/task-card.json",
                            }
                        ],
                        "task_cards": [
                            {
                                "schema_version": "1.1",
                                "card_id": "card_general_flow",
                                "revision": 1,
                                "task_id": task_id,
                                "stage_id": "stage_general_flow",
                                "instance_id": instance_id,
                                "agent_type": "general",
                                "objective": "Summarize the files in the shared folder.",
                                "instructions": ["Write results only when requested."],
                                "input_assets": [
                                    {
                                        "asset_id": imported["asset_id"],
                                        "manifest_relpath": (
                                            f"inputs/manifests/{imported['asset_id']}.json"
                                        ),
                                    }
                                ],
                                "expected_deliveries": [
                                    {
                                        "kind": "document",
                                        "role": "summary",
                                        "required": True,
                                        "accepted_mime_types": ["text/markdown"],
                                    }
                                ],
                                "parameters": {},
                                "created_at": utc_now(),
                            }
                        ],
                        "operation_id": "save_general_flow",
                        "envelope": self._envelope("save-general-flow", 1),
                    },
                )
                self.assertEqual(planned.status_code, 200, planned.text)
                try:
                    started = client.post(
                        f"/api/v1/tasks/{task_id}/confirm-start",
                        json={
                            "operation_id": "start_general_flow",
                            "instance_ids": [instance_id],
                            "envelope": self._envelope("start-general-flow", 2),
                        },
                    )
                    self.assertEqual(started.status_code, 200, started.text)
                    operation = started.json()
                    deadline = time.monotonic() + 12
                    while (
                        operation["state"] in {"QUEUED", "RUNNING"}
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.05)
                        operation = client.get(
                            "/api/v1/start-operations/start_general_flow"
                        ).json()
                    self.assertEqual(operation["state"], "COMMITTED", operation)
                    instance = client.get(
                        f"/api/v1/instances/{instance_id}?refresh=false"
                    ).json()["instance"]
                    self.assertEqual(instance["status"], "RUNNING")
                    with urlopen(
                        f"{instance['ui_url']}api/messages", timeout=3
                    ) as response:
                        payload = json.loads(response.read())
                    self.assertIn("running", payload)
                    self.assertEqual(payload["messages"][0]["role"], "user")
                    self.assertIn(
                        "Summarize the files", payload["messages"][0]["content"]
                    )
                    materialized = (
                        root
                        / "workspace"
                        / "tasks"
                        / task_id
                        / "resources"
                        / "shared"
                        / "inputs"
                        / imported["asset_id"]
                        / "本地说明.md"
                    )
                    self.assertEqual(
                        materialized.read_text(encoding="utf-8"),
                        "# 中文说明\n请使用克制的版式。\n",
                    )
                    runtime_card = json.loads(
                        (
                            root
                            / "workspace"
                            / "tasks"
                            / task_id
                            / "instances"
                            / instance_id
                            / "runtime"
                            / "general-task-card.json"
                        ).read_text(encoding="utf-8")
                    )
                    self.assertTrue(
                        any(
                            item["path"].endswith("本地说明.md")
                            for item in runtime_card["materialized_inputs"]
                        )
                    )
                    projection = client.get(f"/api/v1/tasks/{task_id}/work-items").json()
                    self.assertEqual(projection["items"][0]["agent_type"], "general")
                    self.assertTrue(projection["items"][0]["stage"]["available"])
                finally:
                    app.state.container.application.cancel_instance(task_id, instance_id)

    @staticmethod
    def _envelope(key: str, revision: int) -> dict[str, object]:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "operator",
            "expected_revision": revision,
        }


if __name__ == "__main__":
    unittest.main()
