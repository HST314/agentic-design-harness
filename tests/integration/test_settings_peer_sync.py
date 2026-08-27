"""Phase G: per-instance sync_to_peers fan-out and task settings broadcast."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from harness.adapters import AdapterRegistry
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.core.errors import HarnessError
from harness.services.agent_config_materialization import ImageAgentConfigMaterializer
from harness.services.instance_runtime_settings import InstanceRuntimeSettingsService
from harness.services.runtime_config_observability import RuntimeConfigObservability
from harness.services.system_settings import SystemSettingsService
from harness.services.task_config import TaskConfigService
from harness.services.task_config_rebase import TaskConfigRebaseService
from runtime_helpers import (
    build_config_snapshot,
    build_service,
    create_task,
    envelope,
    image_plan,
)

from integration.test_runtime_settings_control_plane import SafePointImageAdapter

ROOT = Path(__file__).resolve().parents[2]


class PeerSyncCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        self.task_config = TaskConfigService(self.store, build_config_snapshot())
        self.materializer = ImageAgentConfigMaterializer(self.store, self.task_config)
        self.adapter = SafePointImageAdapter()
        self.adapters = AdapterRegistry([self.adapter])
        self.observability = RuntimeConfigObservability(self.store)
        self.settings = InstanceRuntimeSettingsService(
            self.store,
            self.task_config,
            self.materializer,
            self.adapters,
            self.observability,
        )
        self.rebase = TaskConfigRebaseService(
            self.store,
            self.task_config,
            self.materializer,
            self.observability,
        )

    def tearDown(self) -> None:
        self.store.close()
        for path in sorted(self.root.rglob("*"), reverse=True):
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)
        self.temporary.cleanup()

    def _planned_task(self, task_id: str, *, count: int = 1) -> str:
        created = create_task(self.commands, task_id)
        self.task_config.pin(task_id)
        draft = image_plan(task_id, count=count)
        self.commands.save_plan(
            task_id,
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope(f"save-{task_id}", created["revision"]),
        )
        return task_id

    def _settings_envelope(self, task_id: str, key: str):
        return envelope(key, self.store.task.revision(task_id, task_id))

    def _start_instance_projection(self, task_id: str, instance_id: str) -> None:
        current = self.store.task.revision(task_id, task_id)
        self.commands.confirm_start(task_id, envelope(f"confirm-{task_id}", current))
        for status in ("STARTING", "RUNNING"):
            current = self.store.task.revision(task_id, task_id)
            self.commands.transition_instance(
                task_id,
                instance_id,
                status,
                envelope(f"{status.lower()}-{task_id}", current, actor_type="adapter"),
            )

    def _work_item_id(self, task_id: str, instance_id: str) -> str:
        return self.settings.get(task_id, instance_id)["scope"]["work_item_id"]

    def _enable_sync(self, task_id: str, instance_id: str) -> dict:
        return self.settings.set_sync_to_peers(
            task_id,
            self._work_item_id(task_id, instance_id),
            sync_to_peers=True,
            envelope=self._settings_envelope(task_id, f"toggle-{instance_id}"),
        )

    def _propose_and_confirm(
        self, task_id: str, instance_id: str, patch: dict, key: str
    ) -> dict:
        current = self.settings.get(task_id, instance_id)
        proposal = self.settings.propose(
            task_id,
            instance_id,
            base_revision=current["revision"]["current"],
            patch=patch,
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, f"propose-{key}"),
        )
        return self.settings.confirm(
            task_id,
            instance_id,
            proposal["proposal_id"],
            envelope=self._settings_envelope(task_id, f"confirm-{key}"),
        )

    def _current_revision(self, task_id: str, instance_id: str) -> int:
        current = self.materializer.revisions.read_current(task_id, instance_id)
        if current is None:
            self.materializer.materialize(task_id, instance_id)
            current = self.materializer.revisions.read_current(task_id, instance_id)
        return int(current["state"]["revision"])


class PeerSyncToggleTests(PeerSyncCase):
    def test_toggle_persists_and_projects_into_runtime_settings(self) -> None:
        task_id = self._planned_task("task_peer_toggle", count=2)
        before = self.settings.get(task_id, "i_image_1")
        self.assertFalse(before["sync_to_peers"])
        self.assertEqual(
            before["sync_peers"],
            [
                {
                    "instance_id": "i_image_2",
                    "work_item_id": self._work_item_id(task_id, "i_image_2"),
                    "started": False,
                }
            ],
        )

        work_item_id = before["scope"]["work_item_id"]
        toggled = self.settings.set_sync_to_peers(
            task_id,
            work_item_id,
            sync_to_peers=True,
            envelope=self._settings_envelope(task_id, "toggle-on"),
        )
        self.assertTrue(toggled["sync_to_peers"])
        self.assertEqual(toggled["instance_id"], "i_image_1")
        self.assertEqual(toggled["work_item_id"], work_item_id)
        self.assertEqual(len(toggled["sync_peers"]), 1)

        after = self.settings.get(task_id, "i_image_1")
        self.assertTrue(after["sync_to_peers"])

        off = self.settings.set_sync_to_peers(
            task_id,
            work_item_id,
            sync_to_peers=False,
            envelope=self._settings_envelope(task_id, "toggle-off"),
        )
        self.assertFalse(off["sync_to_peers"])
        self.assertFalse(self.settings.get(task_id, "i_image_1")["sync_to_peers"])

    def test_toggle_rejects_unknown_work_item(self) -> None:
        task_id = self._planned_task("task_peer_toggle_missing", count=1)
        with self.assertRaises(HarnessError) as raised:
            self.settings.set_sync_to_peers(
                task_id,
                "work_missing",
                sync_to_peers=True,
                envelope=self._settings_envelope(task_id, "toggle-missing"),
            )
        self.assertEqual(raised.exception.code, "TASK_NOT_FOUND")

    def test_toggle_rejects_system_actor(self) -> None:
        task_id = self._planned_task("task_peer_toggle_actor", count=1)
        with self.assertRaises(HarnessError) as raised:
            self.settings.set_sync_to_peers(
                task_id,
                self._work_item_id(task_id, "i_image_1"),
                sync_to_peers=True,
                envelope=envelope(
                    "toggle-system",
                    self.store.task.revision(task_id, task_id),
                    actor_type="system",
                ),
            )
        self.assertEqual(raised.exception.code, "VALIDATION_ERROR")


class PeerSyncFanOutTests(PeerSyncCase):
    def test_confirmed_overrides_fan_out_to_unstarted_peer(self) -> None:
        task_id = self._planned_task("task_peer_fanout", count=2)
        self._enable_sync(task_id, "i_image_1")
        self.assertEqual(self._current_revision(task_id, "i_image_2"), 1)

        result = self._propose_and_confirm(
            task_id,
            "i_image_1",
            {"candidate_concurrency": 2, "watermark": True},
            "fanout",
        )

        self.assertEqual(result["status"], "APPLIED_BEFORE_START")
        peer_sync = result["peer_sync"]
        self.assertEqual(peer_sync["updated"], 1, peer_sync)
        self.assertEqual(peer_sync["failed"], 0)
        self.assertEqual(
            peer_sync["items"],
            [
                {
                    "instance_id": "i_image_2",
                    "status": "APPLIED_BEFORE_START",
                    "revision_id": peer_sync["items"][0]["revision_id"],
                    "branch_id": None,
                }
            ],
        )
        peer = self.materializer.revisions.read_current(task_id, "i_image_2")
        self.assertEqual(peer["manifest"]["overrides"]["candidate_concurrency"], 2)
        self.assertTrue(peer["manifest"]["overrides"]["watermark"])
        self.assertEqual(self._current_revision(task_id, "i_image_2"), 2)
        peer_instance = self.store.instance.get(task_id, "i_image_2")
        self.assertEqual(peer_instance["config_revision"], 2)

    def test_toggle_off_leaves_peer_untouched(self) -> None:
        task_id = self._planned_task("task_peer_disabled", count=2)
        result = self._propose_and_confirm(
            task_id,
            "i_image_1",
            {"candidate_concurrency": 2},
            "disabled",
        )
        self.assertEqual(result["status"], "APPLIED_BEFORE_START")
        self.assertNotIn("peer_sync", result)
        self.assertEqual(self._current_revision(task_id, "i_image_2"), 1)

    def test_fan_out_reaches_running_peer_at_safe_checkpoint(self) -> None:
        task_id = self._planned_task("task_peer_running", count=2)
        self._start_instance_projection(task_id, "i_image_2")
        peers = self.settings.get(task_id, "i_image_1")["sync_peers"]
        self.assertEqual(peers[0]["started"], True)
        self._enable_sync(task_id, "i_image_1")

        result = self._propose_and_confirm(
            task_id,
            "i_image_1",
            {"candidate_concurrency": 3},
            "running-peer",
        )

        peer_sync = result["peer_sync"]
        self.assertEqual(peer_sync["failed"], 0, peer_sync)
        (item,) = peer_sync["items"]
        self.assertEqual(item["instance_id"], "i_image_2")
        self.assertEqual(item["status"], "APPLIED_ON_BRANCH")
        self.assertGreaterEqual(self.adapter.apply_calls, 1)
        peer = self.materializer.revisions.read_current(task_id, "i_image_2")
        self.assertEqual(peer["manifest"]["overrides"]["candidate_concurrency"], 3)
        peer_instance = self.store.instance.get(task_id, "i_image_2")
        self.assertEqual(peer_instance["config_revision"], 2)

    def test_fan_out_is_idempotent_under_confirm_replay(self) -> None:
        task_id = self._planned_task("task_peer_replay", count=2)
        self._enable_sync(task_id, "i_image_1")
        current = self.settings.get(task_id, "i_image_1")
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=current["revision"]["current"],
            patch={"watermark": True},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, "propose-replay"),
        )
        confirm_envelope = self._settings_envelope(task_id, "confirm-replay")
        first = self.settings.confirm(
            task_id,
            "i_image_1",
            proposal["proposal_id"],
            envelope=confirm_envelope,
        )
        self.assertEqual(first["peer_sync"]["updated"], 1)
        revision_after_first = self._current_revision(task_id, "i_image_2")

        replay = self.settings.confirm(
            task_id,
            "i_image_1",
            proposal["proposal_id"],
            envelope=confirm_envelope,
        )

        self.assertEqual(replay["status"], first["status"])
        self.assertEqual(
            self._current_revision(task_id, "i_image_2"), revision_after_first
        )
        self.assertEqual(replay["peer_sync"]["updated"], 1, replay["peer_sync"])

    def test_fan_out_skips_terminal_peer(self) -> None:
        task_id = self._planned_task("task_peer_terminal", count=2)
        self._start_instance_projection(task_id, "i_image_2")
        current_revision = self.store.task.revision(task_id, task_id)
        self.commands.transition_instance(
            task_id,
            "i_image_2",
            "SUCCEEDED",
            envelope("complete-peer", current_revision, actor_type="adapter"),
        )
        self._enable_sync(task_id, "i_image_1")

        result = self._propose_and_confirm(
            task_id,
            "i_image_1",
            {"watermark": True},
            "terminal-peer",
        )

        peer_sync = result["peer_sync"]
        self.assertEqual(peer_sync["completed_history_unchanged"], 1, peer_sync)
        self.assertEqual(peer_sync["updated"], 0)
        self.materializer.materialize(task_id, "i_image_2")
        peer = self.materializer.revisions.read_current(task_id, "i_image_2")
        self.assertNotIn("watermark", peer["manifest"]["overrides"])

    def test_peer_already_matching_reports_unchanged(self) -> None:
        task_id = self._planned_task("task_peer_unchanged", count=2)
        # Give the peer the same override first, without any sync toggle.
        self._propose_and_confirm(
            task_id, "i_image_2", {"candidate_concurrency": 2}, "peer-preset"
        )
        self._enable_sync(task_id, "i_image_1")
        revision_before = self._current_revision(task_id, "i_image_2")

        result = self._propose_and_confirm(
            task_id,
            "i_image_1",
            {"candidate_concurrency": 2},
            "source-match",
        )

        peer_sync = result["peer_sync"]
        self.assertEqual(peer_sync["unchanged"], 1, peer_sync)
        self.assertEqual(peer_sync["updated"], 0)
        self.assertEqual(
            self._current_revision(task_id, "i_image_2"), revision_before
        )


class TaskSettingsBroadcastTests(PeerSyncCase):
    def _service(self, candidate) -> SystemSettingsService:
        self.task_config.process_snapshot = candidate
        return SystemSettingsService(
            self.root,
            HarnessSettings(config_snapshot=candidate),
            self.store,
            self.task_config,
            self.materializer,
            self.settings,
            self.rebase,
        )

    @staticmethod
    def _drifted_snapshot(concurrency: int):
        current = build_config_snapshot()
        image = current.runtime.image_agent.model_copy(
            update={"candidate_concurrency": concurrency}
        )
        return current.model_copy(
            update={
                "revision": f"cfg_broadcast_{concurrency}",
                "runtime": current.runtime.model_copy(
                    update={"image_agent": image}
                ),
            }
        )

    def test_broadcast_updates_every_image_instance_of_the_task(self) -> None:
        task_id = self._planned_task("task_broadcast_all", count=2)
        candidate = self._drifted_snapshot(2)
        service = self._service(candidate)

        result = service.broadcast_task(
            task_id, actor={"actor_type": "human", "actor_id": "broadcast_test"}
        )

        self.assertEqual(result["updated"], 2, result)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(
            {item["instance_id"] for item in result["items"]},
            {"i_image_1", "i_image_2"},
        )
        for instance_id in ("i_image_1", "i_image_2"):
            current = self.materializer.revisions.read_current(task_id, instance_id)
            self.assertEqual(
                current["manifest"]["effective_runtime"]["candidate_concurrency"], 2
            )
            self.assertEqual(int(current["state"]["revision"]), 2)
            instance = self.store.instance.get(task_id, instance_id)
            self.assertEqual(instance["config_revision"], 2)

    def test_broadcast_branches_running_instance_at_safe_checkpoint(self) -> None:
        task_id = self._planned_task("task_broadcast_running", count=1)
        self._start_instance_projection(task_id, "i_image_1")
        candidate = self._drifted_snapshot(2)
        service = self._service(candidate)

        result = service.broadcast_task(
            task_id, actor={"actor_type": "human", "actor_id": "broadcast_test"}
        )

        self.assertEqual(result["updated"], 1, result)
        (item,) = result["items"]
        self.assertEqual(item["status"], "APPLIED_ON_BRANCH")
        self.assertEqual(self.adapter.apply_calls, 1)

    def test_broadcast_preserves_completed_instance_history(self) -> None:
        task_id = self._planned_task("task_broadcast_completed", count=1)
        self._start_instance_projection(task_id, "i_image_1")
        current_revision = self.store.task.revision(task_id, task_id)
        self.commands.transition_instance(
            task_id,
            "i_image_1",
            "SUCCEEDED",
            envelope("complete-broadcast", current_revision, actor_type="adapter"),
        )
        candidate = self._drifted_snapshot(2)
        service = self._service(candidate)

        result = service.broadcast_task(
            task_id, actor={"actor_type": "human", "actor_id": "broadcast_test"}
        )

        self.assertEqual(result["completed_history_unchanged"], 1, result)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(self.adapter.apply_calls, 0)

    def test_broadcast_is_a_no_op_when_instances_already_match(self) -> None:
        task_id = self._planned_task("task_broadcast_noop", count=2)
        candidate = self._drifted_snapshot(5)
        service = self._service(candidate)

        result = service.broadcast_task(
            task_id, actor={"actor_type": "human", "actor_id": "broadcast_test"}
        )

        self.assertEqual(result["updated"], 0, result)
        self.assertEqual(result["unchanged"], 2)
        for instance_id in ("i_image_1", "i_image_2"):
            self.assertEqual(self._current_revision(task_id, instance_id), 1)

    def test_broadcast_rejects_unknown_task_and_missing_plan(self) -> None:
        candidate = self._drifted_snapshot(2)
        service = self._service(candidate)
        with self.assertRaises(HarnessError) as missing_task:
            service.broadcast_task(
                "task_missing",
                actor={"actor_type": "human", "actor_id": "broadcast_test"},
            )
        self.assertEqual(missing_task.exception.code, "TASK_NOT_FOUND")

        create_task(self.commands, "task_broadcast_unplanned")
        with self.assertRaises(HarnessError) as missing_plan:
            service.broadcast_task(
                "task_broadcast_unplanned",
                actor={"actor_type": "human", "actor_id": "broadcast_test"},
            )
        self.assertEqual(missing_plan.exception.code, "VALIDATION_ERROR")

    def test_broadcast_rejects_non_human_actor(self) -> None:
        task_id = self._planned_task("task_broadcast_actor", count=1)
        candidate = self._drifted_snapshot(2)
        service = self._service(candidate)
        with self.assertRaises(HarnessError) as raised:
            service.broadcast_task(
                task_id, actor={"actor_type": "master", "actor_id": "m"}
            )
        self.assertEqual(raised.exception.code, "VALIDATION_ERROR")


class SettingsPeerSyncApiTests(unittest.TestCase):
    def test_sync_toggle_and_broadcast_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(
                HarnessSettings(
                    control_root=root / "control-data",
                    workspace_root=root / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    config_snapshot=build_config_snapshot(),
                )
            )
            with TestClient(app) as client:
                container = app.state.container
                created = create_task(container.commands, "task_sync_api")
                container.task_config.pin("task_sync_api")
                draft = image_plan("task_sync_api", count=2)
                container.commands.save_plan(
                    "task_sync_api",
                    stages=draft["stages"],
                    instances=draft["instances"],
                    task_cards=draft["task_cards"],
                    envelope=envelope("save-sync-api", created["revision"]),
                )
                current = client.get("/api/v1/instances/i_image_1/runtime-settings")
                self.assertEqual(current.status_code, 200, current.text)
                payload = current.json()
                self.assertFalse(payload["sync_to_peers"])
                self.assertEqual(
                    [item["instance_id"] for item in payload["sync_peers"]],
                    ["i_image_2"],
                )
                work_item_id = payload["scope"]["work_item_id"]

                task_revision = container.store.task.revision(
                    "task_sync_api", "task_sync_api"
                )
                toggled = client.post(
                    f"/api/v1/tasks/task_sync_api/work-items/{work_item_id}/sync-toggle",
                    json={
                        "sync_to_peers": True,
                        "envelope": self._body_envelope(
                            "sync-toggle-api", task_revision
                        ),
                    },
                )
                self.assertEqual(toggled.status_code, 200, toggled.text)
                self.assertTrue(toggled.json()["sync_to_peers"])
                self.assertEqual(toggled.json()["instance_id"], "i_image_1")

                projected = client.get("/api/v1/instances/i_image_1/runtime-settings")
                self.assertTrue(projected.json()["sync_to_peers"])

                task_revision = container.store.task.revision(
                    "task_sync_api", "task_sync_api"
                )
                broadcast = client.post(
                    "/api/v1/tasks/task_sync_api/settings/broadcast",
                    json={
                        "envelope": self._body_envelope(
                            "broadcast-api", task_revision
                        )
                    },
                )
                self.assertEqual(broadcast.status_code, 200, broadcast.text)
                body = broadcast.json()
                self.assertEqual(body["task_id"], "task_sync_api")
                self.assertEqual(body["unchanged"], 2, body)
                self.assertEqual(body["failed"], 0)

                missing = client.post(
                    "/api/v1/tasks/task_sync_api/work-items/work_missing/sync-toggle",
                    json={
                        "sync_to_peers": True,
                        "envelope": self._body_envelope(
                            "sync-toggle-missing", task_revision
                        ),
                    },
                )
                self.assertEqual(missing.status_code, 404, missing.text)
                self.assertEqual(missing.json()["error"]["code"], "TASK_NOT_FOUND")

    @staticmethod
    def _body_envelope(key: str, expected_revision: int) -> dict:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "tester",
            "expected_revision": expected_revision,
        }


if __name__ == "__main__":
    unittest.main()
