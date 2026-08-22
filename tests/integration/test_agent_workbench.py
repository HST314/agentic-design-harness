from __future__ import annotations

import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.storage.repository import Actor, utc_now

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def frame_page(
    *,
    x_frame_options: str | None = None,
    content_security_policy: str | None = None,
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"<!doctype html><title>Image Agent</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if x_frame_options:
                self.send_header("X-Frame-Options", x_frame_options)
            if content_security_policy:
                self.send_header("Content-Security-Policy", content_security_policy)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class AgentWorkbenchApiTests(unittest.TestCase):
    def test_link_requires_current_work_item_and_adapter_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            with TestClient(app) as client:
                task_id = "task_agent_workbench"
                instance_id = "instance_agent_workbench"
                self._create_image_plan(client, app, task_id, instance_id)
                projection = client.get(f"/api/v1/tasks/{task_id}/work-items").json()
                work_item_id = projection["items"][0]["work_item_id"]
                route = f"/api/v1/instances/{instance_id}/ui-link"

                missing_context = client.get(route)
                self.assertEqual(missing_context.status_code, 422, missing_context.text)

                no_url = client.get(
                    route,
                    params={"task_id": task_id, "work_item_id": work_item_id},
                )
                self.assertEqual(no_url.status_code, 200, no_url.text)
                self.assertEqual(no_url.json()["link_status"], "NO_UI_URL")

                wrong_task = client.get(
                    route,
                    params={"task_id": "task_other", "work_item_id": work_item_id},
                )
                self.assertEqual(wrong_task.status_code, 404, wrong_task.text)

                with frame_page() as port:
                    self._publish_url(app, task_id, instance_id, port, f"http://127.0.0.1:{port}/")
                    ready = client.get(
                        route,
                        params={"task_id": task_id, "work_item_id": work_item_id},
                    )
                    self.assertEqual(ready.status_code, 200, ready.text)
                    self.assertTrue(ready.json()["embeddable"])
                    self.assertEqual(ready.json()["link_status"], "READY")
                    self.assertEqual(
                        ready.json()["frame_policy"], "FRAME_ANCESTORS_NOT_DECLARED"
                    )

                self._publish_url(
                    app,
                    task_id,
                    instance_id,
                    443,
                    "https://public.example.invalid/",
                )
                rejected = client.get(
                    route,
                    params={"task_id": task_id, "work_item_id": work_item_id},
                )
                self.assertEqual(rejected.status_code, 409, rejected.text)
                self.assertEqual(rejected.json()["error"]["code"], "UI_LINK_REJECTED")

    def test_frame_policy_block_returns_controlled_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._app(Path(temporary))
            with TestClient(app) as client:
                task_id = "task_frame_blocked"
                instance_id = "instance_frame_blocked"
                self._create_image_plan(client, app, task_id, instance_id)
                projection = client.get(f"/api/v1/tasks/{task_id}/work-items").json()
                work_item_id = projection["items"][0]["work_item_id"]
                cases = (
                    ({"x_frame_options": "DENY"}, "X_FRAME_OPTIONS_BLOCKED"),
                    (
                        {"content_security_policy": "frame-ancestors 'none'"},
                        "FRAME_ANCESTORS_BLOCKED",
                    ),
                )
                for page_options, expected_policy in cases:
                    with self.subTest(policy=expected_policy), frame_page(
                        **page_options
                    ) as port:
                        ui_url = f"http://127.0.0.1:{port}/"
                        self._publish_url(app, task_id, instance_id, port, ui_url)
                        response = client.get(
                            f"/api/v1/instances/{instance_id}/ui-link",
                            params={"task_id": task_id, "work_item_id": work_item_id},
                        )
                        self.assertEqual(response.status_code, 200, response.text)
                        payload = response.json()
                        self.assertFalse(payload["embeddable"])
                        self.assertEqual(payload["link_status"], "FRAME_BLOCKED")
                        self.assertEqual(payload["frame_policy"], expected_policy)
                        self.assertEqual(payload["ui_url"], ui_url)

    @staticmethod
    def _app(root: Path):
        return create_app(
            HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
            )
        )

    def _create_image_plan(self, client, app, task_id: str, instance_id: str) -> None:
        created = client.post(
            "/api/v1/tasks",
            json={
                "task_id": task_id,
                "title": "F4 Image workbench",
                "goal": "Open only the current trusted Image Agent instance.",
                "master_owner": "master_default",
                "start_policy": "manual",
                "input_manifest": "inputs/manifests/input.json",
                "envelope": self._envelope(f"create-{task_id}", 0),
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        app.state.container.credentials.configure_pool(
            [
                {
                    "credential_pair_id": f"cred_{task_id}",
                    "provider": "fake",
                    "key_id": f"key_{task_id}",
                    "base_url": "https://provider.invalid/v1",
                    "api_key": "not-a-secret-agent-workbench-test",
                    "api_key_env": "FAKE_API_KEY",
                    "base_url_env": "FAKE_BASE_URL",
                    "revision": 1,
                    "enabled": True,
                }
            ]
        )
        imported = app.state.container.assets.import_bytes(
            task_id,
            filename="brief.md",
            content=b"# F4 workbench brief\n",
            description="Use as the verified workbench input.",
            source="test",
            idempotency_key=f"import-{task_id}",
        )
        selected = app.state.container.assets.select_inputs(
            task_id, [imported["asset_id"]], manifest_id=f"manifest-{task_id}"
        )
        created_at = utc_now()
        response = client.put(
            f"/api/v1/tasks/{task_id}/plan",
            json={
                "stages": [
                    {
                        "stage_id": f"stage_{task_id}",
                        "task_id": task_id,
                        "type": "image",
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
                        "stage_id": f"stage_{task_id}",
                        "agent_type": "image",
                        "required": True,
                        "approval_mode": "human",
                        "config_revision": 1,
                        "credential_pair_ref": "pending_assignment",
                        "credential_pair_revision": 1,
                        "workspace_relpath": f"instances/{instance_id}",
                        "task_card_relpath": f"instances/{instance_id}/task-card.json",
                    }
                ],
                "task_cards": [
                    {
                        "schema_version": "1.1",
                        "card_id": f"card_{task_id}",
                        "revision": 1,
                        "task_id": task_id,
                        "stage_id": f"stage_{task_id}",
                        "instance_id": instance_id,
                        "agent_type": "image",
                        "objective": "Create an approved visual direction.",
                        "instructions": ["Use the verified brief."],
                        "input_assets": selected["task_card_inputs"],
                        "expected_deliveries": [
                            {
                                "kind": "image",
                                "role": "final_artwork",
                                "required": True,
                                "accepted_mime_types": ["image/png"],
                            }
                        ],
                        "parameters": {"usage_context": "F4 integration test"},
                        "created_at": created_at,
                    }
                ],
                "providers": {instance_id: "fake"},
                "operation_id": f"save_{task_id}",
                "envelope": self._envelope(f"save-{task_id}", 1),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    @staticmethod
    def _publish_url(app, task_id: str, instance_id: str, port: int, ui_url: str) -> None:
        app.state.container.store.update_instance_fields(
            task_id,
            instance_id,
            {
                "process": {
                    "pid": 12345,
                    "port": port,
                    "launch_id": f"launch_{instance_id}",
                    "state": "RUNNING",
                    "started_at": utc_now(),
                },
                "ui_url": ui_url,
            },
            actor=Actor("system", "test_workbench"),
            command="test_publish_ui_url",
            idempotency_key=f"publish-{instance_id}-{port}",
        )

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
