from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import yaml
from harness.core.errors import HarnessError, SimulatedCrash
from harness.services.configuration import ConfigurationService, GlobalConfigBody
from harness.storage.repository import Actor
from runtime_helpers import build_service, create_task, envelope, image_plan


class ConfigurationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        created = create_task(self.commands, "t_config", "auto")
        draft = image_plan("t_config", 2)
        self.commands.save_plan(
            "t_config",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope("save-config-plan", created["revision"]),
        )
        self.config = ConfigurationService(self.store)
        self.global_config = self.config.initialize()
        for instance_id in ("i_image_1", "i_image_2"):
            self.config.create_instance_snapshot("t_config", instance_id)
        self.actor = Actor("human", "tester")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _start_first_instance(self) -> None:
        revision = self.store.task.revision("t_config", "t_config")
        self.commands.transition_instance(
            "t_config",
            "i_image_1",
            "STARTING",
            envelope("config-starting", revision, "adapter"),
        )
        revision = self.store.task.revision("t_config", "t_config")
        self.commands.transition_instance(
            "t_config",
            "i_image_1",
            "RUNNING",
            envelope("config-running", revision, "adapter"),
        )

    def test_local_change_is_isolated_then_global_save_forces_complete_overwrite(self) -> None:
        first = self.config.update_instance(
            "t_config",
            "i_image_1",
            {"image_runtime_policy": {"candidate_concurrency": 3}},
            expected_revision=1,
            idempotency_key="local-config-one",
            actor=self.actor,
        )
        second_path = self.config._instance_config_path("t_config", "i_image_2")
        second = self.config._read_instance_config("t_config", "i_image_2")
        self.assertEqual(first["config"]["image_runtime_policy"]["candidate_concurrency"], 3)
        self.assertEqual(second["config"]["image_runtime_policy"]["candidate_concurrency"], 5)
        self.assertEqual(first["scope"], "instance")

        self._start_first_instance()
        model_call = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_config"
            / "instances"
            / "i_image_1"
            / "work"
            / "model-call.json"
        )
        model_call.write_text('{"request_id":"already-sent"}', encoding="utf-8")
        replacement = GlobalConfigBody.model_validate(
            {
                **GlobalConfigBody().model_dump(mode="json"),
                "image_runtime_policy": {
                    **GlobalConfigBody().image_runtime_policy.model_dump(mode="json"),
                    "candidate_concurrency": 4,
                },
            }
        )
        saved = self.config.save_global(
            replacement,
            expected_revision=1,
            idempotency_key="global-config-two",
            actor=self.actor,
        )
        self.assertEqual(saved["revision"], 2)
        one = self.config._read_instance_config("t_config", "i_image_1")
        two = self.config._read_instance_config("t_config", "i_image_2")
        self.assertEqual(one["config_revision"], 3)
        self.assertEqual(two["config_revision"], 2)
        self.assertEqual(one["source_global_revision"], 2)
        self.assertEqual(one["config"], two["config"])
        self.assertEqual(one["config"]["image_runtime_policy"]["candidate_concurrency"], 4)
        self.assertTrue(one["restart_required"])
        self.assertFalse(two["restart_required"])
        self.assertEqual(model_call.read_text(encoding="utf-8"), '{"request_id":"already-sent"}')
        self.assertTrue(second_path.is_file())

        runtime_path = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_config"
            / "instances"
            / "i_image_1"
            / "runtime"
            / "runtime.yaml"
        )
        runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
        self.assertEqual(runtime["candidate_concurrency"], 4)
        self.assertEqual(runtime["image_api_base_url"], "")
        self.assertNotIn("agent_code_version", one["config"])
        projected = self.store.instance.get("t_config", "i_image_1")
        self.assertEqual(projected["config_revision"], 3)
        self.assertTrue(projected["restart_required"])

    def test_hot_apply_clears_restart_flag_and_is_validated(self) -> None:
        self._start_first_instance()
        calls: list[tuple[str, str, Path]] = []

        def apply(task_id: str, instance_id: str, path: Path) -> bool:
            calls.append((task_id, instance_id, path))
            return path.is_file()

        hot = ConfigurationService(self.store, apply_config=apply)
        body = GlobalConfigBody.model_validate(
            {
                **GlobalConfigBody().model_dump(mode="json"),
                "image_runtime_policy": {
                    **GlobalConfigBody().image_runtime_policy.model_dump(mode="json"),
                    "watermark": True,
                },
            }
        )
        hot.save_global(
            body,
            expected_revision=1,
            idempotency_key="hot-global",
            actor=self.actor,
        )
        snapshot = hot._read_instance_config("t_config", "i_image_1")
        self.assertFalse(snapshot["restart_required"])
        self.assertIsNotNone(snapshot["applied_at"])
        self.assertEqual(calls[0][0:2], ("t_config", "i_image_1"))
        call_count = len(calls)
        hot.recover()
        recovered = hot._read_instance_config("t_config", "i_image_1")
        self.assertFalse(recovered["restart_required"])
        self.assertEqual(len(calls), call_count)

    def test_global_commit_recovers_before_and_during_target_projection(self) -> None:
        body = GlobalConfigBody.model_validate(
            {
                **GlobalConfigBody().model_dump(mode="json"),
                "image_runtime_policy": {
                    **GlobalConfigBody().image_runtime_policy.model_dump(mode="json"),
                    "default_output_size": "4K",
                },
            }
        )

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_global_target_1":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.config.save_global(
                body,
                expected_revision=1,
                idempotency_key="crash-global",
                actor=self.actor,
                crash_hook=crash,
            )
        before = [
            self.config._read_instance_config("t_config", item)["config_revision"]
            for item in ("i_image_1", "i_image_2")
        ]
        self.assertEqual(sorted(before), [1, 2])
        self.config.recover()
        after = [
            self.config._read_instance_config("t_config", item)
            for item in ("i_image_1", "i_image_2")
        ]
        self.assertEqual([item["config_revision"] for item in after], [2, 2])
        self.assertTrue(
            all(
                item["config"]["image_runtime_policy"]["default_output_size"] == "4K"
                for item in after
            )
        )
        replay = self.config.save_global(
            body,
            expected_revision=1,
            idempotency_key="crash-global",
            actor=self.actor,
        )
        self.assertEqual(replay["revision"], 2)

    def test_applied_event_recovers_before_restart_projection(self) -> None:
        self._start_first_instance()
        pending = self.config.update_instance(
            "t_config",
            "i_image_1",
            {"image_runtime_policy": {"watermark": True}},
            expected_revision=1,
            idempotency_key="pending-restart-config",
            actor=self.actor,
        )
        self.assertTrue(pending["restart_required"])
        self.config._record_config_applied(pending, "crash_test")

        recovered_service = ConfigurationService(self.store)
        recovered_service.recover()
        recovered = recovered_service._read_instance_config(
            "t_config", "i_image_1"
        )
        self.assertFalse(recovered["restart_required"])
        self.assertIsNotNone(recovered["applied_at"])

    def test_archived_instance_is_not_overwritten(self) -> None:
        archived = deepcopy(self.store.instance.get("t_config", "i_image_2"))
        archived["status"] = "ARCHIVED"
        archived.update(
            {
                "instance_id": "i_archived",
                "workspace_relpath": "instances/i_archived",
                "task_card_relpath": "instances/i_archived/task-card.json",
            }
        )
        self.store.instance.put(
            "t_config",
            "i_archived",
            archived,
            expected_revision=0,
            actor=self.actor,
            command="test_archived_instance",
            idempotency_key="test-archived-instance",
        )
        archived_snapshot = self.config.create_instance_snapshot("t_config", "i_archived")
        with self.assertRaises(HarnessError) as read_only:
            self.config.update_instance(
                "t_config",
                "i_archived",
                {"image_runtime_policy": {"watermark": True}},
                expected_revision=archived_snapshot["config_revision"],
                idempotency_key="archived-config-update",
                actor=self.actor,
            )
        self.assertEqual(read_only.exception.code, "INVALID_STATE_TRANSITION")
        self.config.save_global(
            GlobalConfigBody().model_copy(
                update={
                    "image_runtime_policy": GlobalConfigBody().image_runtime_policy.model_copy(
                        update={"watermark": True}
                    )
                }
            ),
            expected_revision=1,
            idempotency_key="skip-archived",
            actor=self.actor,
        )
        after = self.config._read_instance_config("t_config", "i_archived")
        self.assertEqual(after, archived_snapshot)

    def test_unknown_code_and_credential_fields_are_rejected(self) -> None:
        with self.assertRaises(HarnessError) as code:
            self.config.update_instance(
                "t_config",
                "i_image_1",
                {"agent_code_version": "mutable"},
                expected_revision=1,
                idempotency_key="invalid-code",
                actor=self.actor,
            )
        self.assertEqual(code.exception.code, "VALIDATION_ERROR")
        invalid = GlobalConfigBody().model_dump(mode="json")
        invalid["image_model_config"]["state_bindings"] = [
            {
                "state": "render",
                "model_role": "text_to_image_model",
                "provider": "fake",
                "model": "fake-model",
                "parameters": {"api_key": "must-not-enter-config"},
                "fallback_model": None,
            }
        ]
        with self.assertRaises(HarnessError) as credential:
            self.config.save_global(
                invalid,
                expected_revision=1,
                idempotency_key="invalid-secret-field",
                actor=self.actor,
            )
        self.assertEqual(credential.exception.code, "VALIDATION_ERROR")

    def test_concurrent_local_and_global_commits_are_serialized(self) -> None:
        global_body = GlobalConfigBody().model_copy(
            update={
                "image_runtime_policy": GlobalConfigBody().image_runtime_policy.model_copy(
                    update={"max_auto_questions": 2}
                )
            }
        )
        def local_commit() -> dict | str:
            try:
                return self.config.update_instance(
                    "t_config",
                    "i_image_1",
                    {"image_runtime_policy": {"watermark": True}},
                    expected_revision=1,
                    idempotency_key="concurrent-local",
                    actor=self.actor,
                )
            except HarnessError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            local_future = executor.submit(
                local_commit,
            )
            global_future = executor.submit(
                self.config.save_global,
                global_body,
                expected_revision=1,
                idempotency_key="concurrent-global",
                actor=self.actor,
            )
            local_result = local_future.result(timeout=5)
            global_result = global_future.result(timeout=5)

        snapshot = self.config._read_instance_config("t_config", "i_image_1")
        self.assertIn(
            local_result if isinstance(local_result, str) else "COMMITTED",
            {"COMMITTED", "REVISION_CONFLICT"},
        )
        self.assertEqual(global_result["revision"], 2)
        self.assertIn(snapshot["config_revision"], {2, 3})
        self.assertEqual(snapshot["source_global_revision"], 2)
        self.assertEqual(snapshot["scope"], "global")
        self.assertEqual(
            snapshot["config"]["image_runtime_policy"]["max_auto_questions"], 2
        )


if __name__ == "__main__":
    unittest.main()
