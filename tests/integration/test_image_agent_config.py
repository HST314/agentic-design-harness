from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import yaml
from harness.core.config_kernel import ConfigSnapshot
from harness.services.agent_config_materialization import ImageAgentConfigMaterializer
from harness.services.task_config import TaskConfigService
from runtime_helpers import build_config_snapshot, build_service, create_task


class ImageAgentConfigMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        create_task(self.commands, "t_image_config")

    def tearDown(self) -> None:
        self.store.close()
        runtime_root = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_image_config"
            / "instances"
            / "i_image_config"
            / "runtime-config"
        )
        if runtime_root.exists():
            runtime_root.chmod(0o700)
        self.temporary.cleanup()

    def test_defaults_overrides_and_metadata_are_materialized_read_only(self) -> None:
        snapshot = self._snapshot_with_overrides()
        service = TaskConfigService(self.store, snapshot)
        materializer = ImageAgentConfigMaterializer(self.store, service)

        result = materializer.materialize("t_image_config", "i_image_config")
        expected_root = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_image_config"
            / "instances"
            / "i_image_config"
            / "runtime-config"
        )
        self.assertEqual(result["runtime_path"], expected_root / "runtime.yaml")
        self.assertEqual(result["model_config_path"], expected_root / "model_config.yaml")
        runtime = self._yaml(result["runtime_path"])
        model_config = self._yaml(result["model_config_path"])
        bindings = {item["state"]: item for item in model_config["state_bindings"]}

        self.assertEqual(runtime["question_preference"], "blocking_only")
        self.assertEqual(runtime["candidate_concurrency"], 4)
        self.assertFalse(runtime["offline_mode"])
        self.assertEqual(bindings["intake_clarify"]["model"], "text-model")
        self.assertEqual(bindings["confirmation_build"]["model"], "text-model-alt")
        self.assertEqual(bindings["initial_candidate_generation"]["model"], "image-model")
        self.assertEqual(bindings["self_check_inspection"]["model"], "vision-model-alt")
        self.assertEqual(bindings["self_check_rework"]["model"], "image-model-alt")
        self.assertEqual(bindings["human_prompt_rework"]["model"], "image-model")
        self.assertEqual(runtime["source_config_revision"], snapshot.revision)
        self.assertEqual(model_config["source_config_revision"], snapshot.revision)
        self.assertEqual(runtime["config_hash"], model_config["config_hash"])
        self.assertEqual(runtime["generated_at"], model_config["generated_at"])
        if os.name != "nt":
            self.assertEqual(result["runtime_path"].stat().st_mode & 0o777, 0o400)
            self.assertEqual(result["model_config_path"].stat().st_mode & 0o777, 0o400)
            self.assertEqual(result["runtime_path"].parent.stat().st_mode & 0o777, 0o500)

    def test_provider_secret_exists_only_in_launch_environment(self) -> None:
        secret = "phase-c-secret-never-on-disk"
        base_url = "http://127.0.0.1:19090"
        snapshot = build_config_snapshot(api_key=secret, base_url=base_url)
        service = TaskConfigService(self.store, snapshot)
        materializer = ImageAgentConfigMaterializer(self.store, service)

        launch = materializer.resolve_launch("t_image_config", "i_image_config")
        self.assertEqual(
            launch.provider_environment,
            {"ARK_API_KEY": secret, "ARK_BASE_URL": base_url},
        )
        runtime_root = launch.runtime_path.parent
        serialized = b"".join(path.read_bytes() for path in runtime_root.iterdir())
        self.assertNotIn(secret.encode(), serialized)
        self.assertNotIn(base_url.encode(), serialized)
        self.assertNotIn(secret, repr(launch))

    def test_task_snapshot_prevents_later_root_model_drift(self) -> None:
        initial = build_config_snapshot(api_key="initial-secret")
        service = TaskConfigService(self.store, initial)
        materializer = ImageAgentConfigMaterializer(self.store, service)
        first = materializer.materialize("t_image_config", "i_image_config")
        first_model = self._yaml(first["model_config_path"])

        changed_raw = build_config_snapshot(api_key="rotated-secret").model_dump(mode="json")
        changed_raw["providers"]["providers"]["ark"]["api_key"] = "rotated-secret"
        changed_raw["model_list"]["text_models"][0]["model"] = "changed-after-task"
        service.process_snapshot = ConfigSnapshot.model_validate(changed_raw)
        second = materializer.resolve_launch("t_image_config", "i_image_config")
        second_model = self._yaml(second.model_config_path)

        self.assertEqual(first_model, second_model)
        self.assertEqual(second.provider_environment["ARK_API_KEY"], "rotated-secret")
        self.assertNotIn("changed-after-task", second.model_config_path.read_text(encoding="utf-8"))

    def _snapshot_with_overrides(self) -> ConfigSnapshot:
        raw = build_config_snapshot().model_dump(mode="json")
        raw["model_list"]["text_models"].append(
            {
                **raw["model_list"]["text_models"][0],
                "id": "ark-text-alternate",
                "model": "text-model-alt",
            }
        )
        raw["model_list"]["vlm_models"].append(
            {
                **raw["model_list"]["vlm_models"][0],
                "id": "ark-vlm-alternate",
                "model": "vision-model-alt",
            }
        )
        raw["model_list"]["image_models"].append(
            {
                **raw["model_list"]["image_models"][0],
                "id": "ark-image-alternate",
                "model": "image-model-alt",
            }
        )
        image = raw["runtime"]["image_agent"]
        image["question_preference"] = "on_demand"
        image["candidate_concurrency"] = 4
        image["advanced_model_overrides"].update(
            {
                "confirmation_build": "ark-text-alternate",
                "self_check_inspection": "ark-vlm-alternate",
                "self_check_rework": "ark-image-alternate",
            }
        )
        return ConfigSnapshot.model_validate(raw)

    @staticmethod
    def _yaml(path: Path) -> dict:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(value, dict)
        return value


if __name__ == "__main__":
    unittest.main()
