from __future__ import annotations

import base64
import io
import json
import tempfile
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.storage.repository import utc_now

from tests.runtime_helpers import build_config_snapshot

ROOT = Path(__file__).resolve().parents[2]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class RealPptAdapterG5Tests(unittest.TestCase):
    def test_confirmed_image_to_workbench_gates_and_zip_export(self) -> None:
        dependency_root = ROOT / ".runtime" / "ppt-agent-deps"
        if not (dependency_root / "html5lib").is_dir():
            self.skipTest("PPT Agent dependencies are not installed; run scripts/dev.py setup.")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_path, model_path = self._mock_configuration(root)
            settings = HarnessSettings(
                control_root=root / "control",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
                image_agent_dependency_root=root / "missing-image-deps",
                ppt_agent_root=ROOT / "agents" / "ppt-agent",
                ppt_agent_lock_path=ROOT / "agents" / "ppt-agent.lock.json",
                ppt_agent_python=Path(__import__("sys").executable).resolve(),
                ppt_agent_dependency_root=dependency_root,
                ppt_agent_runtime_policy=runtime_path,
                ppt_agent_model_config=model_path,
                config_snapshot=build_config_snapshot(
                    supervisor_port_start=19200,
                    supervisor_port_end=19220,
                    supervisor_startup_timeout=20,
                ),
            )
            app = create_app(settings)
            with TestClient(app) as client:
                self._create_and_start(client, root)
                detail = self._wait_for_instance(client, app)
                base_url = detail["ui_url"].rstrip("/")
                work_items = client.get("/api/v1/tasks/t_ppt_smoke/work-items").json()["items"]
                work_item = next(
                    item for item in work_items if "i_ppt_smoke" in item["instance_ids"]
                )
                link = client.get(
                    "/api/v1/instances/i_ppt_smoke/ui-link",
                    params={"task_id": "t_ppt_smoke", "work_item_id": work_item["work_item_id"]},
                )
                self.assertEqual(link.status_code, 200, link.text)
                self.assertEqual(link.json()["link_status"], "READY")
                project = self._drive_all_gates(base_url)
                revision = project["full_deck_revision"]
                status, exported, _ = self._call(base_url, "GET", revision["export_url"])
                self.assertEqual(status, 200)
                self.assertTrue(zipfile.is_zipfile(io.BytesIO(exported)))
                synced_images = (
                    root
                    / "workspace/tasks/t_ppt_smoke/instances/i_ppt_smoke"
                    / "work/projects/i_ppt_smoke/images"
                )
                self.assertEqual(
                    sorted(path.suffix for path in synced_images.iterdir()),
                    [".md", ".png"],
                )
                self.assertEqual(project["state"], "acceptance")
                app.state.container.application.cancel_instance("t_ppt_smoke", "i_ppt_smoke")

    @staticmethod
    def _mock_configuration(root: Path) -> tuple[Path, Path]:
        runtime_path = root / "runtime.yaml"
        runtime = yaml.safe_load(
            (ROOT / "config/ppt_agent_runtime.yaml").read_text(encoding="utf-8")
        )
        runtime["full_deck_batched_generation_enabled"] = False
        runtime_path.write_text(
            yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        model_path = root / "model.yaml"
        model = yaml.safe_load(
            (ROOT / "config/ppt_agent_model_config.yaml").read_text(encoding="utf-8")
        )
        for binding in model["state_bindings"]:
            binding.update(
                provider="mock",
                model="deterministic-preview",
                parameters={},
                fallback_model=None,
                base_url=None,
            )
        model_path.write_text(
            yaml.safe_dump(model, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return runtime_path, model_path

    def _create_and_start(self, client: TestClient, root: Path) -> None:
        created = client.post(
            "/api/v1/tasks",
            json={
                "task_id": "t_ppt_smoke",
                "title": "PPT smoke",
                "goal": "Create a presentation from one confirmed image.",
                "master_owner": "master_default",
                "start_policy": "manual",
                "input_manifest": "inputs/manifests/smoke.json",
                "envelope": self._envelope("create-ppt-smoke", 0),
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        shared = root / "workspace/tasks/t_ppt_smoke/resources/shared"
        shared.mkdir(parents=True, exist_ok=True)
        stem = "bundle_0123456789abcdef0123456789abcdef"
        (shared / f"{stem}.png").write_bytes(PNG)
        (shared / f"{stem}.md").write_text(
            "# 样例图\n\n品牌升级发布会的蓝色科技主视觉。", encoding="utf-8"
        )
        plan = client.put(
            "/api/v1/tasks/t_ppt_smoke/plan",
            json={
                "stages": [
                    {
                        "stage_id": "s_ppt",
                        "task_id": "t_ppt_smoke",
                        "type": "ppt",
                        "position": 1,
                        "depends_on": [],
                        "required": True,
                        "instance_ids": ["i_ppt_smoke"],
                    }
                ],
                "instances": [
                    {
                        "instance_id": "i_ppt_smoke",
                        "task_id": "t_ppt_smoke",
                        "stage_id": "s_ppt",
                        "agent_type": "ppt",
                        "required": True,
                        "approval_mode": "human",
                        "config_revision": 1,
                        "workspace_relpath": "instances/i_ppt_smoke",
                        "task_card_relpath": "instances/i_ppt_smoke/task-card.json",
                    }
                ],
                "task_cards": [
                    {
                        "schema_version": "1.0",
                        "card_id": "card_ppt_smoke",
                        "revision": 1,
                        "task_id": "t_ppt_smoke",
                        "stage_id": "s_ppt",
                        "instance_id": "i_ppt_smoke",
                        "agent_type": "ppt",
                        "objective": "制作品牌升级发布会演示文稿",
                        "instructions": ["使用已确认样例图", "结论先行"],
                        "input_assets": [],
                        "expected_deliveries": [
                            {
                                "kind": "archive",
                                "role": "html_ppt",
                                "required": True,
                                "accepted_mime_types": ["application/zip"],
                            }
                        ],
                        "parameters": {"slide_count": 4},
                        "created_at": utc_now(),
                    }
                ],
                "operation_id": "save-ppt-smoke",
                "envelope": self._envelope("save-ppt-smoke", 1),
            },
        )
        self.assertEqual(plan.status_code, 200, plan.text)
        started = client.post(
            "/api/v1/tasks/t_ppt_smoke/confirm-start",
            json={
                "operation_id": "start-ppt-smoke",
                "envelope": self._envelope("start-ppt-smoke", plan.json()["task_revision"]),
            },
        )
        self.assertEqual(started.status_code, 200, started.text)

    def _wait_for_instance(self, client: TestClient, app) -> dict:
        for _ in range(300):
            instance = client.get("/api/v1/instances/i_ppt_smoke?refresh=false").json()["instance"]
            operation = app.state.container.application.latest_start_operation(
                "t_ppt_smoke", instance_id="i_ppt_smoke"
            )
            if (
                instance["status"] == "RUNNING"
                and instance.get("ui_url")
                and operation is not None
                and operation["state"] == "COMMITTED"
            ):
                return instance
            self.assertNotIn(instance["status"], {"FAILED", "FAILED_TO_START"}, instance)
            time.sleep(0.05)
        self.fail(
            "PPT instance did not become ready: "
            + json.dumps(
                {
                    "instance": instance,
                    "operation": app.state.container.application.latest_start_operation(
                        "t_ppt_smoke"
                    ),
                    "logs": app.state.container.supervisor.log_summary(
                        "t_ppt_smoke", "i_ppt_smoke"
                    ),
                },
                ensure_ascii=False,
            )
        )

    def _drive_all_gates(self, base_url: str) -> dict:
        _, project, _ = self._call(base_url, "GET", "/api/projects/i_ppt_smoke")
        self._job(base_url, "start_clarification", project)
        _, project, _ = self._call(base_url, "GET", "/api/projects/i_ppt_smoke")
        card = project["question_card"]
        _, project, _ = self._call(
            base_url,
            "POST",
            "/api/projects/i_ppt_smoke/clarification",
            {
                "checkpoint_id": project["checkpoint_id"],
                "question_card_id": card["question_card_id"],
                "answers": {
                    item["question_id"]: "管理层，4页"  # noqa: RUF001
                    for item in card["questions"]
                },
            },
        )
        if project.get("active_job"):
            self._wait_job(base_url, project["active_job"]["job_id"])
            _, project, _ = self._call(base_url, "GET", "/api/projects/i_ppt_smoke")
        for operation, document_type in (
            ("generate_narrative", "narrative_structure"),
            ("generate_outline", "slide_outline"),
        ):
            self._job(base_url, operation, project)
            _, project, _ = self._call(base_url, "GET", "/api/projects/i_ppt_smoke")
            document = project["documents"][document_type][-1]
            _, project, _ = self._call(
                base_url,
                "POST",
                f"/api/projects/i_ppt_smoke/documents/{document_type}/approve",
                {
                    "checkpoint_id": project["checkpoint_id"],
                    "revision_hash": document["revision_hash"],
                },
            )
        self._job(base_url, "generate_sample", project)
        _, project, _ = self._call(base_url, "GET", "/api/projects/i_ppt_smoke")
        sample = project["samples"][-1]
        _, project, _ = self._call(
            base_url,
            "POST",
            "/api/projects/i_ppt_smoke/samples/approve",
            {"checkpoint_id": project["checkpoint_id"], "revision_hash": sample["revision_hash"]},
        )
        _, project, _ = self._call(
            base_url,
            "POST",
            "/api/projects/i_ppt_smoke/full-deck/enter",
            {
                "checkpoint_id": project["checkpoint_id"],
                "sample_revision_hash": sample["revision_hash"],
            },
        )
        self._job(base_url, "generate_full_deck", project)
        _, project, _ = self._call(base_url, "GET", "/api/projects/i_ppt_smoke")
        revision = project["full_deck_revision"]
        _, project, _ = self._call(
            base_url,
            "POST",
            "/api/projects/i_ppt_smoke/full-deck/approve",
            {"checkpoint_id": project["checkpoint_id"], "revision_hash": revision["revision_hash"]},
        )
        return project

    def _job(self, base_url: str, operation: str, project: dict) -> None:
        status, payload, _ = self._call(
            base_url,
            "POST",
            "/api/projects/i_ppt_smoke/jobs",
            {"operation": operation, "checkpoint_id": project["checkpoint_id"]},
        )
        self.assertEqual(status, 202)
        self._wait_job(base_url, payload["job_id"])

    def _wait_job(self, base_url: str, job_id: str) -> None:
        for _ in range(400):
            _, job, _ = self._call(base_url, "GET", f"/api/jobs/{job_id}")
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                self.assertEqual(job["status"], "succeeded", job)
                return
            time.sleep(0.05)
        self.fail(f"PPT job {job_id} timed out")

    @staticmethod
    def _call(base_url: str, method: str, path: str, payload: dict | None = None):
        request = urllib.request.Request(
            base_url + path,
            data=None
            if payload is None
            else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            content = response.read()
            if "json" in response.headers.get("content-type", ""):
                content = json.loads(content)
            return response.status, content, dict(response.headers)

    @staticmethod
    def _envelope(key: str, revision: int) -> dict:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "ppt-smoke",
            "expected_revision": revision,
        }
