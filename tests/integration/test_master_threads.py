from __future__ import annotations

import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.core.errors import HarnessError
from harness.domain.commands import CommandEnvelope
from harness.services.master_orchestrator import MasterRunObservation
from harness.storage.atomic import atomic_write_json, read_json
from harness.storage.locks import FileLock
from harness.storage.repository import Actor, utc_now
from runtime_helpers import build_config_snapshot

ROOT = Path(__file__).resolve().parents[2]


class RecordingOrchestrator:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.revision = 0
        self.source_citation = "block/text_b1"

    def submit_message(self, task_id: str, message: dict[str, Any]) -> str:
        self.revision += 1
        run_id = f"run_master_{self.revision}"
        self.runs[run_id] = {"task_id": task_id, "message": deepcopy(message)}
        return run_id

    def observe_run(self, task_id: str, run_id: str) -> MasterRunObservation:
        self._assert_owner(task_id, run_id)
        return MasterRunObservation(
            "PLAN_READY",
            f"计划 r{self._revision(run_id)} 已生成, 请确认。",
            "秋季发布会主视觉",
        )

    def load_plan(self, task_id: str, run_id: str) -> dict[str, Any]:
        self._assert_owner(task_id, run_id)
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
                    "instructions": [
                        "遵守品牌安全区。",
                        *[
                            f"引用 {asset['asset_id']}/{self.source_citation}。"
                            for asset in message["asset_refs"]
                        ],
                    ],
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

    def _assert_owner(self, task_id: str, run_id: str) -> None:
        if self.runs[run_id]["task_id"] != task_id:
            raise AssertionError("run belongs to another task")

    @staticmethod
    def _revision(run_id: str) -> int:
        return int(run_id.rsplit("_", 1)[1])


class TwoCardRecordingOrchestrator(RecordingOrchestrator):
    def load_plan(self, task_id: str, run_id: str) -> dict[str, Any]:
        proposal = super().load_plan(task_id, run_id)
        revision = proposal["revision"]
        first_item = proposal["work_items"][0]
        first_card = proposal["execution_cards"][0]
        instance_id = f"instance_master_b_{revision}"
        card_id = f"card_master_b_{revision}"
        proposal["work_items"].append(
            {
                **deepcopy(first_item),
                "work_item_id": f"work_master_b_{revision}",
                "title": f"主视觉方向 B {revision}",
                "current_instance_id": instance_id,
                "instance_ids": [instance_id],
                "task_card_ids": [card_id],
            }
        )
        proposal["execution_cards"].append(
            {
                **deepcopy(first_card),
                "card_id": card_id,
                "instance_id": instance_id,
                "objective": f"生成主视觉方向 B {revision}。",
            }
        )
        return proposal


class RecordingApplication:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []
        self.started: list[dict[str, Any]] = []

    def save_plan_and_create_instances(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.saved.append({"task_id": task_id, **deepcopy(kwargs)})
        expected = kwargs["envelope"].expected_revision
        return {"task_revision": expected, "task": {"task_id": task_id}}

    def confirm_and_start_ready_instances(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.started.append({"task_id": task_id, **deepcopy(kwargs)})
        return {"task_id": task_id, "launches": [{"instance_id": "recorded"}], "unavailable": []}


class LandedRecordingApplication(RecordingApplication):
    def __init__(self, commands: Any, store: Any) -> None:
        super().__init__()
        self.commands = commands
        self.store = store
        self.started_instance_ids: set[str] = set()
        self.merge_preserved_started_instances: list[bool] = []

    def save_plan_and_create_instances(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.saved.append({"task_id": task_id, **deepcopy(kwargs)})
        before_plan = self.store.plan.get(task_id, task_id)
        before_started = {}
        if before_plan is not None:
            card_by_instance = {
                card["instance_id"]: card for card in before_plan["task_cards"]
            }
            before_started = {
                instance["instance_id"]: {
                    "instance": deepcopy(instance),
                    "card": deepcopy(card_by_instance[instance["instance_id"]]),
                    "projection_revision": self.store.instance.revision(
                        task_id, instance["instance_id"]
                    ),
                }
                for instance in before_plan["instances"]
                if instance["instance_id"] in self.started_instance_ids
                or instance["status"] not in {"READY", "UNAVAILABLE"}
            }
        result = self.commands.save_plan(
            task_id,
            stages=kwargs["stages"],
            instances=kwargs["instances"],
            task_cards=kwargs["task_cards"],
            envelope=kwargs["envelope"],
            mode=kwargs.get("mode", "replace"),
            expected_plan_revision=kwargs.get("expected_plan_revision"),
        )
        if kwargs.get("mode") == "merge":
            after_plan = result["plan"]
            after_card_by_instance = {
                card["instance_id"]: card for card in after_plan["task_cards"]
            }
            after_instance_by_id = {
                instance["instance_id"]: instance for instance in after_plan["instances"]
            }
            self.merge_preserved_started_instances.extend(
                after_instance_by_id[instance_id] == snapshot["instance"]
                and after_card_by_instance[instance_id] == snapshot["card"]
                and self.store.instance.revision(task_id, instance_id)
                == snapshot["projection_revision"]
                for instance_id, snapshot in before_started.items()
            )
        return result

    def confirm_and_start_ready_instances(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.started.append({"task_id": task_id, **deepcopy(kwargs)})
        plan = self.store.plan.get(task_id, task_id)
        if plan is None:
            raise AssertionError("expected a landed plan before starting instances")
        operation_id = kwargs["operation_id"]
        if plan["task"]["status"] == "AWAITING_START_CONFIRMATION":
            self.commands.confirm_start(
                task_id,
                CommandEnvelope(
                    idempotency_key=f"{operation_id}-confirm-start",
                    actor_type="human",
                    actor_id="human_operator",
                    expected_revision=self.store.task.revision(task_id, task_id),
                ),
            )
        selected = kwargs.get("only_instance_ids") or []
        self.started_instance_ids.update(selected)
        launches = [{"instance_id": instance_id} for instance_id in selected]
        return {"task_id": task_id, "launches": launches, "unavailable": []}

    def latest_start_operation(
        self, task_id: str, *, instance_id: str | None = None
    ) -> dict[str, Any] | None:
        if instance_id not in self.started_instance_ids:
            return None
        return {"task_id": task_id, "instance_id": instance_id, "state": "QUEUED"}


class InterruptedApplication(RecordingApplication):
    def confirm_and_start_ready_instances(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated crash after the plan-save checkpoint")


class InterruptedBeforePlanSaveApplication(RecordingApplication):
    def save_plan_and_create_instances(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated crash after the confirmation-intent checkpoint")


class MasterThreadApiTests(unittest.TestCase):
    def test_existing_master_session_get_does_not_wait_for_the_command_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            app.state.container.master_threads.orchestrator = RecordingOrchestrator()
            with TestClient(app) as client:
                task_id = self._create_submit(client, "manual")
                lock_path = (
                    app.state.container.store.layout.control_root
                    / "locks"
                    / f"command-{task_id}.lock"
                )
                with FileLock(lock_path, 1):
                    started_at = time.monotonic()
                    response = client.get(
                        f"/api/v1/tasks/{task_id}/master/messages"
                    )
                    elapsed = time.monotonic() - started_at

                self.assertEqual(response.status_code, 200, response.text)
                self.assertLess(elapsed, 0.5)

    def test_revisioned_thread_adjustment_and_manual_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            orchestrator = RecordingOrchestrator()
            application = RecordingApplication()
            app.state.container.master_threads.orchestrator = orchestrator
            app.state.container.master_threads.application = application
            with TestClient(app) as client:
                task_id = self._create_submit(client, "manual")
                first = client.get(f"/api/v1/tasks/{task_id}/master/messages")
                self.assertEqual(first.status_code, 200, first.text)
                first_session = first.json()
                self.assertEqual(first_session["latest_proposal"]["revision"], 1)
                self.assertEqual(first_session["latest_proposal"]["status"], "PENDING_CONFIRMATION")
                self.assertEqual(
                    first_session["unfinished_image_instance_ids"],
                    ["instance_master_1"],
                )
                self.assertEqual(
                    [item["role"] for item in first_session["messages"]],
                    ["user", "master"],
                )
                self.assertEqual(application.saved, [])

                late_upload = client.post(
                    f"/api/v1/tasks/{task_id}/assets",
                    files={"file": ("late.md", b"# late brief\n", "text/markdown")},
                    data={
                        "declared_mime_type": "text/markdown",
                        "description": "后续补充约束",
                        "idempotency_key": "upload-late-master-resource",
                        "actor_id": "human_operator",
                        "expected_revision": "3",
                    },
                )
                self.assertEqual(late_upload.status_code, 200, late_upload.text)
                late_asset_id = late_upload.json()["asset"]["asset_id"]
                cast(Any, client.app).state.container.asset_understanding.prepare(
                    task_id, [late_asset_id]
                )
                long_adjustment = "请把方向改得更克制。" + ("补充" * 10_000)
                adjusted = client.post(
                    f"/api/v1/tasks/{task_id}/master/messages",
                    json={
                        "content": long_adjustment,
                        "asset_refs": [{
                            "asset_id": late_asset_id,
                            "manifest_relpath": f"inputs/manifests/{late_asset_id}.json",
                        }],
                        "envelope": self._envelope(
                            "adjust-master-plan", first_session["thread_revision"]
                        ),
                    },
                )
                self.assertEqual(adjusted.status_code, 200, adjusted.text)
                adjusted_session = adjusted.json()
                self.assertEqual(adjusted_session["messages"][-2]["content"], long_adjustment)
                self.assertEqual(
                    adjusted_session["latest_proposal"]["execution_cards"][0]["input_assets"][0]["asset_id"],
                    late_asset_id,
                )
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
                        "expected_card_revisions": {"card_master_1": 1},
                        "envelope": self._envelope("stale-confirm", 1),
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(stale.json()["error"]["code"], "REVISION_CONFLICT")

                confirmed = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/2/confirm",
                    json={
                        "task_expected_revision": adjusted_session["task_revision"],
                        "expected_card_revisions": {"card_master_2": 1},
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

    def test_task_card_edit_creates_a_new_plan_revision_and_requires_exact_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            application = RecordingApplication()
            app.state.container.master_threads.orchestrator = RecordingOrchestrator()
            app.state.container.master_threads.application = application
            with TestClient(app) as client:
                task_id = self._create_submit(client, "manual")
                first = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                card = first["latest_proposal"]["execution_cards"][0]
                request = {
                    "expected_proposal_revision": 1,
                    "expected_card_revision": 1,
                    "objective": "生成更克制的自然光主视觉。",
                    "instructions": [*card["instructions"], "整体降低饱和度。"],
                    "input_assets": card["input_assets"],
                    "expected_deliveries": card["expected_deliveries"],
                    "parameters": {
                        "usage_context": "发布会主屏",
                        "aspect_ratio": "16:9",
                        "variants": 2,
                    },
                    "envelope": self._envelope("revise-card-1", 1),
                }
                revised = client.patch(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/task-cards/{card['card_id']}",
                    json=request,
                )
                self.assertEqual(revised.status_code, 200, revised.text)
                session = revised.json()
                proposal = session["latest_proposal"]
                self.assertEqual(proposal["revision"], 2)
                self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
                self.assertEqual(proposal["execution_cards"][0]["revision"], 2)
                self.assertEqual(
                    proposal["execution_cards"][0]["objective"],
                    "生成更克制的自然光主视觉。",
                )
                self.assertIn("请审阅未启动任务卡后继续", session["messages"][-1]["content"])
                proposals = app.state.container.store.plan_proposal.list(task_id)
                self.assertEqual(
                    {item["revision"]: item["status"] for item in proposals},
                    {1: "SUPERSEDED", 2: "PENDING_CONFIRMATION"},
                )

                replay = client.patch(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/task-cards/{card['card_id']}",
                    json=request,
                )
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(
                    replay.json()["latest_proposal"]["proposal_id"],
                    proposal["proposal_id"],
                )

                stale_review = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/2/confirm",
                    json={
                        "task_expected_revision": session["task_revision"],
                        "expected_card_revisions": {card["card_id"]: 1},
                        "envelope": self._envelope("confirm-stale-card", 2),
                    },
                )
                self.assertEqual(stale_review.status_code, 409, stale_review.text)
                self.assertEqual(stale_review.json()["error"]["code"], "REVISION_CONFLICT")
                self.assertEqual(application.saved, [])

                confirmed = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/2/confirm",
                    json={
                        "task_expected_revision": session["task_revision"],
                        "expected_card_revisions": {card["card_id"]: 2},
                        "envelope": self._envelope("confirm-reviewed-card", 2),
                    },
                )
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                self.assertEqual(confirmed.json()["proposal"]["status"], "CONFIRMED")
                self.assertEqual(
                    application.saved[0]["task_cards"][0]["objective"],
                    "生成更克制的自然光主视觉。",
                )

    def test_started_card_locks_while_an_unstarted_peer_remains_editable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            container = app.state.container
            container.master_threads.orchestrator = TwoCardRecordingOrchestrator()
            application = LandedRecordingApplication(container.commands, container.store)
            container.master_threads.application = application
            with TestClient(app) as client:
                task_id = self._create_submit(client, "manual")
                initial = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                cards = {
                    card["instance_id"]: card
                    for card in initial["latest_proposal"]["execution_cards"]
                }
                started_card = cards["instance_master_1"]
                unstarted_card = cards["instance_master_b_1"]
                self.assertCountEqual(
                    initial["editable_card_ids"],
                    [started_card["card_id"], unstarted_card["card_id"]],
                )

                started = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/confirm",
                    json={
                        "task_expected_revision": initial["task_revision"],
                        "expected_card_revisions": {
                            started_card["card_id"]: 1,
                            unstarted_card["card_id"]: 1,
                        },
                        "instance_ids": [started_card["instance_id"]],
                        "envelope": self._envelope("start-first-card", 1),
                    },
                )
                self.assertEqual(started.status_code, 200, started.text)
                started_session = started.json()["session"]
                self.assertEqual(started_session["editable_card_ids"], [unstarted_card["card_id"]])
                self.assertEqual(
                    started_session["instance_statuses"][started_card["instance_id"]],
                    "READY",
                )
                self.assertEqual(
                    started_session["instance_statuses"][unstarted_card["instance_id"]],
                    "READY",
                )

                locked_edit = client.patch(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/task-cards/{started_card['card_id']}",
                    json={
                        "expected_proposal_revision": 1,
                        "expected_card_revision": 1,
                        **self._editable_card_request(started_card),
                        "objective": "不得覆盖已启动任务卡。",
                        "envelope": self._envelope("reject-started-card-edit", 1),
                    },
                )
                self.assertEqual(locked_edit.status_code, 409, locked_edit.text)
                self.assertEqual(
                    locked_edit.json()["error"]["code"], "INVALID_STATE_TRANSITION"
                )

                editable_request = {
                    "expected_proposal_revision": 1,
                    "expected_card_revision": 1,
                    **self._editable_card_request(unstarted_card),
                    "objective": "生成更克制的第二条主视觉方向。",
                    "envelope": self._envelope("revise-unstarted-card", 1),
                }
                revised = client.patch(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/task-cards/{unstarted_card['card_id']}",
                    json=editable_request,
                )
                self.assertEqual(revised.status_code, 200, revised.text)
                revised_session = revised.json()
                revised_proposal = revised_session["latest_proposal"]
                revised_cards = {
                    card["instance_id"]: card
                    for card in revised_proposal["execution_cards"]
                }
                self.assertEqual(revised_proposal["revision"], 2)
                self.assertEqual(revised_session["editable_card_ids"], [unstarted_card["card_id"]])
                self.assertEqual(revised_cards[started_card["instance_id"]], started_card)
                self.assertEqual(revised_cards[unstarted_card["instance_id"]]["revision"], 2)

                confirmed_revision = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/2/confirm",
                    json={
                        "task_expected_revision": revised_session["task_revision"],
                        "expected_card_revisions": {
                            started_card["card_id"]: 1,
                            unstarted_card["card_id"]: 2,
                        },
                        "instance_ids": [unstarted_card["instance_id"]],
                        "envelope": self._envelope("start-revised-peer", 2),
                    },
                )
                self.assertEqual(confirmed_revision.status_code, 200, confirmed_revision.text)
                self.assertEqual(application.saved[-1]["mode"], "merge")
                self.assertEqual(application.merge_preserved_started_instances, [True])
                self.assertEqual(
                    confirmed_revision.json()["session"]["editable_card_ids"], []
                )

    def test_task_card_edit_revalidates_secrets_manifests_and_delivery_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            app.state.container.master_threads.orchestrator = RecordingOrchestrator()
            with TestClient(app) as client:
                task_id = self._create_submit(client, "manual")
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                card = session["latest_proposal"]["execution_cards"][0]
                base = {
                    "expected_proposal_revision": 1,
                    "expected_card_revision": 1,
                    "objective": card["objective"],
                    "instructions": card["instructions"],
                    "input_assets": card["input_assets"],
                    "expected_deliveries": card["expected_deliveries"],
                    "parameters": card["parameters"],
                }
                invalid_requests = [
                    {
                        **base,
                        "objective": "api_key=abcdefgh12345678",
                        "envelope": self._envelope("reject-card-secret", 1),
                    },
                    {
                        **base,
                        "input_assets": [{
                            **card["input_assets"][0],
                            "manifest_relpath": (
                                f"resources/manifests/{card['input_assets'][0]['asset_id']}.json"
                            ),
                        }],
                        "envelope": self._envelope("reject-card-manifest", 1),
                    },
                    {
                        **base,
                        "expected_deliveries": [{
                            **card["expected_deliveries"][0],
                            "accepted_mime_types": ["image/PNG"],
                        }],
                        "envelope": self._envelope("reject-card-mime", 1),
                    },
                    {
                        **base,
                        "instructions": [
                            f"引用 {card['input_assets'][0]['asset_id']}"
                            "/block/does_not_exist。"
                        ],
                        "envelope": self._envelope("reject-card-fabricated-block", 1),
                    },
                ]
                for request in invalid_requests:
                    with self.subTest(key=request["envelope"]["idempotency_key"]):
                        response = client.patch(
                            f"/api/v1/tasks/{task_id}/plan-proposals/1/task-cards/{card['card_id']}",
                            json=request,
                        )
                        self.assertIn(response.status_code, {400, 422}, response.text)

                proposals = app.state.container.store.plan_proposal.list(task_id)
                self.assertEqual(len(proposals), 1)
                self.assertEqual(proposals[0]["status"], "PENDING_CONFIRMATION")

    def test_auto_mode_waits_for_exact_human_confirmation_before_starting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            application = RecordingApplication()
            app.state.container.master_threads.orchestrator = RecordingOrchestrator()
            app.state.container.master_threads.application = application
            with TestClient(app) as client:
                task_id = self._create_submit(client, "auto")
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                self.assertEqual(
                    session["latest_proposal"]["status"], "PENDING_CONFIRMATION"
                )
                self.assertEqual(session["thread"]["active_run"]["status"], "PLAN_READY")
                self.assertEqual(application.saved, [])
                self.assertEqual(application.started, [])

                non_human = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/confirm",
                    json={
                        "task_expected_revision": session["task_revision"],
                        "expected_card_revisions": {"card_master_1": 1},
                        "envelope": {
                            **self._envelope("reject-master-confirm", 1),
                            "actor_type": "master",
                            "actor_id": "master_gateway",
                        },
                    },
                )
                self.assertEqual(non_human.status_code, 422, non_human.text)
                self.assertEqual(non_human.json()["error"]["code"], "VALIDATION_ERROR")

                stale_task = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/confirm",
                    json={
                        "task_expected_revision": session["task_revision"] + 1,
                        "expected_card_revisions": {"card_master_1": 1},
                        "envelope": self._envelope("reject-stale-auto-task", 1),
                    },
                )
                self.assertEqual(stale_task.status_code, 409, stale_task.text)
                self.assertEqual(stale_task.json()["error"]["code"], "REVISION_CONFLICT")

                stale_card = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/confirm",
                    json={
                        "task_expected_revision": session["task_revision"],
                        "expected_card_revisions": {"card_master_1": 2},
                        "envelope": self._envelope("reject-stale-auto-card", 1),
                    },
                )
                self.assertEqual(stale_card.status_code, 409, stale_card.text)
                self.assertEqual(stale_card.json()["error"]["code"], "REVISION_CONFLICT")
                self.assertEqual(application.saved, [])
                self.assertEqual(application.started, [])

                confirmed = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/confirm",
                    json={
                        "task_expected_revision": session["task_revision"],
                        "expected_card_revisions": {"card_master_1": 1},
                        "envelope": self._envelope("confirm-auto-plan", 1),
                    },
                )
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                self.assertEqual(confirmed.json()["proposal"]["status"], "CONFIRMED")
                self.assertEqual(len(application.saved), 1)
                self.assertEqual(len(application.started), 1)
                self.assertNotIn("providers", application.saved[0])

    def test_legacy_fabricated_citation_aborts_before_plan_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            application = RecordingApplication()
            app.state.container.master_threads.orchestrator = RecordingOrchestrator()
            app.state.container.master_threads.application = application
            with TestClient(app) as client:
                task_id = self._create_submit(client, "auto")
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                service = app.state.container.master_threads
                self._persist_fabricated_citation(service, task_id)

                response = client.post(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/confirm",
                    json={
                        "task_expected_revision": session["task_revision"],
                        "expected_card_revisions": {"card_master_1": 1},
                        "envelope": self._envelope("reject-legacy-fabrication", 1),
                    },
                )

                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("block that does not exist", response.text)
                self.assertEqual(application.saved, [])
                self.assertEqual(application.started, [])
                intent = read_json(service._confirm_intent_path(task_id, 1))
                self.assertEqual(intent["state"], "ABORTED")
                self.assertEqual(intent["error"]["code"], "VALIDATION_ERROR")

    def test_recovery_aborts_fabricated_citations_before_confirmation_side_effects(
        self,
    ) -> None:
        for intent_state in ("PREPARED", "PLAN_SAVED"):
            with (
                self.subTest(intent_state=intent_state),
                tempfile.TemporaryDirectory() as temporary,
            ):
                app = self._app(Path(temporary))
                application = RecordingApplication()
                app.state.container.master_threads.orchestrator = RecordingOrchestrator()
                app.state.container.master_threads.application = application
                with TestClient(app) as client:
                    task_id = self._create_submit(client, "auto")
                    session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                    service = app.state.container.master_threads
                    proposal = self._persist_fabricated_citation(service, task_id)
                    intent_path = service._confirm_intent_path(task_id, 1)
                    atomic_write_json(
                        intent_path,
                        {
                            "schema_version": "1.0",
                            "kind": "CONFIRM_MASTER_PLAN",
                            "task_id": task_id,
                            "proposal_id": proposal["proposal_id"],
                            "proposal_revision": 1,
                            "expected_card_revisions": {"card_master_1": 1},
                            "task_expected_revision": session["task_revision"],
                            "actor": {
                                "actor_type": "human",
                                "actor_id": "human_operator",
                            },
                            "state": intent_state,
                            "prepared_at": utc_now(),
                            "plan_result": (
                                {
                                    "task_revision": session["task_revision"],
                                    "task": {"task_id": task_id},
                                }
                                if intent_state == "PLAN_SAVED"
                                else None
                            ),
                            "start_result": None,
                            "result": None,
                        },
                    )

                    with self.assertRaisesRegex(
                        HarnessError, "block that does not exist"
                    ):
                        service.recover()

                    self.assertEqual(application.saved, [])
                    self.assertEqual(application.started, [])
                    intent = read_json(intent_path)
                    self.assertEqual(intent["state"], "ABORTED")
                    self.assertEqual(intent["error"]["code"], "VALIDATION_ERROR")

    def test_recovery_rejects_a_non_human_confirmation_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            application = RecordingApplication()
            app.state.container.master_threads.orchestrator = RecordingOrchestrator()
            app.state.container.master_threads.application = application
            with TestClient(app) as client:
                task_id = self._create_submit(client, "auto")
                service = app.state.container.master_threads
                intent_path = service._confirm_intent_path(task_id, 1)
                atomic_write_json(
                    intent_path,
                    {
                        "schema_version": "1.0",
                        "kind": "CONFIRM_MASTER_PLAN",
                        "task_id": task_id,
                        "proposal_revision": 1,
                        "actor": {
                            "actor_type": "master",
                            "actor_id": "master_gateway",
                        },
                        "state": "PREPARED",
                    },
                )

                with self.assertRaisesRegex(
                    HarnessError, "Only a human may confirm and start"
                ):
                    service.recover()

                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                self.assertEqual(
                    session["latest_proposal"]["status"], "PENDING_CONFIRMATION"
                )
                self.assertEqual(application.saved, [])
                self.assertEqual(application.started, [])

    def test_recovery_rejects_a_fabricated_citation_before_plan_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            orchestrator = RecordingOrchestrator()
            app.state.container.master_threads.orchestrator = orchestrator
            with TestClient(app) as client:
                task_id = self._create_submit(client, "manual")
                service = app.state.container.master_threads
                thread = app.state.container.store.master_thread.get(task_id, task_id)
                self.assertIsNotNone(thread)
                message_id = thread["active_run"]["message_id"]
                message = app.state.container.store.master_message.get(task_id, message_id)
                self.assertIsNotNone(message)

                orchestrator.source_citation = "block/does_not_exist"
                run_id = orchestrator.submit_message(task_id, message)
                now = utc_now()
                service._put_thread(
                    task_id,
                    {
                        **thread,
                        "active_run": {
                            "run_id": run_id,
                            "message_id": message_id,
                            "status": "RUNNING",
                            "started_at": now,
                            "updated_at": now,
                        },
                    },
                    Actor("system", "recovery_test"),
                    "checkpoint_running_master_for_test",
                )

                with self.assertRaisesRegex(HarnessError, "block that does not exist"):
                    service.recover()

                proposals = app.state.container.store.plan_proposal.list(task_id)
                self.assertEqual([item["revision"] for item in proposals], [1])

    def test_recovery_aborts_r1_after_a_persisted_intent_is_superseded_by_r2(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            app.state.container.master_threads.orchestrator = RecordingOrchestrator()
            app.state.container.master_threads.application = (
                InterruptedBeforePlanSaveApplication()
            )
            with TestClient(app) as client:
                task_id = self._create_submit(client, "auto")
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                service = app.state.container.master_threads

                with self.assertRaisesRegex(
                    RuntimeError, "confirmation-intent checkpoint"
                ):
                    service.confirm_plan(
                        task_id,
                        1,
                        task_expected_revision=session["task_revision"],
                        expected_card_revisions={"card_master_1": 1},
                        envelope=CommandEnvelope.model_validate(
                            self._envelope("persist-r1-intent", 1)
                        ),
                    )

                intent_path = service._confirm_intent_path(task_id, 1)
                self.assertEqual(read_json(intent_path)["state"], "PREPARED")
                card = session["latest_proposal"]["execution_cards"][0]
                revised = client.patch(
                    f"/api/v1/tasks/{task_id}/plan-proposals/1/task-cards/{card['card_id']}",
                    json={
                        "expected_proposal_revision": 1,
                        "expected_card_revision": 1,
                        "objective": "采用经过人工复核的新方向。",
                        "instructions": card["instructions"],
                        "input_assets": card["input_assets"],
                        "expected_deliveries": card["expected_deliveries"],
                        "parameters": card["parameters"],
                        "envelope": self._envelope("revise-after-r1-intent", 1),
                    },
                )
                self.assertEqual(revised.status_code, 200, revised.text)
                self.assertEqual(revised.json()["latest_proposal"]["revision"], 2)

                recovered_application = RecordingApplication()
                service.application = recovered_application
                with self.assertRaises(HarnessError) as raised:
                    service.recover()

                self.assertEqual(raised.exception.code, "REVISION_CONFLICT")
                self.assertEqual(recovered_application.saved, [])
                self.assertEqual(recovered_application.started, [])
                self.assertEqual(read_json(intent_path)["state"], "ABORTED")
                proposals = app.state.container.store.plan_proposal.list(task_id)
                self.assertEqual(
                    {item["revision"]: item["status"] for item in proposals},
                    {1: "SUPERSEDED", 2: "PENDING_CONFIRMATION"},
                )
                self.assertEqual(service.recover(), [])

    def test_direct_test_settings_without_deployment_snapshot_fail_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(
                HarnessSettings(
                    control_root=root / "control-data",
                    workspace_root=root / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                )
            )
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/task-intakes",
                    json={
                        "prompt": "为秋季发布会制作主视觉。",
                        "start_policy": "manual",
                        "envelope": self._envelope("create-without-config", 0),
                    },
                )
                self.assertEqual(created.status_code, 422, created.text)
                self.assertEqual(created.json()["error"]["code"], "VALIDATION_ERROR")

    def test_startup_recovery_resumes_a_partial_master_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            orchestrator = RecordingOrchestrator()
            app.state.container.master_threads.orchestrator = orchestrator
            app.state.container.master_threads.application = InterruptedApplication()
            with TestClient(app) as client:
                task_id = self._create_submit(client, "manual")
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                service = app.state.container.master_threads
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    service.confirm_plan(
                        task_id,
                        1,
                        task_expected_revision=session["task_revision"],
                        expected_card_revisions={"card_master_1": 1},
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

    @staticmethod
    def _editable_card_request(card: dict[str, Any]) -> dict[str, Any]:
        return {
            "objective": card["objective"],
            "instructions": deepcopy(card["instructions"]),
            "input_assets": deepcopy(card["input_assets"]),
            "expected_deliveries": deepcopy(card["expected_deliveries"]),
            "parameters": deepcopy(card["parameters"]),
        }

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
        asset_id = uploaded.json()["asset"]["asset_id"]
        cast(Any, client.app).state.container.asset_understanding.prepare(
            task_id, [asset_id]
        )
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
    def _persist_fabricated_citation(
        service: Any, task_id: str
    ) -> dict[str, Any]:
        proposal = service.store.plan_proposal.get(task_id, "proposal_master_1")
        if proposal is None:
            raise AssertionError("expected the first Master proposal")
        fabricated = deepcopy(proposal)
        asset_id = fabricated["execution_cards"][0]["input_assets"][0]["asset_id"]
        fabricated["execution_cards"][0]["instructions"] = [
            f"引用 {asset_id}/block/does_not_exist。"
        ]
        service.store.plan_proposal.put(
            task_id,
            fabricated["proposal_id"],
            fabricated,
            expected_revision=service.store.plan_proposal.revision(
                task_id, fabricated["proposal_id"]
            ),
            actor=Actor("system", "legacy_fixture"),
            command="seed_legacy_fabricated_citation",
            idempotency_key=f"seed-legacy-fabrication-{task_id}",
        )
        return fabricated

    @staticmethod
    def _app(root: Path):
        return create_app(
            HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
                config_snapshot=build_config_snapshot(),
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
