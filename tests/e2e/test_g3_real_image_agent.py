from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from harness.adapters.image import ImageAgentAdapter
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.services.configuration import (
    GlobalConfigBody,
    ImageModelConfig,
    ModelBinding,
)
from harness.storage.repository import utc_now

ROOT = Path(__file__).resolve().parents[2]
IMAGE_AGENT_ROOT = os.getenv("HARNESS_IMAGE_AGENT_ROOT")
IMAGE_AGENT_PYTHON = os.getenv("HARNESS_IMAGE_AGENT_PYTHON")
IMAGE_AGENT_DEPENDENCY_ROOT = os.getenv("HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class DeterministicProviderHandler(BaseHTTPRequestHandler):
    """Small OpenAI-compatible provider used by the real Image subprocess."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/final.png":
            self._send(PNG, "image/png")
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/chat/completions"):
            self.server.chat_requests += 1  # type: ignore[attr-defined]
            content = body["messages"][-1]["content"]
            if isinstance(content, list):
                payload = {
                    "passed": True,
                    "decision": "pass",
                    "deviations": [],
                    "rework_prompt_delta": "",
                    "overall_score": 96,
                    "dimension_scores": {"task_fit": 96, "visual_quality": 96},
                    "confidence": 0.99,
                }
            elif "TaskConfirmationDoc" in content:
                payload = {
                    "summary": "已根据受控输入形成可执行创作任务书。",
                    "confirmed_facts": [],
                    "default_handling_for_unknowns": [],
                    "forbidden_items": [],
                    "human_annotations": [],
                    "markdown_body": (
                        "# 创作任务书\n\n"
                        "为内部审核创作一张清晰、克制且可追溯的最终视觉稿。\n"
                    ),
                }
            else:
                payload = {"questions": []}
            response = {
                "id": "chatcmpl_g3_acceptance",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model", "g3-provider"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(payload, ensure_ascii=False),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
            self._send_json(response)
            return
        if self.path.endswith("/images/generations"):
            self.server.image_requests += 1  # type: ignore[attr-defined]
            host, port = self.server.server_address  # type: ignore[attr-defined]
            self._send_json(
                {
                    "created": 1,
                    "data": [{"url": f"http://{host}:{port}/final.png"}],
                }
            )
            return
        self.send_error(404)

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(self, payload: dict[str, Any]) -> None:
        self._send(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def deterministic_provider() -> Iterator[tuple[str, ThreadingHTTPServer]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), DeterministicProviderHandler)
    server.chat_requests = 0  # type: ignore[attr-defined]
    server.image_requests = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@unittest.skipUnless(
    IMAGE_AGENT_ROOT and IMAGE_AGENT_PYTHON and IMAGE_AGENT_DEPENDENCY_ROOT,
    "set all HARNESS_IMAGE_AGENT_* runtime paths for the G3 real-Adapter gate",
)
class RealImageAdapterG3Tests(unittest.TestCase):
    def test_real_http_approval_to_finalize_publish_and_complete(self) -> None:
        with (
            deterministic_provider() as (provider_url, provider),
            tempfile.TemporaryDirectory() as temporary,
        ):
            runtime = Path(temporary)
            app = create_app(
                HarnessSettings(
                    control_root=runtime / "control-data",
                    workspace_root=runtime / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    image_agent_root=Path(str(IMAGE_AGENT_ROOT)),
                    image_agent_python=Path(str(IMAGE_AGENT_PYTHON)),
                    image_agent_dependency_root=Path(
                        str(IMAGE_AGENT_DEPENDENCY_ROOT)
                    ),
                )
            )
            app.state.container.configuration.initialize(self._online_config())
            instance_id = "i_g3_real_image"
            try:
                with TestClient(app) as client:
                    container = app.state.container
                    self.assertIsInstance(
                        container.adapters.get("image"), ImageAgentAdapter
                    )
                    container.credentials.configure_pool(
                        [
                            {
                                "credential_pair_id": "cred_g3_real",
                                "provider": "ark",
                                "key_id": "key_g3_real",
                                "base_url": provider_url,
                                "api_key": "not-a-secret-g3-real",
                                "api_key_env": "ARK_API_KEY",
                                "base_url_env": "ARK_BASE_URL",
                                "revision": 1,
                                "enabled": True,
                            }
                        ]
                    )
                    self._create_task(client)
                    selected = self._import_brief(container)
                    plan = client.put(
                        "/api/v1/tasks/t_g3_real_image/plan",
                        json=self._plan_request(selected),
                    )
                    self.assertEqual(plan.status_code, 200, plan.text)
                    started = client.post(
                        "/api/v1/tasks/t_g3_real_image/confirm-start",
                        json={
                            "operation_id": "start_g3_real_image",
                            "envelope": self._envelope(
                                "start-g3-real-image", plan.json()["task_revision"]
                            ),
                        },
                    )
                    self.assertEqual(started.status_code, 200, started.text)

                    taskbook = self._wait_for_boundary(client, instance_id)
                    self._resolve(client, taskbook, "approve_taskbook", {}, 1)

                    selection = self._wait_for_boundary(client, instance_id)
                    selection_details = client.get(
                        f"/api/v1/approvals/{selection['pending_approval']['approval_id']}"
                    )
                    self.assertEqual(selection_details.status_code, 200)
                    candidates = selection_details.json()["payload"]["context"][
                        "candidates"
                    ]
                    self._resolve(
                        client,
                        selection,
                        "select_master",
                        {"selected_id": candidates[0]["id"]},
                        2,
                    )

                    calibration = self._wait_for_boundary(client, instance_id)
                    self._resolve(
                        client,
                        calibration,
                        "review_calibration",
                        {"manual_action": "accept_current"},
                        3,
                    )

                    completed = self._wait_for_boundary(client, instance_id)
                    self.assertEqual(completed["instance"]["status"], "SUCCEEDED")
                    task = client.get("/api/v1/tasks/t_g3_real_image").json()["task"]
                    self.assertEqual(task["status"], "SUCCEEDED")
                    resources = client.get(
                        "/api/v1/tasks/t_g3_real_image/files?group=shared"
                    ).json()
                    published = [
                        item
                        for item in resources["assets"]
                        if item["manifest"]["producer_instance_id"] == instance_id
                    ]
                    self.assertEqual(len(published), 1)
                    self.assertEqual(published[0]["integrity_status"], "VERIFIED")
                    self.assertEqual(published[0]["manifest"]["sha256"], self._sha256(PNG))
                    approvals = client.get(
                        f"/api/v1/instances/{instance_id}/approvals"
                    ).json()["items"]
                    self.assertEqual([item["status"] for item in approvals], [
                        "APPROVED", "APPROVED", "APPROVED"
                    ])
                    inbox = client.get("/api/v1/inbox?owner=human").json()["items"]
                    self.assertEqual(
                        [item["kind"] for item in inbox],
                        [
                            "APPROVAL_REQUIRED",
                            "APPROVAL_REQUIRED",
                            "APPROVAL_REQUIRED",
                            "INSTANCE_SUCCEEDED",
                            "TASK_SUCCEEDED",
                        ],
                    )
                    self.assertTrue(
                        all(item["status"] == "HANDLED" for item in inbox[:3])
                    )
                    finalized = (
                        runtime
                        / "workspace"
                        / "tasks"
                        / "t_g3_real_image"
                        / "instances"
                        / instance_id
                        / "work"
                        / instance_id
                        / "delivery"
                        / "finalized.json"
                    )
                    self.assertTrue(finalized.is_file())
                    self.assertGreaterEqual(provider.chat_requests, 3)  # type: ignore[attr-defined]
                    self.assertEqual(provider.image_requests, 5)  # type: ignore[attr-defined]
                    usage_response = client.get(
                        "/api/v1/tasks/t_g3_real_image/usage"
                    )
                    self.assertEqual(usage_response.status_code, 200, usage_response.text)
                    usage = usage_response.json()
                    expected_calls = (  # type: ignore[attr-defined]
                        provider.chat_requests + provider.image_requests
                    )
                    self.assertEqual(usage["completeness"], "COMPLETE")
                    self.assertEqual(usage["event_count"], expected_calls)
                    self.assertEqual(
                        usage["tokens"]["total_tokens"],
                        provider.chat_requests * 2,  # type: ignore[attr-defined]
                    )
                    image_usage = [
                        event
                        for event in usage["events"]
                        if event.get("call_type") == "text_to_image_model"
                    ]
                    self.assertEqual(len(image_usage), provider.image_requests)  # type: ignore[attr-defined]
                    self.assertTrue(
                        all(
                            event["usage_basis"] == "image_units"
                            and event["billing_units"][0]["unit"] == "image"
                            and event["billing_units"][0]["attributes"]["resolution"]
                            == "2560x1440"
                            for event in image_usage
                        )
                    )
                    self.assertEqual(usage["cost"]["completeness"], "UNKNOWN")
                    self.assertEqual(
                        usage["cost"]["unpriced_event_count"], expected_calls
                    )
            finally:
                if app.state.container.store.instance.get(
                    "t_g3_real_image", instance_id
                ) is not None:
                    with suppress(Exception):
                        app.state.container.application.cancel_instance(
                            "t_g3_real_image", instance_id
                        )
                self._make_tree_removable(runtime)

    @staticmethod
    def _online_config() -> GlobalConfigBody:
        base = GlobalConfigBody()
        policy = type(base.image_runtime_policy).model_validate(
            {
                **base.image_runtime_policy.model_dump(mode="json"),
                "offline_mode": False,
                "category_constraint": {"release": "off"},
                "style_direction": {"release": "off"},
                "skill_invocation": {"release": "off"},
                "self_check": {
                    "termination": "solo",
                    "fixed_rounds": 1,
                    "max_rounds": 1,
                    "stop_early_on_pass": True,
                    "release": "manual",
                },
            }
        )
        bindings = [
            ModelBinding(
                state="intake_clarify",
                model_role="reasoning_llm",
                provider="ark",
                model="g3-text",
            ),
            ModelBinding(
                state="confirmation_build",
                model_role="reasoning_llm",
                provider="ark",
                model="g3-text",
            ),
            ModelBinding(
                state="initial_candidate_generation",
                model_role="text_to_image_model",
                provider="ark",
                model="doubao-seedream-g3-test",
            ),
            ModelBinding(
                state="self_check_inspection",
                model_role="vision_language_model",
                provider="ark",
                model="g3-vlm",
            ),
            ModelBinding(
                state="self_check_rework",
                model_role="text_to_image_model",
                provider="ark",
                model="doubao-seedream-g3-test",
            ),
            ModelBinding(
                state="human_prompt_rework",
                model_role="text_to_image_model",
                provider="ark",
                model="doubao-seedream-g3-test",
            ),
        ]
        return GlobalConfigBody(
            image_provider="ark",
            image_runtime_policy=policy,
            image_model_config=ImageModelConfig(
                model_config_id="g3_deterministic_provider",
                state_bindings=bindings,
            ),
            supervisor=base.supervisor,
        )

    @staticmethod
    def _create_task(client: TestClient) -> None:
        response = client.post(
            "/api/v1/tasks",
            json={
                "task_id": "t_g3_real_image",
                "title": "G3 real Image Adapter",
                "goal": "Publish a real HTTP Image workflow delivery.",
                "master_owner": "master_default",
                "start_policy": "manual",
                "input_manifest": "inputs/manifests/g3-real.json",
                "envelope": RealImageAdapterG3Tests._envelope(
                    "create-g3-real-image", 0
                ),
            },
        )
        if response.status_code != 200:
            raise AssertionError(response.text)

    @staticmethod
    def _import_brief(container) -> list[dict[str, str]]:
        imported = container.assets.import_bytes(
            "t_g3_real_image",
            filename="brief.md",
            content=b"# Controlled G3 provider acceptance brief\n",
            description="Approved source for the real Image Adapter G3 gate.",
            source="g3_real_acceptance",
            idempotency_key="import-g3-real-brief",
        )
        selected = container.assets.select_inputs(
            "t_g3_real_image",
            [imported["asset_id"]],
            manifest_id="g3-real-inputs",
        )
        return selected["task_card_inputs"]

    @classmethod
    def _plan_request(cls, inputs: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "stages": [
                {
                    "stage_id": "s_image",
                    "task_id": "t_g3_real_image",
                    "type": "image",
                    "position": 1,
                    "depends_on": [],
                    "required": True,
                    "instance_ids": ["i_g3_real_image"],
                }
            ],
            "instances": [
                {
                    "instance_id": "i_g3_real_image",
                    "task_id": "t_g3_real_image",
                    "stage_id": "s_image",
                    "agent_type": "image",
                    "required": True,
                    "approval_mode": "human",
                    "config_revision": 1,
                    "credential_pair_ref": "pending_assignment",
                    "credential_pair_revision": 1,
                    "workspace_relpath": "instances/i_g3_real_image",
                    "task_card_relpath": "instances/i_g3_real_image/task-card.json",
                }
            ],
            "task_cards": [
                {
                    "schema_version": "1.1",
                    "card_id": "card_g3_real_image",
                    "revision": 1,
                    "task_id": "t_g3_real_image",
                    "stage_id": "s_image",
                    "instance_id": "i_g3_real_image",
                    "agent_type": "image",
                    "objective": "Create one controlled final image.",
                    "instructions": ["Use only the approved source brief."],
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
                        "usage_context": "G3 controlled integration acceptance",
                    },
                    "created_at": utc_now(),
                }
            ],
            "providers": {"i_g3_real_image": "ark"},
            "operation_id": "save_g3_real_image_plan",
            "envelope": cls._envelope("save-g3-real-image-plan", 1),
        }

    def _wait_for_boundary(
        self, client: TestClient, instance_id: str, timeout: float = 45
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        detail: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/instances/{instance_id}")
            self.assertEqual(response.status_code, 200, response.text)
            detail = response.json()
            status = detail["instance"]["status"]
            if status in {"WAITING_APPROVAL", "SUCCEEDED", "FAILED"}:
                break
            time.sleep(0.1)
        if detail is None:
            self.fail("Image instance produced no observation.")
        self.assertNotEqual(detail["instance"]["status"], "FAILED", detail)
        self.assertIn(
            detail["instance"]["status"], {"WAITING_APPROVAL", "SUCCEEDED"}, detail
        )
        return detail

    def _resolve(
        self,
        client: TestClient,
        detail: dict[str, Any],
        action: str,
        payload: dict[str, Any],
        index: int,
    ) -> None:
        approval = detail["pending_approval"]
        self.assertIsNotNone(approval)
        approval_details = client.get(
            f"/api/v1/approvals/{approval['approval_id']}"
        ).json()
        self.assertIn(action, approval_details["payload"]["available_actions"])
        response = client.post(
            f"/api/v1/approvals/{approval['approval_id']}/resolve",
            json={
                "decision": "APPROVED",
                "action": action,
                "payload": payload,
                "operation_id": f"resolve_g3_real_{index}",
                "envelope": self._envelope(
                    f"resolve-g3-real-{index}", approval["store_revision"]
                ),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["advance"]["accepted"])

    @staticmethod
    def _envelope(key: str, revision: int) -> dict[str, object]:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "g3_acceptance",
            "expected_revision": revision,
        }

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

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


if __name__ == "__main__":
    unittest.main()
