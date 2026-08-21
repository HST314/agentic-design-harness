from __future__ import annotations

import os
import signal
import stat
import tempfile
import time
import unittest
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.services.process_runtime import process_start_identity
from harness.storage.repository import utc_now

from tests.e2e import test_g3_real_image_agent as g3_fixtures

ROOT = Path(__file__).resolve().parents[2]
IMAGE_AGENT_ROOT = os.getenv("HARNESS_IMAGE_AGENT_ROOT")
IMAGE_AGENT_PYTHON = os.getenv("HARNESS_IMAGE_AGENT_PYTHON")
IMAGE_AGENT_DEPENDENCY_ROOT = os.getenv("HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT")


@unittest.skipUnless(
    IMAGE_AGENT_ROOT and IMAGE_AGENT_PYTHON and IMAGE_AGENT_DEPENDENCY_ROOT,
    "set all HARNESS_IMAGE_AGENT_* runtime paths for the G4 real multi-instance gate",
)
class RealMultiImageAgentG4Tests(unittest.TestCase):
    task_id = "t_g4_multi_image"
    instance_ids = ("i_g4_image_1", "i_g4_image_2", "i_g4_image_3")

    def test_three_real_processes_complete_and_publish_verified_deliveries(self) -> None:
        with (
            g3_fixtures.deterministic_provider() as (provider_url, _provider),
            tempfile.TemporaryDirectory() as temporary,
        ):
            runtime = Path(temporary)
            app = create_app(self._settings(runtime))
            app.state.container.configuration.initialize(
                g3_fixtures.RealImageAdapterG3Tests._online_config()
            )
            try:
                with TestClient(app) as client:
                    container = app.state.container
                    container.credentials.configure_pool(
                        self._credential_pairs(provider_url=provider_url)
                    )
                    self._create_task(client, self.task_id, "create-g4-complete")
                    inputs = self._import_brief(container, self.task_id)
                    saved = client.put(
                        f"/api/v1/tasks/{self.task_id}/plan",
                        json=self._plan_request(
                            self.task_id,
                            self.instance_ids,
                            inputs,
                            provider="ark",
                        ),
                    )
                    self.assertEqual(saved.status_code, 200, saved.text)
                    self.assertEqual(len(saved.json()["plan"]["task_cards"]), 3)
                    started = client.post(
                        f"/api/v1/tasks/{self.task_id}/confirm-start",
                        json={
                            "operation_id": "start_g4_complete",
                            "envelope": self._envelope(
                                "start-g4-complete", saved.json()["task_revision"]
                            ),
                        },
                    )
                    self.assertEqual(started.status_code, 200, started.text)
                    self.assertEqual(len(started.json()["launches"]), 3)
                    boundaries = {
                        instance_id: self._wait_for_boundary(client, instance_id)
                        for instance_id in self.instance_ids
                    }
                    processes = [
                        detail["instance"]["process"] for detail in boundaries.values()
                    ]
                    self.assertEqual(len({item["pid"] for item in processes}), 3)
                    self.assertEqual(len({item["port"] for item in processes}), 3)

                    self._complete_all_instances(client)

                    task = client.get(f"/api/v1/tasks/{self.task_id}").json()["task"]
                    self.assertEqual(task["status"], "SUCCEEDED")
                    final_instances = [
                        client.get(f"/api/v1/instances/{instance_id}").json()[
                            "instance"
                        ]
                        for instance_id in self.instance_ids
                    ]
                    self.assertEqual(
                        [item["status"] for item in final_instances],
                        ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"],
                    )
                    resources = client.get(
                        f"/api/v1/tasks/{self.task_id}/files?group=shared"
                    ).json()["assets"]
                    published = [
                        item
                        for item in resources
                        if item["manifest"].get("producer_instance_id")
                        in self.instance_ids
                    ]
                    self.assertEqual(len(published), 3)
                    self.assertTrue(
                        all(item["integrity_status"] == "VERIFIED" for item in published)
                    )
                    self.assertEqual(
                        {item["manifest"]["producer_instance_id"] for item in published},
                        set(self.instance_ids),
                    )
            finally:
                self._cleanup_instances(app, self.task_id, self.instance_ids)
                self._make_tree_removable(runtime)

    def test_process_loss_cancel_peer_and_control_plane_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            settings = self._settings(runtime)
            first_app = create_app(settings)
            recovered_job_ids: dict[str, str] = {}
            victim_process: dict[str, object] | None = None
            try:
                with TestClient(first_app) as client:
                    container = first_app.state.container
                    container.credentials.configure_pool(self._credential_pairs())
                    self._create_task(client, self.task_id, "create-g4-multi")
                    inputs = self._import_brief(container, self.task_id)
                    saved = client.put(
                        f"/api/v1/tasks/{self.task_id}/plan",
                        json=self._plan_request(self.task_id, self.instance_ids, inputs),
                    )
                    self.assertEqual(saved.status_code, 200, saved.text)
                    assignments = [
                        container.store.instance.get(self.task_id, instance_id)[
                            "credential_pair_ref"
                        ]
                        for instance_id in self.instance_ids
                    ]
                    self.assertEqual(assignments, ["cred_g4_1", "cred_g4_2", "cred_g4_3"])

                    self._create_task(client, "t_g4_cycle", "create-g4-cycle")
                    cycle_inputs = self._import_brief(container, "t_g4_cycle")
                    cycled = client.put(
                        "/api/v1/tasks/t_g4_cycle/plan",
                        json=self._plan_request(
                            "t_g4_cycle", ("i_g4_cycle_1",), cycle_inputs
                        ),
                    )
                    self.assertEqual(cycled.status_code, 200, cycled.text)
                    cycle_instance = container.store.instance.get(
                        "t_g4_cycle", "i_g4_cycle_1"
                    )
                    self.assertEqual(cycle_instance["credential_pair_ref"], "cred_g4_1")

                    started = client.post(
                        f"/api/v1/tasks/{self.task_id}/confirm-start",
                        json={
                            "operation_id": "start_g4_multi",
                            "envelope": self._envelope(
                                "start-g4-multi", saved.json()["task_revision"]
                            ),
                        },
                    )
                    self.assertEqual(started.status_code, 200, started.text)
                    self.assertEqual(len(started.json()["launches"]), 3)
                    details = {
                        instance_id: self._wait_for_boundary(client, instance_id)
                        for instance_id in self.instance_ids
                    }
                    processes = [item["instance"]["process"] for item in details.values()]
                    self.assertEqual(len({item["pid"] for item in processes}), 3)
                    self.assertEqual(len({item["port"] for item in processes}), 3)
                    for instance_id, detail in details.items():
                        self.assertEqual(detail["instance"]["status"], "WAITING_APPROVAL")
                        instance_root = (
                            runtime
                            / "workspace"
                            / "tasks"
                            / self.task_id
                            / "instances"
                            / instance_id
                        )
                        self.assertTrue((instance_root / "runtime").is_dir())
                        self.assertTrue((instance_root / "work" / instance_id).is_dir())
                        recovered_job_ids[instance_id] = detail["observation"]["details"][
                            "job_id"
                        ]

                    local = client.get(
                        f"/api/v1/instances/{self.instance_ids[0]}/config"
                    ).json()["config"]
                    local_update = client.put(
                        f"/api/v1/instances/{self.instance_ids[0]}/config",
                        json={
                            "patch": {
                                "image_runtime_policy": {"max_auto_questions": 3}
                            },
                            "operation_id": "local-g4-config",
                            "envelope": self._envelope(
                                "local-g4-config-envelope", local["config_revision"]
                            ),
                        },
                    )
                    self.assertEqual(local_update.status_code, 200, local_update.text)
                    global_config = client.get("/api/v1/config/global").json()["config"]
                    global_revision = global_config.pop("revision")
                    updated_global = deepcopy(global_config)
                    updated_global["image_runtime_policy"]["max_auto_questions"] = 5
                    overwritten = client.put(
                        "/api/v1/config/global",
                        json={
                            "config": updated_global,
                            "operation_id": "global-g4-config",
                            "envelope": self._envelope(
                                "global-g4-config-envelope", global_revision
                            ),
                        },
                    )
                    self.assertEqual(overwritten.status_code, 200, overwritten.text)
                    for instance_id in self.instance_ids:
                        snapshot = client.get(
                            f"/api/v1/instances/{instance_id}/config"
                        ).json()["config"]
                        self.assertEqual(snapshot["source_global_revision"], global_revision + 1)
                        self.assertEqual(
                            snapshot["config"]["image_runtime_policy"][
                                "max_auto_questions"
                            ],
                            5,
                        )
                        self.assertFalse(snapshot["restart_required"])
                    usage = client.get(
                        f"/api/v1/tasks/{self.task_id}/usage"
                    ).json()
                    self.assertEqual(usage["completeness"], "NOT_REPORTED")
                    self.assertEqual(
                        [item["completeness"] for item in usage["instances"]],
                        ["NOT_REPORTED", "NOT_REPORTED", "NOT_REPORTED"],
                    )
                    # Freeze the launch record as RUNNING, then kill the real
                    # process group while monitoring is stopped. The next
                    # control-plane process must be the component that detects it.
                    victim_process = details[self.instance_ids[0]]["instance"][
                        "process"
                    ]
                    container.supervisor.close()
                    os.killpg(int(victim_process["pid"]), signal.SIGKILL)
                    self._wait_for_process_loss(int(victim_process["pid"]))

                recovered_app = create_app(settings)
                with TestClient(recovered_app) as recovered_client:
                    victim = recovered_client.get(
                        f"/api/v1/instances/{self.instance_ids[0]}"
                    ).json()
                    self.assertEqual(victim["instance"]["status"], "FAILED")
                    self.assertEqual(victim["instance"]["process"]["state"], "EXITED")
                    for instance_id in self.instance_ids[1:]:
                        detail = recovered_client.get(
                            f"/api/v1/instances/{instance_id}"
                        ).json()
                        self.assertEqual(detail["instance"]["status"], "WAITING_APPROVAL")
                        self.assertEqual(
                            detail["observation"]["details"]["job_id"],
                            recovered_job_ids[instance_id],
                        )
                    task_revision = recovered_client.get(
                        f"/api/v1/tasks/{self.task_id}"
                    ).json()["task_revision"]
                    cancelled = recovered_client.post(
                        f"/api/v1/instances/{self.instance_ids[1]}/cancel",
                        json={
                            "operation_id": "cancel-g4-image-2",
                            "envelope": self._envelope(
                                "cancel-g4-image-2-envelope", task_revision
                            ),
                        },
                    )
                    self.assertEqual(cancelled.status_code, 200, cancelled.text)
                    self.assertEqual(cancelled.json()["instance"]["status"], "CANCELLED")
                    survivor = recovered_client.get(
                        f"/api/v1/instances/{self.instance_ids[2]}"
                    ).json()
                    self.assertEqual(survivor["instance"]["status"], "WAITING_APPROVAL")
                    self.assertEqual(
                        survivor["observation"]["details"]["job_id"],
                        recovered_job_ids[self.instance_ids[2]],
                    )
            finally:
                active_app = locals().get("recovered_app", first_app)
                self._cleanup_instances(active_app, self.task_id, self.instance_ids)
                self._make_tree_removable(runtime)

    @staticmethod
    def _credential_pairs(
        *, provider_url: str | None = None
    ) -> list[dict[str, object]]:
        provider = "ark" if provider_url is not None else "fake"
        return [
            {
                "credential_pair_id": f"cred_g4_{index}",
                "provider": provider,
                "key_id": f"key_g4_{index}",
                "base_url": provider_url or f"https://offline-{index}.invalid/v1",
                "api_key": f"synthetic-offline-g4-{index}",
                "api_key_env": "ARK_API_KEY" if provider_url else "FAKE_API_KEY",
                "base_url_env": "ARK_BASE_URL" if provider_url else "FAKE_BASE_URL",
                "revision": 1,
                "enabled": True,
            }
            for index in range(1, 4)
        ]

    @staticmethod
    def _create_task(client: TestClient, task_id: str, key: str) -> None:
        response = client.post(
            "/api/v1/tasks",
            json={
                "task_id": task_id,
                "title": f"G4 multi-instance gate {task_id}",
                "goal": "Verify real isolated Image processes and G4 control-plane behavior.",
                "master_owner": "master_default",
                "start_policy": "manual",
                "input_manifest": f"inputs/manifests/{task_id}.json",
                "envelope": RealMultiImageAgentG4Tests._envelope(key, 0),
            },
        )
        if response.status_code != 200:
            raise AssertionError(response.text)

    @staticmethod
    def _import_brief(container, task_id: str) -> list[dict[str, str]]:
        imported = container.assets.import_bytes(
            task_id,
            filename="brief.md",
            content=b"# Three offline posters\nUse the registered brief only.\n",
            description="Approved G4 multi-instance brief",
            source="g4_acceptance",
            idempotency_key="import-g4-multi-brief",
        )
        return container.assets.select_inputs(
            task_id, [imported["asset_id"]], manifest_id="g4-multi-inputs"
        )["task_card_inputs"]

    @staticmethod
    def _plan_request(
        task_id: str,
        instance_ids: tuple[str, ...],
        inputs: list[dict[str, str]],
        *,
        provider: str = "fake",
    ) -> dict[str, object]:
        return {
            "stages": [
                {
                    "stage_id": "s_image",
                    "task_id": task_id,
                    "type": "image",
                    "position": 1,
                    "depends_on": [],
                    "required": True,
                    "instance_ids": list(instance_ids),
                }
            ],
            "instances": [
                {
                    "instance_id": instance_id,
                    "task_id": task_id,
                    "stage_id": "s_image",
                    "agent_type": "image",
                    "required": True,
                    "approval_mode": "human",
                    "config_revision": 1,
                    "credential_pair_ref": "pending_assignment",
                    "credential_pair_revision": 1,
                    "workspace_relpath": f"instances/{instance_id}",
                    "task_card_relpath": f"instances/{instance_id}/task-card.json",
                }
                for instance_id in instance_ids
            ],
            "task_cards": [
                {
                    "schema_version": "1.1",
                    "card_id": f"card_{instance_id}",
                    "revision": 1,
                    "task_id": task_id,
                    "stage_id": "s_image",
                    "instance_id": instance_id,
                    "agent_type": "image",
                    "objective": f"Create offline poster {instance_id}.",
                    "instructions": ["Use only the approved brief."],
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
                        "usage_context": "G4 offline acceptance",
                    },
                    "created_at": utc_now(),
                }
                for instance_id in instance_ids
            ],
            "providers": {instance_id: provider for instance_id in instance_ids},
            "operation_id": f"save_{task_id}_plan",
            "envelope": RealMultiImageAgentG4Tests._envelope(
                f"save-{task_id}-plan", 1
            ),
        }

    @staticmethod
    def _wait_for_boundary(client: TestClient, instance_id: str) -> dict:
        deadline = time.monotonic() + 30
        detail = None
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/instances/{instance_id}")
            if response.status_code != 200:
                raise AssertionError(response.text)
            detail = response.json()
            if detail["instance"]["status"] in {"WAITING_APPROVAL", "FAILED"}:
                return detail
            time.sleep(0.1)
        raise AssertionError(f"{instance_id} did not reach a workflow boundary: {detail}")

    def _complete_all_instances(self, client: TestClient) -> None:
        pending = set(self.instance_ids)
        steps = {instance_id: 0 for instance_id in self.instance_ids}
        deadline = time.monotonic() + 180
        priority = (
            "approve_taskbook",
            "approve_category_constraint",
            "approve_skill_invocations",
            "select_master",
            "review_calibration",
            "approve_final",
            "start_category_match",
            "start_clarification",
            "build_taskbook",
            "prepare_style_direction",
            "render_candidates",
            "choose_master",
            "start_quality_inspection",
            "open_final_approval",
        )
        while pending and time.monotonic() < deadline:
            for instance_id in tuple(sorted(pending)):
                detail = client.get(f"/api/v1/instances/{instance_id}").json()
                status = detail["instance"]["status"]
                self.assertNotEqual(status, "FAILED", detail)
                if status == "SUCCEEDED":
                    pending.remove(instance_id)
                    continue
                if status != "WAITING_APPROVAL":
                    continue
                approval = detail["pending_approval"]
                approval_details = client.get(
                    f"/api/v1/approvals/{approval['approval_id']}"
                ).json()
                actions = approval_details["payload"]["available_actions"]
                action = next((item for item in priority if item in actions), None)
                self.assertIsNotNone(action, approval_details)
                payload: dict[str, object] = {}
                if action == "select_master":
                    candidate = approval_details["payload"]["context"]["candidates"][0]
                    payload["selected_id"] = candidate.get("id") or candidate[
                        "candidate_id"
                    ]
                elif action == "review_calibration":
                    payload["manual_action"] = "accept_current"
                steps[instance_id] += 1
                response = client.post(
                    f"/api/v1/approvals/{approval['approval_id']}/resolve",
                    json={
                        "decision": "APPROVED",
                        "action": action,
                        "payload": payload,
                        "operation_id": (
                            f"resolve_{instance_id}_{steps[instance_id]}"
                        ),
                        "envelope": self._envelope(
                            f"resolve-{instance_id}-{steps[instance_id]}",
                            approval["store_revision"],
                        ),
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["advance"]["accepted"])
            time.sleep(0.05)
        self.assertEqual(pending, set(), f"instances did not complete: {sorted(pending)}")

    @staticmethod
    def _settings(runtime: Path) -> HarnessSettings:
        return HarnessSettings(
            control_root=runtime / "control-data",
            workspace_root=runtime / "workspace",
            contracts_root=ROOT / "contracts" / "v1",
            image_agent_root=Path(str(IMAGE_AGENT_ROOT)),
            image_agent_python=Path(str(IMAGE_AGENT_PYTHON)),
            image_agent_dependency_root=Path(str(IMAGE_AGENT_DEPENDENCY_ROOT)),
        )

    @staticmethod
    def _wait_for_process_loss(pid: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process_start_identity(pid) is not None:
            time.sleep(0.02)
        if process_start_identity(pid) is not None:
            raise AssertionError(f"process {pid} did not exit after SIGKILL")

    @staticmethod
    def _cleanup_instances(app, task_id: str, instance_ids: tuple[str, ...]) -> None:
        for instance_id in instance_ids:
            instance = app.state.container.store.instance.get(task_id, instance_id)
            if instance is None or instance["status"] == "ARCHIVED":
                continue
            with suppress(Exception):
                if instance["status"] == "SUCCEEDED":
                    app.state.container.application.archive_instance(
                        task_id, instance_id
                    )
                else:
                    app.state.container.application.cancel_instance(
                        task_id, instance_id
                    )

    @staticmethod
    def _envelope(key: str, revision: int) -> dict[str, object]:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "g4_acceptance",
            "expected_revision": revision,
        }

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
