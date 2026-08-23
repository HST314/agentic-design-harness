from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.services.model_clients import ModelResult, ModelUsage, ToolCall
from harness.storage.repository import utc_now
from runtime_helpers import PROJECT_ROOT, build_config_snapshot


class PlanningTextClient:
    def __init__(self, factory: FakeModelFactory) -> None:
        self.factory = factory

    def complete_structured(self, **kwargs: Any) -> ModelResult:
        self.factory.calls.append(deepcopy(kwargs))
        number = len(self.factory.calls)
        if number == 1:
            return self._result(
                number,
                output=None,
                tool_calls=(
                    ToolCall(
                        "call_read_brief",
                        "read_asset_blocks",
                        {"asset_id": self.factory.asset_id, "block_ids": ["text_b1"]},
                    ),
                ),
            )
        task_id = self.factory.task_id
        asset_id = self.factory.asset_id
        now = utc_now()
        proposal = {
            "schema_version": "1.0",
            "proposal_id": "proposal_internal_master_1",
            "task_id": task_id,
            "revision": 1,
            "status": "PENDING_CONFIRMATION",
            "stages": [
                {
                    "stage_id": "stage_internal_image_1",
                    "type": "image",
                    "position": 1,
                    "depends_on": [],
                    "required": True,
                }
            ],
            "work_items": [
                {
                    "schema_version": "1.0",
                    "work_item_id": "work_internal_image_1",
                    "task_id": task_id,
                    "stage_id": "stage_internal_image_1",
                    "title": "Create cited key visual",
                    "agent_type": "image",
                    "required": True,
                    "depends_on": [],
                    "current_instance_id": "instance_internal_image_1",
                    "instance_ids": ["instance_internal_image_1"],
                    "task_card_ids": ["card_internal_image_1"],
                }
            ],
            "execution_cards": [
                {
                    "schema_version": "1.1",
                    "card_id": "card_internal_image_1",
                    "revision": 1,
                    "task_id": task_id,
                    "stage_id": "stage_internal_image_1",
                    "instance_id": "instance_internal_image_1",
                    "agent_type": "image",
                    "objective": "Create the campaign key visual.",
                    "instructions": (
                        [f"Use {asset_id}/block/text_b1 as the brief source."]
                        if self.factory.cite_sources
                        else ["Use the uploaded brief as the source."]
                    ),
                    "input_assets": [
                        {
                            "asset_id": asset_id,
                            "manifest_relpath": f"inputs/manifests/{asset_id}.json",
                        }
                    ],
                    "expected_deliveries": [
                        {
                            "kind": "image",
                            "role": "key_visual",
                            "required": True,
                            "accepted_mime_types": ["image/png"],
                        }
                    ],
                    "parameters": {"variants": 2, "usage_context": "Launch"},
                    "created_at": now,
                }
            ],
            "created_at": now,
            "updated_at": now,
            "confirmed_at": None,
        }
        return self._result(
            number,
            output={
                "status": "PLAN_READY",
                "message": "The cited plan is ready for review.",
                "task_title": "Cited launch visual",
                "proposal": proposal,
            },
            tool_calls=(),
        )

    @staticmethod
    def _result(
        number: int,
        *,
        output: dict[str, Any] | None,
        tool_calls: tuple[ToolCall, ...],
    ) -> ModelResult:
        return ModelResult(
            request_id=f"master_request_{number}",
            provider_request_id=f"provider_master_{number}",
            provider="ark",
            model="text-model",
            call_type="reasoning_llm",
            output=output,
            tool_calls=tool_calls,
            usage=ModelUsage(10, 5, 0, 0, 15, {"prompt_tokens": 10}),
        )


class FakeModelFactory:
    def __init__(self) -> None:
        self.task_id = ""
        self.asset_id = ""
        self.cite_sources = True
        self.calls: list[dict[str, Any]] = []

    def text(self, snapshot, model_id, *, timeout_seconds):
        return PlanningTextClient(self)

    def vision(self, snapshot, model_id, *, timeout_seconds):
        raise AssertionError("Markdown understanding must not call VLM")


class MasterOrchestratorIntegrationTests(unittest.TestCase):
    def test_required_source_citation_rejects_an_uncited_task_card(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = FakeModelFactory()
            factory.cite_sources = False
            settings = HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=PROJECT_ROOT / "contracts" / "v1",
                image_agent_lock_path=PROJECT_ROOT / "agents" / "image-agent.lock.json",
                image_agent_root=PROJECT_ROOT / "agents" / "image_agent_mvp",
                config_snapshot=build_config_snapshot(require_source_citations=True),
            )
            app = create_app(settings, model_clients=factory)
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/task-intakes",
                    json={
                        "prompt": "Create a launch key visual from the brief.",
                        "start_policy": "manual",
                        "envelope": self._envelope("create-uncited-master", 0),
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                body = created.json()
                task_id = body["task"]["task_id"]
                factory.task_id = task_id
                uploaded = client.post(
                    f"/api/v1/task-intakes/{task_id}/assets",
                    files={
                        "file": (
                            "brief.md",
                            b"# Brief\n\nUse blue and preserve generous logo clearance.",
                            "text/markdown",
                        )
                    },
                    data={
                        "declared_mime_type": "text/markdown",
                        "description": "brand brief",
                        "idempotency_key": "upload-uncited-brief",
                        "actor_id": "human_operator",
                        "expected_revision": "1",
                    },
                )
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                factory.asset_id = uploaded.json()["asset"]["asset_id"]

                submitted = client.post(
                    f"/api/v1/task-intakes/{task_id}/submit",
                    json={
                        "task_expected_revision": body["task_revision"],
                        "envelope": self._envelope(
                            "submit-uncited-master",
                            uploaded.json()["intake_revision"],
                        ),
                    },
                )
                self.assertEqual(submitted.status_code, 200, submitted.text)
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages")
                self.assertEqual(session.status_code, 200, session.text)
                self.assertEqual(
                    session.json()["thread"]["active_run"]["status"], "FAILED"
                )
                self.assertEqual(
                    app.state.container.store.plan_proposal.list(task_id), []
                )

    def test_internal_master_uses_asset_tool_persists_plan_and_audits_usage_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = FakeModelFactory()
            settings = HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=PROJECT_ROOT / "contracts" / "v1",
                image_agent_lock_path=PROJECT_ROOT / "agents" / "image-agent.lock.json",
                image_agent_root=PROJECT_ROOT / "agents" / "image_agent_mvp",
                config_snapshot=build_config_snapshot(),
            )
            app = create_app(settings, model_clients=factory)
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/task-intakes",
                    json={
                        "prompt": "Create a launch key visual from the brief.",
                        "start_policy": "manual",
                        "envelope": self._envelope("create-internal-master", 0),
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                body = created.json()
                task_id = body["task"]["task_id"]
                factory.task_id = task_id
                uploaded = client.post(
                    f"/api/v1/task-intakes/{task_id}/assets",
                    files={
                        "file": (
                            "brief.md",
                            b"# Brief\n\nUse blue and preserve generous logo clearance.",
                            "text/markdown",
                        )
                    },
                    data={
                        "declared_mime_type": "text/markdown",
                        "description": "brand brief",
                        "idempotency_key": "upload-internal-brief",
                        "actor_id": "human_operator",
                        "expected_revision": "1",
                    },
                )
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                factory.asset_id = uploaded.json()["asset"]["asset_id"]
                submitted = client.post(
                    f"/api/v1/task-intakes/{task_id}/submit",
                    json={
                        "task_expected_revision": body["task_revision"],
                        "envelope": self._envelope(
                            "submit-internal-master",
                            uploaded.json()["intake_revision"],
                        ),
                    },
                )
                self.assertEqual(submitted.status_code, 200, submitted.text)
                session = client.get(
                    f"/api/v1/tasks/{task_id}/master/messages"
                ).json()
                self.assertEqual(session["thread"]["active_run"]["status"], "PLAN_READY")
                self.assertEqual(session["latest_proposal"]["revision"], 1)
                self.assertEqual(session["task"]["title"], "Cited launch visual")
                self.assertEqual(len(factory.calls), 2)
                tool_messages = [
                    item
                    for item in factory.calls[1]["messages"]
                    if item["role"] == "tool"
                ]
                self.assertEqual(len(tool_messages), 1)
                self.assertIn("text_b1", tool_messages[0]["content"])

                usage = client.get(f"/api/v1/tasks/{task_id}/usage?refresh=false").json()
                self.assertEqual(usage["event_count"], 2)
                self.assertEqual(usage["tokens"]["total_tokens"], 30)
                self.assertEqual(usage["instances"][0]["agent_type"], "master")

                message = app.state.container.store.master_message.get(
                    task_id, session["thread"]["active_run"]["message_id"]
                )
                app.state.container.master_threads.orchestrator.submit_message(
                    task_id, message
                )
                self.assertEqual(len(factory.calls), 2)
                self.assertEqual(
                    len(app.state.container.store.usage.list(task_id)), 2
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
