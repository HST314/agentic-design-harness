from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.domain.commands import CommandEnvelope
from harness.services.work_item_projections import logical_work_items
from harness.storage.repository import Actor, utc_now

ROOT = Path(__file__).resolve().parents[2]


class WorkItemProjectionApiTests(unittest.TestCase):
    def test_append_proposals_preserve_work_items_from_prior_revisions(self) -> None:
        proposals = [
            {
                "status": "CONFIRMED",
                "revision": revision,
                "updated_at": f"2026-08-30T00:00:0{revision}Z",
                "work_items": [
                    {
                        "work_item_id": f"work_{revision}",
                        "stage_id": f"stage_{revision}",
                    }
                ],
            }
            for revision in (1, 2)
        ]
        store = SimpleNamespace(
            plan_proposal=SimpleNamespace(list=lambda _task_id: proposals)
        )

        items = logical_work_items(
            store,
            "task_append_projection",
            {"task": {"plan_revision": 2}, "task_cards": [], "instances": []},
            [{"stage_id": "stage_1"}, {"stage_id": "stage_2"}],
        )

        self.assertEqual([item["work_item_id"] for item in items], ["work_1", "work_2"])

    def test_active_instance_wins_over_parallel_approval_and_etag_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            with TestClient(app) as client:
                task_id = "task_board_projection"
                self._create_task(client, task_id)
                container = app.state.container
                imported = container.assets.import_bytes(
                    task_id,
                    filename="brief.md",
                    content=b"# Board projection brief\n",
                    description="Verified board input",
                    source="test",
                    idempotency_key="import-board-projection",
                )
                selected = container.assets.select_inputs(
                    task_id,
                    [imported["asset_id"]],
                    manifest_id="board-projection-inputs",
                )
                saved = self._save_image_plan(
                    client,
                    task_id,
                    selected["task_card_inputs"],
                )
                proposal_time = saved["task"]["updated_at"]
                container.store.plan_proposal.put(
                    task_id,
                    "proposal_board_projection",
                    {
                        "schema_version": "1.0",
                        "proposal_id": "proposal_board_projection",
                        "task_id": task_id,
                        "revision": saved["task"]["plan_revision"],
                        "status": "CONFIRMED",
                        "stages": [
                            {
                                "stage_id": "stage_board_image",
                                "type": "image",
                                "position": 1,
                                "depends_on": [],
                                "required": True,
                            }
                        ],
                        "work_items": [
                            {
                                "schema_version": "1.0",
                                "work_item_id": "work_board_direction",
                                "task_id": task_id,
                                "stage_id": "stage_board_image",
                                "title": "发布会主视觉方向",
                                "agent_type": "image",
                                "required": True,
                                "depends_on": [],
                                "current_instance_id": "instance_board_approval",
                                "instance_ids": [
                                    "instance_board_running",
                                    "instance_board_approval",
                                ],
                                "task_card_ids": [
                                    "card_board_running",
                                    "card_board_approval",
                                ],
                            }
                        ],
                        "execution_cards": saved["plan"]["task_cards"],
                        "created_at": proposal_time,
                        "updated_at": proposal_time,
                        "confirmed_at": proposal_time,
                    },
                    expected_revision=0,
                    actor=Actor("master", "master_default"),
                    command="test_confirm_projection",
                    idempotency_key="test-confirm-projection",
                )
                task_revision = saved["task_revision"]
                for index, (instance_id, statuses) in enumerate(
                    (
                        ("instance_board_running", ("STARTING", "RUNNING")),
                        (
                            "instance_board_approval",
                            ("STARTING", "RUNNING", "WAITING_APPROVAL"),
                        ),
                    )
                ):
                    for status_index, status in enumerate(statuses):
                        transitioned = container.commands.transition_instance(
                            task_id,
                            instance_id,
                            status,
                            CommandEnvelope.model_validate(
                                self._envelope(
                                    f"board-transition-{index}-{status_index}",
                                    task_revision,
                                )
                            ),
                        )
                        task_revision = transitioned["task_revision"]
                container.approvals.ensure_workflow_approval(
                    task_id,
                    "instance_board_approval",
                    step_id="select_visual_direction",
                    capabilities=["approve_taskbook"],
                    context={"direction": "A"},
                    operation_id="job_board_approval",
                )

                response = client.get(f"/api/v1/tasks/{task_id}/work-items")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["cache-control"], "no-cache")
                payload = response.json()
                self.assertEqual(payload["summary"]["RUNNING"], 1)
                self.assertEqual(payload["refresh_after_ms"], 3000)
                self.assertEqual(len(payload["items"]), 1)
                item = payload["items"][0]
                self.assertEqual(item["work_item_id"], "work_board_direction")
                self.assertEqual(item["business_status"], "RUNNING")
                self.assertEqual(item["raw_status"], "RUNNING")
                self.assertEqual(item["current_instance"]["status"], "WAITING_APPROVAL")
                self.assertFalse(item["current_instance"]["manual_finished"])
                self.assertEqual(len(item["pending_approvals"]), 1)

                unchanged = client.get(
                    f"/api/v1/tasks/{task_id}/work-items",
                    headers={"If-None-Match": response.headers["etag"]},
                )
                self.assertEqual(unchanged.status_code, 304, unchanged.text)
                detail = client.get(
                    f"/api/v1/tasks/{task_id}/work-items/work_board_direction"
                )
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json()["item"], item)

                completed = container.commands.transition_instance(
                    task_id,
                    "instance_board_running",
                    "SUCCEEDED",
                    CommandEnvelope.model_validate(
                        self._envelope("board-running-complete", task_revision)
                    ),
                )
                self.assertGreater(completed["task_revision"], task_revision)
                waiting = client.get(f"/api/v1/tasks/{task_id}/work-items").json()
                self.assertEqual(waiting["items"][0]["business_status"], "WAITING_APPROVAL")
                self.assertEqual(waiting["refresh_after_ms"], 5000)

    def test_ppt_plan_projects_available_runtime_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            with TestClient(app) as client:
                task_id = "task_board_ppt"
                self._create_task(client, task_id)
                created_at = utc_now()
                response = client.put(
                    f"/api/v1/tasks/{task_id}/plan",
                    json={
                        "stages": [
                            {
                                "stage_id": "stage_board_ppt",
                                "task_id": task_id,
                                "type": "ppt",
                                "position": 1,
                                "depends_on": [],
                                "required": True,
                                "instance_ids": ["instance_board_ppt"],
                            }
                        ],
                        "instances": [
                            {
                                "instance_id": "instance_board_ppt",
                                "task_id": task_id,
                                "stage_id": "stage_board_ppt",
                                "agent_type": "ppt",
                                "required": True,
                                "approval_mode": "human",
                                "config_revision": 1,
                                "workspace_relpath": "instances/instance_board_ppt",
                                "task_card_relpath": "instances/instance_board_ppt/task-card.json",
                            }
                        ],
                        "task_cards": [
                            {
                                "schema_version": "1.1",
                                "card_id": "card_board_ppt",
                                "revision": 1,
                                "task_id": task_id,
                                "stage_id": "stage_board_ppt",
                                "instance_id": "instance_board_ppt",
                                "agent_type": "ppt",
                                "objective": "整合已确认视觉资源并制作演示文稿。",
                                "instructions": ["使用已确认视觉资源。"],
                                "input_assets": [],
                                "expected_deliveries": [
                                    {
                                        "kind": "presentation",
                                        "role": "final_deck",
                                        "required": True,
                                        "accepted_mime_types": [
                                            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                                        ],
                                    }
                                ],
                                "parameters": {"slide_count": 12},
                                "created_at": created_at,
                            }
                        ],
                        "operation_id": "save_board_ppt_plan",
                        "envelope": self._envelope("save-board-ppt-plan", 1),
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                projection = client.get(f"/api/v1/tasks/{task_id}/work-items").json()
                self.assertTrue(projection["stages"][0]["available"])
                self.assertEqual(projection["items"][0]["business_status"], "TODO")
                self.assertEqual(projection["items"][0]["alerts"], [])

                updated = client.patch(
                    f"/api/v1/tasks/{task_id}/work-items/work_{self._work_id('card_board_ppt')}/status",
                    json={
                        "business_status": "COMPLETED",
                        "envelope": self._envelope(
                            "complete-board-ppt",
                            projection["task_revision"],
                        ),
                    },
                )
                self.assertEqual(updated.status_code, 200, updated.text)
                updated_projection = updated.json()
                self.assertEqual(
                    updated_projection["items"][0]["business_status"], "COMPLETED"
                )
                self.assertEqual(updated_projection["summary"]["COMPLETED"], 1)
                stored = app.state.container.store.plan.get(task_id, task_id)
                self.assertEqual(
                    stored["instances"][0]["manual_business_status"], "COMPLETED"
                )
                self.assertFalse(stored["instances"][0]["manual_finished"])

    @staticmethod
    def _app(root: Path):
        return create_app(
            HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
            )
        )

    def _create_task(self, client: TestClient, task_id: str) -> None:
        response = client.post(
            "/api/v1/tasks",
            json={
                "task_id": task_id,
                "title": "F3 看板投影任务",
                "goal": "验证逻辑子任务看板和计划依赖。",
                "master_owner": "master_default",
                "start_policy": "manual",
                "input_manifest": "inputs/manifests/input.json",
                "envelope": self._envelope(f"create-{task_id}", 0),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    @staticmethod
    def _work_id(card_id: str) -> str:
        return hashlib.sha256(card_id.encode("utf-8")).hexdigest()[:24]

    def _save_image_plan(
        self,
        client: TestClient,
        task_id: str,
        input_assets: list[dict],
    ) -> dict:
        created_at = utc_now()
        stages = [
            {
                "stage_id": "stage_board_image",
                "task_id": task_id,
                "type": "image",
                "position": 1,
                "depends_on": [],
                "required": True,
                "instance_ids": [
                    "instance_board_running",
                    "instance_board_approval",
                ],
            }
        ]
        instances = [
            {
                "instance_id": instance_id,
                "task_id": task_id,
                "stage_id": "stage_board_image",
                "agent_type": "image",
                "required": True,
                "approval_mode": "human",
                "config_revision": 1,
                "workspace_relpath": f"instances/{instance_id}",
                "task_card_relpath": f"instances/{instance_id}/task-card.json",
            }
            for instance_id in ("instance_board_running", "instance_board_approval")
        ]
        cards = [
            {
                "schema_version": "1.1",
                "card_id": card_id,
                "revision": 1,
                "task_id": task_id,
                "stage_id": "stage_board_image",
                "instance_id": instance_id,
                "agent_type": "image",
                "objective": "生成可审阅的发布会主视觉方向。",
                "instructions": ["遵守品牌安全区。"],
                "input_assets": input_assets,
                "expected_deliveries": [
                    {
                        "kind": "image",
                        "role": "key_visual",
                        "required": True,
                        "accepted_mime_types": ["image/png"],
                    }
                ],
                "parameters": {"variants": 2, "usage_context": "发布会主屏"},
                "created_at": created_at,
            }
            for card_id, instance_id in (
                ("card_board_running", "instance_board_running"),
                ("card_board_approval", "instance_board_approval"),
            )
        ]
        response = client.put(
            f"/api/v1/tasks/{task_id}/plan",
            json={
                "stages": stages,
                "instances": instances,
                "task_cards": cards,
                "operation_id": "save_board_projection_plan",
                "envelope": self._envelope("save-board-projection-plan", 1),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        payload["plan"] = {"task_cards": cards}
        return payload

    @staticmethod
    def _envelope(key: str, expected_revision: int) -> dict[str, object]:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "operator",
            "expected_revision": expected_revision,
        }


if __name__ == "__main__":
    unittest.main()
