from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.services.model_clients import (
    ModelClientFailure,
    ModelResult,
    ModelUsage,
    ToolCall,
)
from harness.storage.atomic import read_json
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
        asset_id = self.factory.asset_id
        plan_draft = {
            "stages": [
                {
                    "type": "image",
                    "title": "Create cited key visual",
                    "required": True,
                    "objective": "Create the campaign key visual.",
                    "instructions": (
                        [f"Use {asset_id}/{self.factory.source_citation} as the brief source."]
                        if self.factory.source_citation is not None
                        else ["Use the uploaded brief as the source."]
                    ),
                    "input_asset_ids": [asset_id],
                    "expected_deliveries": [
                        {
                            "kind": "image",
                            "role": "key_visual",
                            "required": True,
                            "accepted_mime_types": ["image/png"],
                        }
                    ],
                    "parameters": {
                        "aspect_ratio": None,
                        "variants": 2,
                        "usage_context": "Launch",
                        "category_id": None,
                        "category_version": None,
                    },
                }
            ],
        }
        return self._result(
            number,
            output={
                "status": "PLAN_READY",
                "message": "The cited plan is ready for review.",
                "task_title": "Cited launch visual",
                "plan_draft": plan_draft,
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
        self.asset_id = ""
        self.source_citation: str | None = "block/text_b1"
        self.calls: list[dict[str, Any]] = []

    def text(self, snapshot, model_id, *, timeout_seconds):
        return PlanningTextClient(self)

    def vision(self, snapshot, model_id, *, timeout_seconds):
        raise AssertionError("Markdown understanding must not call VLM")


class ScriptedTextClient:
    def __init__(self, factory: ScriptedModelFactory) -> None:
        self.factory = factory

    def complete_structured(self, **kwargs: Any) -> ModelResult:
        self.factory.calls.append(deepcopy(kwargs))
        index = len(self.factory.calls) - 1
        scripted = self.factory.responses[min(index, len(self.factory.responses) - 1)]
        if isinstance(scripted, ModelClientFailure):
            raise scripted
        return ModelResult(
            request_id=f"scripted_request_{index + 1}",
            provider_request_id=f"scripted_provider_{index + 1}",
            provider="ark",
            model="text-model",
            call_type="reasoning_llm",
            output=deepcopy(scripted.get("output")),
            tool_calls=scripted.get("tool_calls", ()),
            usage=ModelUsage(10, 5, 0, 0, 15, {"prompt_tokens": 10}),
        )


class ScriptedModelFactory:
    def __init__(
        self,
        responses: list[dict[str, Any] | ModelClientFailure],
    ) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def text(self, snapshot, model_id, *, timeout_seconds):
        return ScriptedTextClient(self)

    def vision(self, snapshot, model_id, *, timeout_seconds):
        raise AssertionError("Markdown understanding must not call VLM")


def image_plan_response(
    *,
    input_asset_ids: list[str] | None = None,
    instructions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": "PLAN_READY",
        "message": "The plan is ready for review.",
        "task_title": "Culture wall key visual",
        "plan_draft": {
            "stages": [
                {
                    "type": "image",
                    "title": "Create the culture wall key visual",
                    "required": True,
                    "objective": "Create a culture wall key visual from the written brief.",
                    "instructions": instructions or ["Use the written requirements."],
                    "input_asset_ids": input_asset_ids or [],
                    "expected_deliveries": [
                        {
                            "kind": "image",
                            "role": "key_visual",
                            "required": True,
                            "accepted_mime_types": ["image/png"],
                        }
                    ],
                    "parameters": {
                        "aspect_ratio": None,
                        "variants": 2,
                        "usage_context": "Culture wall",
                        "category_id": None,
                        "category_version": None,
                    },
                }
            ]
        },
    }


class MasterOrchestratorIntegrationTests(unittest.TestCase):
    def test_zero_asset_text_task_materializes_plan_without_exposing_asset_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = ScriptedModelFactory([{"output": image_plan_response()}])
            app = create_app(self._settings(root), model_clients=factory)
            with TestClient(app) as client:
                body = self._create_task(client, "create-zero-asset")
                self._submit_task(client, body, "submit-zero-asset")
                session = client.get(
                    f"/api/v1/tasks/{body['task']['task_id']}/master/messages"
                ).json()

            self.assertEqual(session["thread"]["active_run"]["status"], "PLAN_READY")
            proposal = session["latest_proposal"]
            self.assertEqual(proposal["task_id"], body["task"]["task_id"])
            self.assertEqual(proposal["revision"], 1)
            self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
            self.assertEqual(proposal["execution_cards"][0]["input_assets"], [])
            self.assertEqual(proposal["created_at"], proposal["updated_at"])
            self.assertTrue(proposal["proposal_id"].startswith("proposal_"))
            self.assertEqual(factory.calls[0]["tools"], [])
            self._assert_strict_model_schema(factory.calls[0]["response_schema"])
            self.assertIn(
                "This task has no assets",
                factory.calls[0]["messages"][0]["content"],
            )

    def test_master_splits_independent_deliverables_into_parallel_cards(self) -> None:
        prompt = "做一个 A 海报和一个 B 文化墙"
        poster = image_plan_response()["plan_draft"]["stages"][0]
        poster.update(
            {
                "title": "北工大 A 海报",
                "objective": "Create the A poster key visual.",
                "parameters": {**poster["parameters"], "usage_context": "A poster"},
            }
        )
        culture_wall = image_plan_response()["plan_draft"]["stages"][0]
        culture_wall.update(
            {
                "title": "北工大 B 文化墙",
                "objective": "Create the B culture wall key visual.",
                "parameters": {**culture_wall["parameters"], "usage_context": "B culture wall"},
            }
        )
        response = image_plan_response()
        response["plan_draft"]["stages"] = [poster, culture_wall]
        factory = ScriptedModelFactory([{"output": response}])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(self._settings(root), model_clients=factory)
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/task-intakes",
                    json={
                        "prompt": prompt,
                        "start_policy": "manual",
                        "envelope": self._envelope("create-parallel-cards", 0),
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                body = created.json()
                task_id = body["task"]["task_id"]
                self._submit_task(client, body, "submit-parallel-cards")
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()

            self.assertEqual(session["thread"]["active_run"]["status"], "PLAN_READY")
            proposal = session["latest_proposal"]
            self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
            self.assertEqual(len(proposal["stages"]), 2)
            self.assertEqual(len(proposal["work_items"]), 2)
            self.assertEqual(len(proposal["execution_cards"]), 2)
            self.assertEqual(
                [item["title"] for item in proposal["work_items"]],
                ["北工大 A 海报", "北工大 B 文化墙"],
            )
            for stage in proposal["stages"]:
                self.assertEqual(stage["type"], "image")
                self.assertEqual(stage["depends_on"], [])
            system_prompt = factory.calls[0]["messages"][0]["content"]
            self.assertIn("one image stage per deliverable", system_prompt)
            self.assertIn("more than 6 image stages", system_prompt)
            user_messages = [
                item["content"]
                for item in factory.calls[0]["messages"]
                if item["role"] == "user"
            ]
            self.assertIn(prompt, user_messages)

    def test_invalid_plan_draft_is_repaired_once_before_materialization(self) -> None:
        invalid = image_plan_response()
        invalid["plan_draft"]["stages"][0]["parameters"]["unsupported_mode"] = "x"
        factory = ScriptedModelFactory([{"output": invalid}, {"output": image_plan_response()}])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(self._settings(root), model_clients=factory)
            with TestClient(app) as client:
                body = self._create_task(client, "create-repaired-draft")
                self._submit_task(client, body, "submit-repaired-draft")
                session = client.get(
                    f"/api/v1/tasks/{body['task']['task_id']}/master/messages"
                ).json()

            self.assertEqual(session["thread"]["active_run"]["status"], "PLAN_READY")
            self.assertEqual(len(factory.calls), 2)
            self.assertEqual(factory.calls[1]["tools"], [])
            repair_prompt = factory.calls[1]["messages"][-1]["content"]
            self.assertIn("Safe validation diagnostic", repair_prompt)
            self.assertIn("output_sha256", repair_prompt)

    def test_invalid_plan_draft_exhaustion_persists_only_safe_diagnostic(self) -> None:
        invalid = image_plan_response()
        invalid["plan_draft"]["stages"][0]["input_asset_ids"] = ["asset_fabricated"]
        factory = ScriptedModelFactory([{"output": invalid}])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(self._settings(root), model_clients=factory)
            with TestClient(app) as client:
                body = self._create_task(client, "create-invalid-draft")
                task_id = body["task"]["task_id"]
                self._submit_task(client, body, "submit-invalid-draft")
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()

            self.assertEqual(session["thread"]["active_run"]["status"], "FAILED")
            self.assertEqual(session["thread"]["last_error"]["code"], "MASTER_OUTPUT_INVALID")
            self.assertNotIn("素材", session["thread"]["last_error"]["message"])
            self.assertEqual(len(factory.calls), 3)
            run_id = session["thread"]["active_run"]["run_id"]
            run = read_json(
                root / "control-data" / "tasks" / task_id / "master" / "runs" / f"{run_id}.json"
            )
            self.assertEqual(
                set(run["diagnostic"]),
                {
                    "phase",
                    "cause_code",
                    "schema",
                    "path",
                    "reason",
                    "output_sha256",
                },
            )
            self.assertEqual(run["diagnostic"]["cause_code"], "MASTER_OUTPUT_INVALID")
            self.assertEqual(len(run["diagnostic"]["output_sha256"]), 64)
            self.assertNotIn("output", run)

    def test_asset_tool_validation_failure_has_a_separate_error_domain(self) -> None:
        factory = ScriptedModelFactory(
            [
                {
                    "output": None,
                    "tool_calls": (ToolCall("call_unknown", "unknown_asset_tool", {}),),
                }
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(self._settings(root), model_clients=factory)
            with TestClient(app) as client:
                body = self._create_task(client, "create-tool-failure")
                task_id = body["task"]["task_id"]
                uploaded = self._upload_brief(client, task_id)
                self._submit_task(
                    client,
                    body,
                    "submit-tool-failure",
                    intake_revision=uploaded["intake_revision"],
                )
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()

            self.assertEqual(session["thread"]["active_run"]["status"], "FAILED")
            self.assertEqual(session["thread"]["last_error"]["code"], "MASTER_ASSET_TOOL_FAILED")
            self.assertIn("素材", session["thread"]["last_error"]["message"])

    def test_model_provider_failure_keeps_provider_error_code(self) -> None:
        factory = ScriptedModelFactory(
            [
                ModelClientFailure(
                    "MODEL_PROVIDER_UNAVAILABLE",
                    "Model provider is temporarily unavailable.",
                )
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(self._settings(root), model_clients=factory)
            with TestClient(app) as client:
                body = self._create_task(client, "create-provider-failure")
                task_id = body["task"]["task_id"]
                self._submit_task(client, body, "submit-provider-failure")
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()

            self.assertEqual(
                session["thread"]["last_error"]["code"],
                "MODEL_PROVIDER_UNAVAILABLE",
            )

    def test_master_rejects_missing_or_fabricated_source_citations(self) -> None:
        for citation in (None, "block/does_not_exist"):
            with self.subTest(citation=citation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                factory = FakeModelFactory()
                factory.source_citation = citation
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
                            "envelope": self._envelope("create-invalid-master", 0),
                        },
                    )
                    self.assertEqual(created.status_code, 200, created.text)
                    body = created.json()
                    task_id = body["task"]["task_id"]
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
                            "idempotency_key": "upload-invalid-brief",
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
                                "submit-invalid-master",
                                uploaded.json()["intake_revision"],
                            ),
                        },
                    )
                    self.assertEqual(submitted.status_code, 200, submitted.text)
                    session = client.get(f"/api/v1/tasks/{task_id}/master/messages")
                    self.assertEqual(session.status_code, 200, session.text)
                    self.assertEqual(session.json()["thread"]["active_run"]["status"], "FAILED")
                    self.assertEqual(app.state.container.store.plan_proposal.list(task_id), [])

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
                session = client.get(f"/api/v1/tasks/{task_id}/master/messages").json()
                self.assertEqual(session["thread"]["active_run"]["status"], "PLAN_READY")
                self.assertEqual(session["latest_proposal"]["revision"], 1)
                self.assertEqual(session["task"]["title"], "Cited launch visual")
                self.assertEqual(len(factory.calls), 2)
                tool_messages = [
                    item for item in factory.calls[1]["messages"] if item["role"] == "tool"
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
                app.state.container.master_threads.orchestrator.submit_message(task_id, message)
                self.assertEqual(len(factory.calls), 2)
                self.assertEqual(len(app.state.container.store.usage.list(task_id)), 2)

    @staticmethod
    def _envelope(key: str, expected_revision: int) -> dict[str, Any]:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "human_operator",
            "expected_revision": expected_revision,
        }

    @staticmethod
    def _settings(root: Path) -> HarnessSettings:
        return HarnessSettings(
            control_root=root / "control-data",
            workspace_root=root / "workspace",
            contracts_root=PROJECT_ROOT / "contracts" / "v1",
            image_agent_lock_path=PROJECT_ROOT / "agents" / "image-agent.lock.json",
            image_agent_root=PROJECT_ROOT / "agents" / "image_agent_mvp",
            config_snapshot=build_config_snapshot(),
        )

    def _create_task(self, client: TestClient, key: str) -> dict[str, Any]:
        created = client.post(
            "/api/v1/task-intakes",
            json={
                "prompt": "Create a culture wall key visual.",
                "start_policy": "manual",
                "envelope": self._envelope(key, 0),
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        return created.json()

    def _upload_brief(self, client: TestClient, task_id: str) -> dict[str, Any]:
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
                "idempotency_key": f"upload-{task_id}",
                "actor_id": "human_operator",
                "expected_revision": "1",
            },
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        return uploaded.json()

    def _submit_task(
        self,
        client: TestClient,
        body: dict[str, Any],
        key: str,
        *,
        intake_revision: int | None = None,
    ) -> None:
        submitted = client.post(
            f"/api/v1/task-intakes/{body['task']['task_id']}/submit",
            json={
                "task_expected_revision": body["task_revision"],
                "envelope": self._envelope(
                    key,
                    body["intake_revision"] if intake_revision is None else intake_revision,
                ),
            },
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)

    def _assert_strict_model_schema(self, value: Any) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if value.get("type") == "object" and isinstance(properties, dict):
                self.assertEqual(set(value.get("required", [])), set(properties))
            for child in value.values():
                self._assert_strict_model_schema(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_strict_model_schema(child)


if __name__ == "__main__":
    unittest.main()
