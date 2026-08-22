from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.domain.commands import CommandEnvelope
from harness.services.master_gateway import MasterRunObservation

ROOT = Path(__file__).resolve().parents[2]


class RecordingGateway:
    available = True

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.revision = 0

    def submit_message(self, task_id: str, message: dict[str, Any]) -> str:
        self.revision += 1
        run_id = f"run_master_{self.revision}"
        self.runs[run_id] = {"task_id": task_id, "message": deepcopy(message)}
        return run_id

    def observe_run(self, run_id: str) -> MasterRunObservation:
        return MasterRunObservation(
            "PLAN_READY",
            f"计划 r{self._revision(run_id)} 已生成, 请确认。",
            "秋季发布会主视觉",
        )

    def load_plan(self, run_id: str) -> dict[str, Any]:
        run = self.runs[run_id]
        revision = self._revision(run_id)
        task_id = run["task_id"]
        message = run["message"]
        instance_id = f"instance_master_{revision}"
        stage_id = f"stage_image_{revision}"
        card_id = f"card_master_{revision}"
        work_item_id = f"work_master_{revision}"
        created_at = message["created_at"]
        return {
            "schema_version": "1.0",
            "proposal_id": f"proposal_master_{revision}",
            "task_id": task_id,
            "revision": revision,
            "status": "PENDING_CONFIRMATION",
            "stages": [
                {
                    "stage_id": stage_id,
                    "type": "image",
                    "position": 1,
                    "depends_on": [],
                    "required": True,
                }
            ],
            "work_items": [
                {
                    "schema_version": "1.0",
                    "work_item_id": work_item_id,
                    "task_id": task_id,
                    "stage_id": stage_id,
                    "title": f"主视觉方向 {revision}",
                    "agent_type": "image",
                    "required": True,
                    "depends_on": [],
                    "current_instance_id": instance_id,
                    "instance_ids": [instance_id],
                    "task_card_ids": [card_id],
                }
            ],
            "execution_cards": [
                {
                    "schema_version": "1.1",
                    "card_id": card_id,
                    "revision": 1,
                    "task_id": task_id,
                    "stage_id": stage_id,
                    "instance_id": instance_id,
                    "agent_type": "image",
                    "objective": f"生成主视觉方向 {revision}。",
                    "instructions": ["遵守品牌安全区。"],
                    "input_assets": deepcopy(message["asset_refs"]),
                    "expected_deliveries": [
                        {
                            "kind": "image",
                            "role": "key_visual",
                            "required": True,
                            "accepted_mime_types": ["image/png"],
                        }
                    ],
                    "parameters": {"usage_context": "发布会主屏", "variants": 3},
                    "created_at": created_at,
                }
            ],
            "created_at": created_at,
            "updated_at": created_at,
            "confirmed_at": None,
        }

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "cancelled": True}

    @staticmethod
    def _revision(run_id: str) -> int:
        return int(run_id.rsplit("_", 1)[1])


class RecordingApplication:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []
        self.started: list[dict[str, Any]] = []

    def save_plan_and_create_instances(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.saved.append({"task_id": task_id, **deepcopy(kwargs)})
        expected = kwargs["envelope"].expected_revision
        return {"task_revision": expected + 1, "task": {"task_id": task_id}}

    def confirm_and_start_ready_instances(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.started.append({"task_id": task_id, **deepcopy(kwargs)})
        return {"task_id": task_id, "launches": [{"instance_id": "recorded"}], "unavailable": []}


class InterruptedApplication(RecordingApplication):
    def confirm_and_start_ready_instances(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated crash after the plan-save checkpoint")


class EnabledCredentials:
    @staticmethod
    def list_redacted() -> list[dict[str, Any]]:
        return [{"provider": "openai", "enabled": True}]


class MasterThreadApiTests(unittest.TestCase):
    def test_revisioned_thread_adjustment_and_manual_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            gateway = RecordingGateway()
            application = RecordingApplication()
            app.state.container.master_threads.gateway = gateway
            app.state.container.master_threads.application = application
            app.state.container.master_threads.credentials = EnabledCredentials()
            with TestClient(app) as client:
                task_id = self._create_submit(client, "manual")
                first = client.get(f"/api/v1/tasks/{task_id}/master/messages")
                self.assertEqual(first.status_code, 200, first.text)
                first_session = first.json()
                self.assertEqual(first_session["latest_proposal"]["revision"], 1)
                self.assertEqual(first_session["latest_proposal"]["status"], "PENDING_CONFIRMATION")
                self.assertEqual(
                    [item["role"] for item in first_session["messages"]],
                    ["user", "master"],
                )
                self.assertEqual(application.saved, [])

                adjusted = client.post(
                    f"/api/v1/tasks/{task_id}/master/messages",
                    json={
                        "content": "请把方向改得更克制。",
                        "asset_refs": [],
                        "envelope": self._envelope(
                            "adjust-master-plan", first_session["thread_revision"]
                        ),
                    },
                )
                self.assertEqual(adjusted.status_code, 200, adjusted.text)
                adjusted_session = adjusted.json()
                self.assertEqual(adjusted_session["latest_proposal"]["revision"], 2)
                proposals = app.state.container.store.plan_proposal.list(task_id)
                self.assertEqual(
                    {item["revision"]: item["status"] for item in proposals},
                    {1: "SUPERSEDED", 2: "PENDING_CONFIRMATION"},
                )

                stale = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/confirm",
                    json={
                        "task_expected_revision": adjusted_session["task_revision"],
                        "envelope": self._envelope("stale-confirm", 1),
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(stale.json()["error"]["code"], "REVISION_CONFLICT")

                confirmed = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/2/confirm",
                    json={
                        "task_expected_revision": adjusted_session["task_revision"],
                        "envelope": self._envelope("confirm-current", 2),
                    },
                )
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                self.assertEqual(confirmed.json()["proposal"]["status"], "CONFIRMED")
                self.assertEqual(len(application.saved), 1)
                self.assertEqual(len(application.started), 1)
                events = client.get(f"/api/v1/tasks/{task_id}/events?limit=200").json()["items"]
                commands = {item["command"] for item in events}
                self.assertIn("save_plan_proposal", commands)
                self.assertIn("confirm_plan_proposal", commands)
                self.assertIn("append_plan_confirmation_message", commands)

    def test_auto_mode_uses_the_same_gates_and_confirms_without_human_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            application = RecordingApplication()
            app.state.container.master_threads.gateway = RecordingGateway()
            app.state.container.master_threads.application = application
            app.state.container.master_threads.credentials = EnabledCredentials()
            with TestClient(app) as client:
                task_id = self._create_submit(client, "auto")
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                self.assertEqual(session["latest_proposal"]["status"], "CONFIRMED")
                self.assertEqual(len(application.saved), 1)
                self.assertEqual(len(application.started), 1)
                self.assertEqual(application.saved[0]["providers"], {"instance_master_1": "openai"})

    def test_unconfigured_gateway_persists_a_truthful_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            with TestClient(app) as client:
                task_id = self._create_submit(client, "manual")
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                self.assertFalse(session["gateway_available"])
                self.assertEqual(session["thread"]["last_error"]["code"], "MASTER_UNAVAILABLE")
                self.assertIsNone(session["latest_proposal"])
                self.assertEqual([item["role"] for item in session["messages"]], ["user", "system"])

    def test_startup_recovery_resumes_a_partial_master_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            gateway = RecordingGateway()
            app.state.container.master_threads.gateway = gateway
            app.state.container.master_threads.application = InterruptedApplication()
            app.state.container.master_threads.credentials = EnabledCredentials()
            with TestClient(app) as client:
                task_id = self._create_submit(client, "manual")
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                service = app.state.container.master_threads
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    service.confirm_plan(
                        task_id,
                        1,
                        task_expected_revision=session["task_revision"],
                        envelope=CommandEnvelope.model_validate(
                            self._envelope("confirm-before-crash", 1)
                        ),
                    )

                resumed_application = RecordingApplication()
                service.application = resumed_application
                recoveries = service.recover()
                self.assertEqual(
                    recoveries,
                    [{
                        "kind": "confirmation",
                        "task_id": task_id,
                        "proposal_revision": 1,
                        "status": "CONFIRMED",
                    }],
                )
                self.assertEqual(resumed_application.saved, [])
                self.assertEqual(len(resumed_application.started), 1)
                recovered = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                self.assertEqual(recovered["latest_proposal"]["status"], "CONFIRMED")

    def _create_submit(self, client: TestClient, start_policy: str) -> str:
        created = client.post(
            "/api/v1/task-intakes",
            json={
                "prompt": "为秋季发布会制作主视觉。",
                "start_policy": start_policy,
                "envelope": self._envelope(f"create-{start_policy}", 0),
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        task_id = body["task"]["task_id"]
        uploaded = client.post(
            f"/api/v1/task-intakes/{task_id}/assets",
            files={"file": ("brief.md", b"# brief\n", "text/markdown")},
            data={
                "declared_mime_type": "text/markdown",
                "description": "品牌约束",
                "idempotency_key": f"upload-{start_policy}",
                "actor_id": "human_operator",
                "expected_revision": "1",
            },
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        submitted = client.post(
            f"/api/v1/task-intakes/{task_id}/submit",
            json={
                "task_expected_revision": body["task_revision"],
                "envelope": self._envelope(
                    f"submit-{start_policy}", uploaded.json()["intake_revision"]
                ),
            },
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        return task_id

    @staticmethod
    def _app(root: Path):
        return create_app(
            HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
            )
        )

    @staticmethod
    def _envelope(key: str, expected_revision: int) -> dict[str, Any]:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "human_operator",
            "expected_revision": expected_revision,
        }


if __name__ == "__main__":
    unittest.main()
