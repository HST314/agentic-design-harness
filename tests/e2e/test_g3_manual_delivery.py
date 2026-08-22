from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from harness.adapters import (
    AdapterCommandResult,
    AdapterObservation,
    AdapterRecoveryResult,
    ValidationResult,
)
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.domain.commands import CommandEnvelope
from harness.storage.repository import utc_now

ROOT = Path(__file__).resolve().parents[2]


class ScriptedG3ImageAdapter:
    agent_type = "image"
    available = True

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.completed = False
        self.advance_count = 0
        self.content = b"\x89PNG\r\n\x1a\ng3-manual-delivery"
        self.sha256 = hashlib.sha256(self.content).hexdigest()

    def validate_task_card(self, _card):
        return ValidationResult(True)

    def prepare(self, _request):
        raise AssertionError("The scripted G3 slice starts at the persisted running boundary.")

    def start(self, instance_id, operation_id):
        return AdapterCommandResult(True, operation_id)

    def stop(self, instance_id, reason, operation_id):
        return AdapterCommandResult(True, operation_id, {"reason": reason})

    def get_status(self, _instance_id):
        if self.completed:
            return AdapterObservation(
                "RUNNING", step_id="completed", details={"completed": True}
            )
        return AdapterObservation(
            "WAITING_APPROVAL",
            step_id="waiting_human_approval",
            capabilities=("approve_taskbook",),
            details={
                "job_id": "job_g3_manual",
                "approval_context": {"taskbook": "G3 frozen review"},
            },
        )

    def request_advance(self, instance_id, action, _payload, operation_id):
        if action != "approve_taskbook":
            raise AssertionError("unexpected G3 action")
        self.advance_count += 1
        output = self.workspace / "tasks" / "t_g3_manual" / "instances" / instance_id
        (output / "outputs").mkdir(parents=True, exist_ok=True)
        (output / "outputs" / "final.png").write_bytes(self.content)
        self.completed = True
        return AdapterCommandResult(True, operation_id, {"job_id": "job_g3_complete"})

    def apply_config(self, instance_id, config, revision, operation_id):
        return AdapterCommandResult(True, operation_id)

    def collect_deliveries(self, instance_id):
        return [
            {
                "source_relative_path": f"instances/{instance_id}/outputs/final.png",
                "kind": "image",
                "role": "final_artwork",
                "description": "Human-approved G3 final artwork",
                "sha256": self.sha256,
            }
        ]

    def collect_usage(self, instance_id, cursor):
        return []

    def get_ui_url(self, instance_id):
        return None

    def recover(self, instance_snapshot):
        return AdapterRecoveryResult(True, instance_snapshot["status"])


class ManualApprovalDeliveryG3Tests(unittest.TestCase):
    def test_human_approval_advances_once_then_publishes_before_task_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            app = create_app(
                HarnessSettings(
                    control_root=runtime / "control-data",
                    workspace_root=runtime / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    delivery_bundle_migration_mode="legacy_only",
                )
            )
            with TestClient(app) as client:
                container = app.state.container
                container.credentials.configure_pool(
                    [
                        {
                            "credential_pair_id": "cred_g3_manual",
                            "provider": "fake",
                            "key_id": "key_g3_manual",
                            "base_url": "https://provider.invalid/v1",
                            "api_key": "not-a-secret-g3-manual",
                            "api_key_env": "FAKE_API_KEY",
                            "base_url_env": "FAKE_BASE_URL",
                            "revision": 1,
                            "enabled": True,
                        }
                    ]
                )
                created = client.post(
                    "/api/v1/tasks",
                    json={
                        "task_id": "t_g3_manual",
                        "title": "G3 manual delivery",
                        "goal": "Complete one manually approved Image delivery.",
                        "master_owner": "master_default",
                        "start_policy": "auto",
                        "input_manifest": "inputs/manifests/g3.json",
                        "envelope": self._envelope("create-g3-manual", 0),
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                imported = container.assets.import_bytes(
                    "t_g3_manual",
                    filename="brief.md",
                    content=b"# G3 approved brief\n",
                    description="Approved source for the G3 acceptance image.",
                    source="g3_acceptance",
                    idempotency_key="import-g3-manual-brief",
                )
                selected = container.assets.select_inputs(
                    "t_g3_manual",
                    [imported["asset_id"]],
                    manifest_id="g3-manual-inputs",
                )
                plan = client.put(
                    "/api/v1/tasks/t_g3_manual/plan",
                    json=self._plan_request(selected["task_card_inputs"]),
                )
                self.assertEqual(plan.status_code, 200, plan.text)
                revision = plan.json()["task_revision"]
                for index, status in enumerate(("STARTING", "RUNNING")):
                    result = container.commands.transition_instance(
                        "t_g3_manual",
                        "i_g3_manual",
                        status,
                        CommandEnvelope.model_validate(
                            self._envelope(f"g3-running-{index}", revision, "adapter")
                        ),
                    )
                    revision = result["task_revision"]

                scripted = ScriptedG3ImageAdapter(runtime / "workspace")
                container.adapters._adapters["image"] = scripted
                waiting = client.get("/api/v1/instances/i_g3_manual")
                self.assertEqual(waiting.status_code, 200, waiting.text)
                self.assertEqual(waiting.json()["instance"]["status"], "WAITING_APPROVAL")
                approval = waiting.json()["pending_approval"]

                resolved = client.post(
                    f"/api/v1/approvals/{approval['approval_id']}/resolve",
                    json={
                        "decision": "APPROVED",
                        "action": "approve_taskbook",
                        "payload": {},
                        "operation_id": "resolve_g3_manual",
                        "envelope": self._envelope(
                            "resolve-g3-manual", approval["store_revision"]
                        ),
                    },
                )
                self.assertEqual(resolved.status_code, 200, resolved.text)
                replay = client.post(
                    f"/api/v1/approvals/{approval['approval_id']}/resolve",
                    json={
                        "decision": "APPROVED",
                        "action": "approve_taskbook",
                        "payload": {},
                        "operation_id": "resolve_g3_manual",
                        "envelope": self._envelope(
                            "resolve-g3-manual", approval["store_revision"]
                        ),
                    },
                )
                self.assertEqual(replay.json(), resolved.json())
                self.assertEqual(scripted.advance_count, 1)

                completed = client.get("/api/v1/instances/i_g3_manual")
                self.assertEqual(completed.status_code, 200, completed.text)
                self.assertEqual(completed.json()["instance"]["status"], "SUCCEEDED")
                task = client.get("/api/v1/tasks/t_g3_manual")
                self.assertEqual(task.json()["task"]["status"], "SUCCEEDED")
                resources = client.get("/api/v1/tasks/t_g3_manual/files?group=shared")
                image = next(
                    item
                    for item in resources.json()["items"]
                    if item["filename"] == "final.png"
                )
                self.assertEqual(image["sha256"], scripted.sha256)
                self.assertEqual(resources.json()["assets"][0]["integrity_status"], "VERIFIED")
                inbox = client.get("/api/v1/inbox?owner=human").json()["items"]
                self.assertEqual(
                    [item["kind"] for item in inbox],
                    ["APPROVAL_REQUIRED", "INSTANCE_SUCCEEDED", "TASK_SUCCEEDED"],
                )
                self.assertEqual(inbox[0]["status"], "HANDLED")

    @classmethod
    def _plan_request(cls, inputs: list[dict[str, str]]) -> dict:
        return {
            "stages": [
                {
                    "stage_id": "s_image",
                    "task_id": "t_g3_manual",
                    "type": "image",
                    "position": 1,
                    "depends_on": [],
                    "required": True,
                    "instance_ids": ["i_g3_manual"],
                }
            ],
            "instances": [
                {
                    "instance_id": "i_g3_manual",
                    "task_id": "t_g3_manual",
                    "stage_id": "s_image",
                    "agent_type": "image",
                    "required": True,
                    "approval_mode": "human",
                    "config_revision": 1,
                    "credential_pair_ref": "pending_assignment",
                    "credential_pair_revision": 1,
                    "workspace_relpath": "instances/i_g3_manual",
                    "task_card_relpath": "instances/i_g3_manual/task-card.json",
                }
            ],
            "task_cards": [
                {
                    "schema_version": "1.1",
                    "card_id": "card_g3_manual",
                    "revision": 1,
                    "task_id": "t_g3_manual",
                    "stage_id": "s_image",
                    "instance_id": "i_g3_manual",
                    "agent_type": "image",
                    "objective": "Create one final image.",
                    "instructions": ["Require an explicit human approval."],
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
                        "usage_context": "G3 acceptance",
                    },
                    "created_at": utc_now(),
                }
            ],
            "providers": {"i_g3_manual": "fake"},
            "operation_id": "save_g3_manual_plan",
            "envelope": cls._envelope("save-g3-manual-plan", 1),
        }

    @staticmethod
    def _envelope(key: str, revision: int, actor_type: str = "human") -> dict[str, object]:
        return {
            "idempotency_key": key,
            "actor_type": actor_type,
            "actor_id": "g3_acceptance",
            "expected_revision": revision,
        }


if __name__ == "__main__":
    unittest.main()
