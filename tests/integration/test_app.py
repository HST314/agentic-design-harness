from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.domain.commands import CommandEnvelope
from harness.storage.repository import utc_now

ROOT = Path(__file__).resolve().parents[2]


class ApplicationTests(unittest.TestCase):
    def test_invalid_image_environment_degrades_only_the_image_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            drifted_dependencies = root / "drifted-dependencies"
            drifted_dependencies.mkdir()
            app = create_app(
                HarnessSettings(
                    control_root=root / "control-data",
                    workspace_root=root / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    image_agent_dependency_root=drifted_dependencies,
                )
            )

            with TestClient(app) as client:
                readiness = client.get("/readyz")
                self.assertEqual(readiness.status_code, 200)
                self.assertEqual(
                    readiness.json(),
                    {"status": "degraded", "disabled_adapters": ["image"]},
                )
                self.assertTrue(app.state.container.store.writer_lease.acquired)
                self.assertEqual(
                    client.get("/api/v1/adapters").json()["items"],
                    [
                        {"agent_type": "general", "available": True},
                        {"agent_type": "image", "available": False},
                        {"agent_type": "ppt", "available": True},
                    ],
                )
                validation = client.post(
                    "/api/v1/contracts/main-task/validate", json={"payload": {}}
                )
                self.assertEqual(validation.status_code, 422)
            self.assertFalse(app.state.container.store.writer_lease.acquired)

    def test_invalid_ppt_environment_degrades_only_the_ppt_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            drifted_dependencies = root / "drifted-ppt-dependencies"
            drifted_dependencies.mkdir()
            app = create_app(
                HarnessSettings(
                    control_root=root / "control-data",
                    workspace_root=root / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    ppt_agent_dependency_root=drifted_dependencies,
                )
            )

            with TestClient(app) as client:
                readiness = client.get("/readyz")
                self.assertEqual(readiness.status_code, 200)
                self.assertEqual(
                    readiness.json(),
                    {"status": "degraded", "disabled_adapters": ["ppt"]},
                )
                self.assertEqual(
                    client.get("/api/v1/adapters").json()["items"],
                    [
                        {"agent_type": "general", "available": True},
                        {"agent_type": "image", "available": True},
                        {"agent_type": "ppt", "available": False},
                    ],
                )
                validation = client.post(
                    "/api/v1/contracts/main-task/validate", json={"payload": {}}
                )
                self.assertEqual(validation.status_code, 422)
            self.assertFalse(app.state.container.store.writer_lease.acquired)

    def test_ppt_artifact_io_failure_keeps_the_control_plane_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "harness.api.app.PptAgentAdapter",
                side_effect=OSError("simulated artifact failure"),
            ):
                app = create_app(
                    HarnessSettings(
                        control_root=root / "control-data",
                        workspace_root=root / "workspace",
                        contracts_root=ROOT / "contracts" / "v1",
                    )
                )

            with TestClient(app) as client:
                self.assertEqual(
                    client.get("/readyz").json(),
                    {"status": "degraded", "disabled_adapters": ["ppt"]},
                )
                self.assertEqual(client.get("/healthz").json()["status"], "ok")

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
                openapi = client.get("/openapi.json").json()
                create_schema = openapi["components"]["schemas"]["CreateTaskRequest"]
                self.assertTrue(create_schema["examples"])
                self.assertIn("/api/v1/tasks/{task_id}/events", openapi["paths"])
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
                        {"agent_type": "general", "available": True},
                        {"agent_type": "image", "available": True},
                        {"agent_type": "ppt", "available": True},
                    ],
                )

                current_revision = plan.json()["task_revision"]
                marked_finished = client.post(
                    "/api/v1/instances/i_api_g2/manual-finished",
                    json={
                        "envelope": self._envelope(
                            "api-mark-manual-finished", current_revision
                        )
                    },
                )
                self.assertEqual(marked_finished.status_code, 200, marked_finished.text)
                self.assertTrue(
                    marked_finished.json()["plan"]["instances"][0]["manual_finished"]
                )
                current_revision = marked_finished.json()["task_revision"]
                marked_in_progress = client.post(
                    "/api/v1/instances/i_api_g2/manual-in-progress",
                    json={
                        "envelope": self._envelope(
                            "api-mark-manual-in-progress", current_revision
                        )
                    },
                )
                self.assertEqual(
                    marked_in_progress.status_code, 200, marked_in_progress.text
                )
                self.assertFalse(
                    marked_in_progress.json()["plan"]["instances"][0]["manual_finished"]
                )
                current_revision = marked_in_progress.json()["task_revision"]
                for index, status in enumerate(("STARTING", "RUNNING", "WAITING_APPROVAL")):
                    transition = container.commands.transition_instance(
                        "t_api_g2",
                        "i_api_g2",
                        status,
                        CommandEnvelope.model_validate(
                            self._envelope(f"api-g3-transition-{index}", current_revision)
                        ),
                    )
                    current_revision = transition["task_revision"]
                    if status == "STARTING":
                        starting = client.get("/api/v1/instances/i_api_g2")
                        self.assertEqual(starting.status_code, 200, starting.text)
                        self.assertEqual(starting.json()["instance"]["status"], "STARTING")
                        self.assertIsNone(starting.json()["observation"])
                    if status == "RUNNING":
                        operation = {
                            "operation_id": "start_api_g2",
                            "state": "RUNNING",
                            "instance_progress": {
                                "i_api_g2": {"state": "AGENT_STARTING"}
                            },
                            "retry_allowed": False,
                        }
                        with patch.object(
                            container.application,
                            "latest_start_operation",
                            return_value=operation,
                        ):
                            agent_starting = client.get("/api/v1/instances/i_api_g2")
                        self.assertEqual(agent_starting.status_code, 200, agent_starting.text)
                        self.assertEqual(agent_starting.json()["instance"]["status"], "RUNNING")
                        self.assertIsNone(agent_starting.json()["observation"])
                        self.assertEqual(
                            agent_starting.json()["start_operation_id"], "start_api_g2"
                        )
                        self.assertEqual(
                            agent_starting.json()["start_progress"],
                            {"state": "AGENT_STARTING"},
                        )
                        self.assertTrue(agent_starting.json()["start_in_progress"])
                        self.assertFalse(agent_starting.json()["start_retry_allowed"])
                created_approval = container.approvals.ensure_workflow_approval(
                    "t_api_g2",
                    "i_api_g2",
                    step_id="waiting_human_approval",
                    capabilities=["approve_taskbook"],
                    context={"taskbook": "review"},
                    operation_id="job_api_g3",
                )
                approval_id = created_approval["approval"]["approval_id"]

                mode = client.put(
                    "/api/v1/instances/i_api_g2/approval-mode",
                    json={
                        "approval_mode": "master",
                        "envelope": self._envelope("api-g3-mode", current_revision),
                    },
                )
                self.assertEqual(mode.status_code, 200, mode.text)
                approval = client.get(f"/api/v1/approvals/{approval_id}")
                self.assertEqual(approval.status_code, 200, approval.text)
                self.assertEqual(approval.json()["approval"]["owner"], "human")
                waiting = client.get("/api/v1/instances/i_api_g2?refresh=false")
                self.assertEqual(waiting.json()["pending_approval"]["approval_id"], approval_id)

                inbox = client.get("/api/v1/inbox?owner=human")
                self.assertEqual(inbox.status_code, 200, inbox.text)
                item = inbox.json()["items"][0]
                marked_read = client.post(
                    f"/api/v1/inbox/{item['inbox_id']}/status",
                    json={
                        "status": "READ",
                        "envelope": self._envelope("api-g3-read", item["store_revision"]),
                    },
                )
                self.assertEqual(marked_read.json()["item"]["status"], "READ")
                resolved = client.post(
                    f"/api/v1/approvals/{approval_id}/resolve",
                    json={
                        "decision": "REJECTED",
                        "action": None,
                        "payload": {},
                        "operation_id": "reject_api_g3",
                        "envelope": self._envelope(
                            "api-g3-reject", approval.json()["approval_revision"]
                        ),
                    },
                )
                self.assertEqual(resolved.status_code, 200, resolved.text)
                self.assertEqual(resolved.json()["approval"]["status"], "REJECTED")
                handled_items = client.get("/api/v1/inbox?owner=human").json()["items"]
                approval_item = next(
                    entry for entry in handled_items if entry["approval_id"] == approval_id
                )
                self.assertEqual(approval_item["status"], "HANDLED")

                files = client.get("/api/v1/tasks/t_api_g2/files?group=inputs")
                self.assertEqual(files.status_code, 200, files.text)
                brief = next(
                    entry for entry in files.json()["items"] if entry["filename"] == "brief.md"
                )
                preview = client.get(
                    "/api/v1/tasks/t_api_g2/files/preview",
                    params={"path": brief["relative_path"]},
                )
                self.assertEqual(preview.text, "# API brief\n")
                download = client.get(
                    "/api/v1/tasks/t_api_g2/files/download",
                    params={"path": brief["relative_path"]},
                )
                self.assertEqual(download.content, b"# API brief\n")
                self.assertEqual(download.headers["x-content-sha256"], brief["sha256"])

                current_revision = container.store.task.revision("t_api_g2", "t_api_g2")
                imported_via_api = client.post(
                    "/api/v1/tasks/t_api_g2/assets",
                    json={
                        "filename": "api-note.txt",
                        "content_base64": base64.b64encode(b"controlled API import").decode(),
                        "description": "Imported through the versioned API",
                        "operation_id": "import_api_note",
                        "envelope": self._envelope("import-api-note", current_revision),
                    },
                )
                self.assertEqual(imported_via_api.status_code, 200, imported_via_api.text)
                self.assertTrue(
                    imported_via_api.json()["manifest"]["relative_path"].endswith("/api-note.txt")
                )
                invalid_import = client.post(
                    "/api/v1/tasks/t_api_g2/assets",
                    json={
                        "filename": "invalid.txt",
                        "content_base64": "%%%not-base64%%%",
                        "description": "invalid",
                        "operation_id": "import_invalid_note",
                        "envelope": self._envelope("import-invalid-note", current_revision),
                    },
                )
                self.assertEqual(invalid_import.status_code, 422)
                self.assertEqual(invalid_import.json()["error"]["code"], "ASSET_VALIDATION_FAILED")

                task_approvals = client.get("/api/v1/tasks/t_api_g2/approvals")
                self.assertEqual(task_approvals.status_code, 200, task_approvals.text)
                self.assertEqual(task_approvals.json()["items"][0]["approval_id"], approval_id)
                events = client.get("/api/v1/tasks/t_api_g2/events")
                self.assertEqual(events.status_code, 200, events.text)
                self.assertTrue(events.json()["items"])
                serialized_events = events.text.lower()
                for forbidden in ("snapshot", "idempotency_key", "api_key", "command_result"):
                    self.assertNotIn(forbidden, serialized_events)
                self.assertEqual(
                    client.get("/api/v1/tasks?cursor=not-a-cursor").json()["error"]["code"],
                    "VALIDATION_ERROR",
                )

                instance_detail = client.get("/api/v1/instances/i_api_g2?refresh=false")
                self.assertNotIn("credential", instance_detail.json())
                self.assertNotIn("config", instance_detail.json())
                self.assertNotIn("not-a-secret-api-g2", instance_detail.text)

                created_second = client.post(
                    "/api/v1/tasks",
                    json={
                        "task_id": "t_api_g5_page",
                        "title": "Pagination task",
                        "goal": "Prove stable keyset pagination.",
                        "master_owner": "master_default",
                        "start_policy": "manual",
                        "input_manifest": "inputs/manifests/input.json",
                        "envelope": self._envelope("create-api-g5-page", 0),
                    },
                )
                self.assertEqual(created_second.status_code, 200, created_second.text)
                first_page = client.get("/api/v1/tasks?limit=1&order=asc").json()
                self.assertTrue(first_page["page"]["has_more"])
                second_page = client.get(
                    "/api/v1/tasks",
                    params={
                        "limit": 1,
                        "order": "asc",
                        "cursor": first_page["page"]["next_cursor"],
                    },
                ).json()
                self.assertNotEqual(
                    first_page["items"][0]["task_id"], second_page["items"][0]["task_id"]
                )

                current_revision = container.store.task.revision("t_api_g2", "t_api_g2")
                cancelled = client.post(
                    "/api/v1/tasks/t_api_g2/cancel",
                    json={
                        "operation_id": "cancel_api_g5_task",
                        "envelope": self._envelope("cancel-api-g5-task", current_revision),
                    },
                )
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                self.assertEqual(cancelled.json()["task"]["status"], "CANCELLED")
                cancelled_replay = client.post(
                    "/api/v1/tasks/t_api_g2/cancel",
                    json={
                        "operation_id": "cancel_api_g5_task",
                        "envelope": self._envelope("cancel-api-g5-task", current_revision),
                    },
                )
                self.assertEqual(cancelled_replay.status_code, 200, cancelled_replay.text)
                self.assertEqual(cancelled_replay.json(), cancelled.json())
                archived = client.post(
                    "/api/v1/instances/i_api_g2/archive",
                    json={
                        "operation_id": "archive_api_g5_instance",
                        "envelope": self._envelope(
                            "archive-api-g5-instance", cancelled.json()["task_revision"]
                        ),
                    },
                )
                self.assertEqual(archived.status_code, 200, archived.text)
                self.assertEqual(archived.json()["instance"]["status"], "ARCHIVED")
                archived_replay = client.post(
                    "/api/v1/instances/i_api_g2/archive",
                    json={
                        "operation_id": "archive_api_g5_instance",
                        "envelope": self._envelope(
                            "archive-api-g5-instance", cancelled.json()["task_revision"]
                        ),
                    },
                )
                self.assertEqual(archived_replay.status_code, 200, archived_replay.text)
                self.assertEqual(archived_replay.json(), archived.json())

    def test_save_plan_http_carries_mode_and_if_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
            )
            app = create_app(settings)
            with TestClient(app) as client:
                container = app.state.container
                created = client.post(
                    "/api/v1/tasks",
                    json={
                        "task_id": "t_api_mode",
                        "title": "Plan mode API task",
                        "goal": "Prove save_plan mode and If-Match plumbing.",
                        "master_owner": "master_default",
                        "start_policy": "manual",
                        "input_manifest": "inputs/manifests/input.json",
                        "envelope": self._envelope("create-api-mode", 0),
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)

                def plan_payload(suffix: str, position: int) -> dict[str, object]:
                    return {
                        "stages": [
                            {
                                "stage_id": f"s_image_{suffix}",
                                "task_id": "t_api_mode",
                                "type": "image",
                                "position": position,
                                "depends_on": [],
                                "required": True,
                                "instance_ids": [f"i_mode_{suffix}"],
                            }
                        ],
                        "instances": [
                            {
                                "instance_id": f"i_mode_{suffix}",
                                "task_id": "t_api_mode",
                                "stage_id": f"s_image_{suffix}",
                                "agent_type": "image",
                                "required": True,
                                "approval_mode": "human",
                                "config_revision": 1,
                                "workspace_relpath": f"instances/i_mode_{suffix}",
                                "task_card_relpath": f"instances/i_mode_{suffix}/task-card.json",
                            }
                        ],
                        "task_cards": [
                            {
                                "schema_version": "1.1",
                                "card_id": f"card_mode_{suffix}",
                                "revision": 1,
                                "task_id": "t_api_mode",
                                "stage_id": f"s_image_{suffix}",
                                "instance_id": f"i_mode_{suffix}",
                                "agent_type": "image",
                                "objective": f"Create the {suffix} visual.",
                                "instructions": ["Use the verified brief."],
                                "input_assets": [],
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
                                    "usage_context": "API mode review",
                                },
                                "created_at": utc_now(),
                            }
                        ],
                    }

                def put_plan(
                    suffix: str,
                    position: int,
                    key: str,
                    expected: int,
                    *,
                    mode: str | None = None,
                    if_match: int | None = None,
                ):
                    body: dict[str, object] = {
                        **plan_payload(suffix, position),
                        "operation_id": f"save_api_mode_{key}",
                        "envelope": self._envelope(f"save-api-mode-{key}", expected),
                    }
                    if mode is not None:
                        body["mode"] = mode
                    headers = {}
                    if if_match is not None:
                        headers["If-Match"] = str(if_match)
                    return client.put("/api/v1/tasks/t_api_mode/plan", json=body, headers=headers)

                initial_plan_revision = container.store.task.get("t_api_mode", "t_api_mode")[
                    "plan_revision"
                ]
                first = put_plan("one", 1, "first", 1, if_match=initial_plan_revision)
                self.assertEqual(first.status_code, 200, first.text)
                landed_plan_revision = first.json()["task"]["plan_revision"]
                self.assertEqual(landed_plan_revision, initial_plan_revision)

                bad_mode = put_plan(
                    "one", 1, "bad-mode", first.json()["task_revision"], mode="bogus"
                )
                self.assertEqual(bad_mode.status_code, 422, bad_mode.text)

                current_revision = first.json()["task_revision"]
                for index, status in enumerate(("STARTING", "RUNNING")):
                    transition = container.commands.transition_instance(
                        "t_api_mode",
                        "i_mode_one",
                        status,
                        CommandEnvelope.model_validate(
                            self._envelope(f"api-mode-transition-{index}", current_revision)
                        ),
                    )
                    current_revision = transition["task_revision"]
                self.assertEqual(
                    container.store.task.get("t_api_mode", "t_api_mode")["status"], "RUNNING"
                )

                stale_match = put_plan(
                    "two", 2, "stale-match", current_revision, mode="append", if_match=99
                )
                self.assertEqual(stale_match.status_code, 409, stale_match.text)
                self.assertEqual(stale_match.json()["error"]["code"], "REVISION_CONFLICT")

                appended = put_plan(
                    "two",
                    2,
                    "append",
                    current_revision,
                    mode="append",
                    if_match=landed_plan_revision,
                )
                self.assertEqual(appended.status_code, 200, appended.text)
                self.assertEqual(appended.json()["task"]["plan_revision"], landed_plan_revision + 1)
                self.assertEqual(len(appended.json()["plan"]["stages"]), 2)

                replaced = put_plan(
                    "three",
                    1,
                    "replace-running",
                    appended.json()["task_revision"],
                )
                self.assertEqual(replaced.status_code, 409, replaced.text)
                self.assertEqual(replaced.json()["error"]["code"], "INVALID_STATE_TRANSITION")

    @staticmethod
    def _envelope(key: str, expected_revision: int) -> dict[str, object]:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "api_tester",
            "expected_revision": expected_revision,
        }
