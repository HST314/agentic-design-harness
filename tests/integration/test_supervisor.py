from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

import harness.services.process_runtime as process_runtime
from harness.core.errors import HarnessError, SimulatedCrash
from harness.services.agent_config_materialization import ImageAgentConfigMaterializer
from harness.services.process_control import force_kill_process_tree
from harness.services.process_runtime import AgentRuntimeArtifact, runtime_artifact_identity
from harness.services.supervisor import ProcessSpec, ProcessSupervisor, process_start_identity
from harness.services.task_config import TaskConfigService
from harness.storage.atomic import atomic_write_json, read_json
from runtime_helpers import build_config_snapshot, build_service, create_task, envelope, image_plan

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
FAKE_AGENT = FIXTURE_ROOT / "fake_agent_process.py"


def make_artifact_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def make_artifact_writable(root: Path) -> None:
    if not root.exists():
        return
    root.chmod(0o755)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        path.chmod(0o755 if path.is_dir() else 0o644)


class ProcessSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(
            self.root, lock_timeout_seconds=5.0
        )
        created = create_task(self.commands, "t_process", "auto")
        draft = image_plan("t_process", 3)
        self.commands.save_plan(
            "t_process",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope("save-process-plan", created["revision"]),
        )
        snapshot = build_config_snapshot(
            api_key="phase-c-supervisor-secret",
            base_url="https://phase-c-provider.invalid/v1",
            supervisor_port_start=19100,
            supervisor_port_end=19130,
            supervisor_startup_timeout=2,
            supervisor_shutdown_grace=1,
        )
        self.task_config = TaskConfigService(self.store, snapshot)
        self.image_config = ImageAgentConfigMaterializer(self.store, self.task_config)
        self.supervisor = ProcessSupervisor(
            self.store,
            self.commands,
            self.image_config,
        )
        self.artifact_root = self.root / "fake-runtime-artifact"
        self.artifact_root.mkdir()
        self.artifact_entrypoint = self.artifact_root / "fake_agent_process.py"
        shutil.copyfile(FAKE_AGENT, self.artifact_entrypoint)
        (self.artifact_root / "requirements.lock").write_text(
            "stdlib-only\n", encoding="utf-8"
        )
        make_artifact_read_only(self.artifact_root)
        self.runtime_artifact = AgentRuntimeArtifact(
            artifact_id="fake-agent",
            revision="1",
            source_root=self.artifact_root,
            entrypoint_relpath="fake_agent_process.py",
            dependency_lock_relpaths=("requirements.lock",),
        )
        self.spec = ProcessSpec(
            command=(sys.executable, str(self.artifact_entrypoint)),
            runtime_artifact=self.runtime_artifact,
        )

    def tearDown(self) -> None:
        self.supervisor.close()
        for path in self.supervisor.launch_root.glob("*.json"):
            record = read_json(path)
            if record["state"] in {"PREPARED", "STARTING", "RUNNING"} and record.get("pid"):
                with suppress(ProcessLookupError):
                    force_kill_process_tree(record["pid"])
        for process in self.supervisor._children.values():
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
        self.store.close()
        make_artifact_writable(self.artifact_root)
        if os.name == "nt":
            make_artifact_writable(self.root)
        self.temporary.cleanup()

    def _start(self, index: int, spec: ProcessSpec | None = None) -> dict:
        return self.supervisor.start_instance(
            "t_process",
            f"i_image_{index}",
            spec or self.spec,
            launch_id=f"launch_{index}",
            attempt_id=f"attempt_{index}",
        )

    @staticmethod
    def _identity(port: int) -> dict:
        with urlopen(f"http://127.0.0.1:{port}/identity", timeout=1) as response:
            return json.loads(response.read())

    def test_health_failures_respect_threshold_and_record_probe_detail(self) -> None:
        snapshot = build_config_snapshot(
            api_key="phase-c-supervisor-secret",
            base_url="https://phase-c-provider.invalid/v1",
            supervisor_port_start=19200,
            supervisor_port_end=19230,
            supervisor_startup_timeout=2,
            supervisor_shutdown_grace=1,
            supervisor_probe_timeout=1.5,
            supervisor_health_failure_threshold=2,
        )
        task_config = TaskConfigService(self.store, snapshot)
        image_config = ImageAgentConfigMaterializer(self.store, task_config)
        supervisor = ProcessSupervisor(self.store, self.commands, image_config)
        self.addCleanup(supervisor.close)
        launch = supervisor.start_instance(
            "t_process",
            "i_image_1",
            self.spec,
            launch_id="launch_health",
            attempt_id="attempt_health",
        )
        record_path = supervisor._launch_path(launch["launch_id"])
        record = read_json(record_path)
        self.assertEqual(record["probe_timeout_seconds"], 1.5)
        self.assertEqual(record["health_failure_threshold"], 2)

        failure = (
            False,
            {
                "path": "/healthz",
                "timeout_seconds": 1.5,
                "error": "TimeoutError: timed out",
            },
        )
        with patch.object(ProcessSupervisor, "_probe_with_detail", return_value=failure):
            # First failure stays below the configured threshold: no kill.
            self.assertEqual(supervisor.monitor_once(), [])
            self.assertEqual(
                self.store.instance.get("t_process", "i_image_1")["status"], "RUNNING"
            )
            record = read_json(record_path)
            self.assertEqual(record["health_failures"], 1)
            self.assertEqual(
                record["last_health_failure"]["error"], "TimeoutError: timed out"
            )
            # Second consecutive failure reaches the threshold: kill as crashed.
            changes = supervisor.monitor_once()
        self.assertEqual(changes[0]["status"], "CRASHED")
        self.assertEqual(
            self.store.instance.get("t_process", "i_image_1")["status"], "CRASHED"
        )
        record = read_json(record_path)
        self.assertEqual(record["exit_reason"], "health_or_process_exit")
        self.assertEqual(
            record["last_health_failure"]["error"], "TimeoutError: timed out"
        )

    def test_three_isolated_processes_crash_cancel_and_archive_independently(self) -> None:
        previous = os.environ.get("UNRELATED_SECRET")
        os.environ["UNRELATED_SECRET"] = "must-not-be-inherited"
        try:
            launches = [self._start(index) for index in range(1, 4)]
        finally:
            if previous is None:
                os.environ.pop("UNRELATED_SECRET", None)
            else:
                os.environ["UNRELATED_SECRET"] = previous
        self.assertEqual(len({item["pid"] for item in launches}), 3)
        self.assertEqual(len({item["port"] for item in launches}), 3)
        identities = [self._identity(item["port"]) for item in launches]
        self.assertEqual(len({item["pid"] for item in identities}), 3)
        self.assertEqual(len({item["cwd"] for item in identities}), 3)
        self.assertTrue(all(item["unrelated_secret"] == "missing" for item in identities))
        for index, identity in enumerate(identities, start=1):
            self.assertEqual(identity["instance_id"], f"i_image_{index}")
            expected_projects_root = (
                self.store.layout.workspace_root
                / "tasks"
                / "t_process"
                / "instances"
                / f"i_image_{index}"
                / "work"
            )
            self.assertEqual(
                Path(identity["projects_root"]).resolve(),
                expected_projects_root.resolve(),
            )

        self.supervisor.begin_model_call(
            "t_process",
            "i_image_1",
            attempt_id="model_attempt_1",
            request_id="provider-request-1",
        )
        with self.assertRaises(HarnessError) as reused_attempt:
            self.supervisor.begin_model_call(
                "t_process",
                "i_image_1",
                attempt_id="model_attempt_1",
                request_id="provider-request-other",
            )
        self.assertEqual(reused_attempt.exception.code, "IDEMPOTENCY_CONFLICT")
        os.kill(launches[0]["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 2
        while (
            process_start_identity(launches[0]["pid"]) is not None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        self.supervisor.monitor_once()
        first = self.store.instance.get("t_process", "i_image_1")
        self.assertEqual(first["status"], "CRASHED")
        attempt = read_json(
            self.store.layout.control_root
            / "tasks"
            / "t_process"
            / "attempts"
            / "model_attempt_1.json"
        )
        self.assertEqual(attempt["status"], "INTERRUPTED")
        self.assertEqual(self._identity(launches[1]["port"])["instance_id"], "i_image_2")
        self.assertEqual(self._identity(launches[2]["port"])["instance_id"], "i_image_3")

        cancelled = self.supervisor.cancel_instance("t_process", "i_image_2")
        self.assertEqual(cancelled["status"], "CANCELLED")
        second_root = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_process"
            / "instances"
            / "i_image_2"
        )
        self.assertTrue(second_root.is_dir())
        self.assertEqual(self._identity(launches[2]["port"])["instance_id"], "i_image_3")

        revision = self.store.task.revision("t_process", "t_process")
        self.commands.transition_instance(
            "t_process",
            "i_image_3",
            "SUCCEEDED",
            envelope("third-succeeded", revision, "adapter"),
        )
        archived = self.supervisor.archive_instance("t_process", "i_image_3")
        self.assertEqual(archived["status"], "ARCHIVED")
        third_root = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_process"
            / "instances"
            / "i_image_3"
        )
        if os.name != "nt":
            self.assertEqual(third_root.stat().st_mode & 0o777, 0o500)
        claims = read_json(self.supervisor.port_allocator.path)["claims"]
        self.assertEqual(claims, {})

    def test_logs_are_redacted_and_launch_spec_file_is_removed(self) -> None:
        spec = ProcessSpec(
            command=self.spec.command,
            runtime_artifact=self.runtime_artifact,
            public_environment={"FAKE_LONG_LOG": "1"},
        )
        launch = self._start(1, spec)
        deadline = time.monotonic() + 1
        summary = self.supervisor.log_summary("t_process", "i_image_1")
        while not summary["logs"]["stdout"] and time.monotonic() < deadline:
            time.sleep(0.02)
            summary = self.supervisor.log_summary("t_process", "i_image_1")
        rendered = json.dumps(summary)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("phase-c-supervisor-secret", rendered)
        self.assertNotIn("https://phase-c-provider.invalid/v1", rendered)
        instance_root = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_process"
            / "instances"
            / "i_image_1"
        )
        self.assertFalse((instance_root / "runtime" / ".launch_1.launch-spec.json").exists())
        self.assertNotIn(
            b"phase-c-supervisor-secret",
            (instance_root / "logs" / "stdout.log").read_bytes(),
        )
        self.assertEqual(launch["state"], "RUNNING")

    def test_log_summary_rejects_symlinked_log_components(self) -> None:
        instance_root = self.store.layout.initialize_instance(
            "t_process", "i_image_1"
        )
        outside = self.root / "outside-logs"
        outside.mkdir()
        (outside / "stdout.log").write_text("HOST_LOG_EXPOSED\n", encoding="utf-8")
        (instance_root / "logs").rmdir()
        (instance_root / "logs").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(HarnessError) as captured:
            self.supervisor.log_summary("t_process", "i_image_1")

        self.assertEqual(captured.exception.code, "PATH_OUTSIDE_TASK_ROOT")
        with self.assertRaises(HarnessError) as launch:
            self._start(1)
        self.assertEqual(launch.exception.code, "PATH_OUTSIDE_TASK_ROOT")
        self.assertEqual(
            (outside / "stdout.log").read_text(encoding="utf-8"),
            "HOST_LOG_EXPOSED\n",
        )

    def test_three_instances_start_concurrently_without_revision_conflicts(self) -> None:
        with ThreadPoolExecutor(max_workers=3) as executor:
            launches = list(executor.map(self._start, range(1, 4)))

        self.assertEqual({item["state"] for item in launches}, {"RUNNING"})
        self.assertEqual(len({item["port"] for item in launches}), 3)

    def test_harness_reconcile_and_restart_keep_task_configuration(self) -> None:
        launch = self._start(1)
        original = self.task_config.get_public("t_process")
        second_supervisor = ProcessSupervisor(
            self.store,
            self.commands,
            self.image_config,
        )
        reconciled = second_supervisor.reconcile()
        self.assertEqual(reconciled[0]["status"], "RECOVERED")
        replay = second_supervisor.start_instance(
            "t_process",
            "i_image_1",
            self.spec,
            launch_id="launch_1",
            attempt_id="attempt_1",
        )
        self.assertEqual(replay["pid"], launch["pid"])
        with self.assertRaises(HarnessError) as reused_launch:
            second_supervisor.start_instance(
                "t_process",
                "i_image_1",
                self.spec,
                launch_id="launch_1",
                attempt_id="attempt_other",
            )
        self.assertEqual(reused_launch.exception.code, "IDEMPOTENCY_CONFLICT")

        restarted = second_supervisor.restart_instance(
            "t_process",
            "i_image_1",
            self.spec,
            launch_id="launch_restart_1",
            attempt_id="attempt_restart_1",
        )
        self.assertNotEqual(restarted["pid"], launch["pid"])
        current = self.task_config.get_public("t_process")
        self.assertEqual(current, original)
        second_supervisor.cancel_instance("t_process", "i_image_1")
        second_supervisor.close()

    def test_reconcile_promotes_a_ready_start_interrupted_by_harness_crash(self) -> None:
        def crash_after_record(checkpoint: str) -> None:
            if checkpoint == "after_process_record":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.supervisor.start_instance(
                "t_process",
                "i_image_1",
                self.spec,
                launch_id="launch_recover_start",
                attempt_id="attempt_recover_start",
                crash_hook=crash_after_record,
            )
        with self.assertRaises(HarnessError) as not_ready:
            self.supervisor.begin_model_call(
                "t_process",
                "i_image_1",
                attempt_id="model_attempt_too_early",
                request_id="provider-request-too-early",
            )
        self.assertEqual(not_ready.exception.code, "PROCESS_START_FAILED")
        launch_path = self.supervisor._launch_path("launch_recover_start")
        interrupted = read_json(launch_path)
        interrupted.update(
            {
                "state": "PREPARED",
                "pid": None,
                "start_identity": None,
                "child_pid": None,
                "child_start_identity": None,
                "started_at": None,
            }
        )
        atomic_write_json(launch_path, interrupted)
        recovered_supervisor = ProcessSupervisor(
            self.store,
            self.commands,
            self.image_config,
        )
        deadline = time.monotonic() + 2
        results = recovered_supervisor.reconcile()
        while (
            not any(item["status"] == "RUNNING" for item in results)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
            results = recovered_supervisor.reconcile()
        self.assertTrue(any(item["status"] == "RUNNING" for item in results))
        instance = self.store.instance.get("t_process", "i_image_1")
        self.assertEqual(instance["status"], "RUNNING")
        recovered_supervisor.cancel_instance("t_process", "i_image_1")
        recovered_supervisor.close()

    def test_health_timeout_fails_start_and_releases_port(self) -> None:
        unhealthy = ProcessSpec(
            command=self.spec.command,
            runtime_artifact=self.runtime_artifact,
            public_environment={"FAKE_HEALTHY": "0"},
        )
        with self.assertRaises(HarnessError) as captured:
            self._start(1, unhealthy)
        self.assertEqual(captured.exception.code, "PROCESS_START_FAILED")
        instance = self.store.instance.get("t_process", "i_image_1")
        self.assertEqual(instance["status"], "FAILED_TO_START")
        self.assertEqual(read_json(self.supervisor.port_allocator.path)["claims"], {})

    def test_runtime_artifact_rejects_writable_and_symlinked_content(self) -> None:
        make_artifact_writable(self.artifact_root)
        with self.assertRaises(HarnessError) as writable:
            self._start(1)
        self.assertEqual(writable.exception.code, "PROCESS_START_FAILED")
        self.assertFalse(self.supervisor._launch_path("launch_1").exists())

        (self.artifact_root / "linked.py").symlink_to(FAKE_AGENT)
        make_artifact_read_only(self.artifact_root)
        with self.assertRaises(HarnessError) as symlinked:
            self._start(1)
        self.assertEqual(symlinked.exception.code, "PROCESS_START_FAILED")
        self.assertFalse(self.supervisor._launch_path("launch_1").exists())

    def test_path_backed_artifact_open_failure_has_actionable_details(self) -> None:
        with (
            patch.object(
                process_runtime,
                "open_regular_readonly",
                side_effect=OSError(13, "blocked"),
            ),
            self.assertRaises(HarnessError) as rejected,
        ):
            process_runtime._artifact_manifest(self.artifact_root)

        self.assertEqual(rejected.exception.code, "PROCESS_START_FAILED")
        self.assertEqual(
            rejected.exception.details,
            {
                "path": "fake_agent_process.py",
                "operation": "open",
                "errno": 13,
            },
        )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX named pipes")
    def test_runtime_artifact_rejects_special_files_without_blocking(self) -> None:
        make_artifact_writable(self.artifact_root)
        os.mkfifo(self.artifact_root / "named-pipe")
        make_artifact_read_only(self.artifact_root)
        with self.assertRaises(HarnessError) as special:
            self._start(1)
        self.assertEqual(special.exception.code, "PROCESS_START_FAILED")
        self.assertFalse(self.supervisor._launch_path("launch_1").exists())

    def test_code_change_is_rejected_before_the_live_process_is_stopped(self) -> None:
        make_artifact_writable(self.artifact_root)
        helper = self.artifact_root / "helper.py"
        self.artifact_entrypoint.write_text(
            "from helper import main\n\nif __name__ == '__main__':\n    main()\n",
            encoding="utf-8",
        )
        shutil.copyfile(FAKE_AGENT, helper)
        make_artifact_read_only(self.artifact_root)
        spec = ProcessSpec(
            command=(sys.executable, str(self.artifact_entrypoint)),
            runtime_artifact=self.runtime_artifact,
        )
        launch = self._start(1, spec)
        artifact_record = read_json(self.supervisor._launch_path("launch_1"))[
            "runtime_artifact"
        ]
        self.assertEqual(artifact_record["artifact_id"], "fake-agent")
        self.assertEqual(
            {item["path"] for item in artifact_record["source_manifest"]},
            {"fake_agent_process.py", "helper.py", "requirements.lock"},
        )
        make_artifact_writable(self.artifact_root)
        helper.write_text(
            helper.read_text(encoding="utf-8") + "\n# imported module changed\n",
            encoding="utf-8",
        )
        make_artifact_read_only(self.artifact_root)
        with self.assertRaises(HarnessError) as changed:
            self.supervisor.restart_instance(
                "t_process",
                "i_image_1",
                spec,
                launch_id="launch_changed_code",
                attempt_id="attempt_changed_code",
            )
        self.assertEqual(changed.exception.code, "PROCESS_START_FAILED")
        self.assertEqual(self._identity(launch["port"])["instance_id"], "i_image_1")
        self.supervisor.cancel_instance("t_process", "i_image_1")

    def test_startup_verified_artifact_skips_per_instance_tree_scans(self) -> None:
        identity = runtime_artifact_identity(self.spec)
        startup_digest = identity.digest
        with patch.object(
            process_runtime,
            "digest_json",
            side_effect=AssertionError("runtime identity digest was recalculated"),
        ):
            self.assertEqual(identity.digest, startup_digest)
        cached_spec = ProcessSpec(
            command=self.spec.command,
            runtime_artifact=self.runtime_artifact,
            verified_runtime_identity=identity,
        )

        with patch.object(
            process_runtime,
            "_artifact_manifest",
            side_effect=AssertionError("runtime tree was rescanned"),
        ):
            launch = self._start(1, cached_spec)

        self.assertEqual(
            launch["runtime_artifact"]["source_manifest_sha256"],
            identity.source_manifest_sha256,
        )
        self.supervisor.cancel_instance("t_process", "i_image_1")

    def test_root_swap_during_restart_executes_the_pinned_artifact(self) -> None:
        launch = self._start(1)
        original_root = self.artifact_root.with_name("original-runtime-artifact")
        replacement_root = self.artifact_root.with_name("replacement-runtime-artifact")
        replacement_root.mkdir()
        (replacement_root / "fake_agent_process.py").write_text(
            "raise SystemExit(97)\n", encoding="utf-8"
        )
        (replacement_root / "requirements.lock").write_text(
            "stdlib-only\n", encoding="utf-8"
        )
        make_artifact_read_only(replacement_root)
        original_manifest = process_runtime._artifact_manifest
        swapped = False

        def manifest_then_swap(root_or_descriptor):
            nonlocal swapped
            manifest = original_manifest(root_or_descriptor)
            if not swapped:
                self.artifact_root.rename(original_root)
                replacement_root.rename(self.artifact_root)
                swapped = True
            return manifest

        try:
            with patch.object(
                process_runtime, "_artifact_manifest", side_effect=manifest_then_swap
            ):
                if os.name == "nt":
                    with self.assertRaises(HarnessError) as blocked:
                        self.supervisor.restart_instance(
                            "t_process",
                            "i_image_1",
                            self.spec,
                            launch_id="launch_root_swap",
                            attempt_id="attempt_root_swap",
                        )
                    self.assertEqual(blocked.exception.code, "PROCESS_START_FAILED")
                    self.assertTrue(swapped)
                    self.assertEqual(
                        self._identity(launch["port"])["instance_id"], "i_image_1"
                    )
                else:
                    restarted = self.supervisor.restart_instance(
                        "t_process",
                        "i_image_1",
                        self.spec,
                        launch_id="launch_root_swap",
                        attempt_id="attempt_root_swap",
                    )
                    self.assertTrue(swapped)
                    self.assertNotEqual(restarted["pid"], launch["pid"])
                    self.assertEqual(
                        self._identity(restarted["port"])["instance_id"],
                        "i_image_1",
                    )
            self.supervisor.cancel_instance("t_process", "i_image_1")
        finally:
            make_artifact_writable(self.artifact_root)
            if original_root.exists():
                shutil.rmtree(self.artifact_root)
                original_root.rename(self.artifact_root)
            if replacement_root.exists():
                make_artifact_writable(replacement_root)
                shutil.rmtree(replacement_root)

    def test_manual_start_confirmation_cannot_be_bypassed(self) -> None:
        created = create_task(self.commands, "t_manual_process", "manual")
        draft = image_plan("t_manual_process")
        saved = self.commands.save_plan(
            "t_manual_process",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope("save-manual-process", created["revision"]),
        )
        with self.assertRaises(HarnessError) as blocked:
            self.supervisor.start_instance(
                "t_manual_process",
                "i_image_1",
                self.spec,
                launch_id="launch_manual_blocked",
                attempt_id="attempt_manual_blocked",
            )
        self.assertEqual(blocked.exception.code, "INVALID_STATE_TRANSITION")
        self.assertFalse(self.supervisor._launch_path("launch_manual_blocked").exists())

        self.commands.confirm_start(
            "t_manual_process",
            envelope("confirm-manual-process", saved["task_revision"]),
        )
        launched = self.supervisor.start_instance(
            "t_manual_process",
            "i_image_1",
            self.spec,
            launch_id="launch_manual_allowed",
            attempt_id="attempt_manual_allowed",
        )
        self.assertEqual(launched["state"], "RUNNING")
        self.supervisor.cancel_instance("t_manual_process", "i_image_1")

    def test_port_conflict_and_stale_pid_do_not_target_unrelated_process(self) -> None:
        conflict = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        conflict.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        conflict.bind(("127.0.0.1", 19100))
        conflict.listen()
        try:
            launch = self._start(1)
        finally:
            conflict.close()
        self.assertNotEqual(launch["port"], 19100)
        force_kill_process_tree(launch["pid"])
        deadline = time.monotonic() + 1
        while process_start_identity(launch["pid"]) is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        record_path = self.supervisor._launch_path("launch_1")
        record = read_json(record_path)
        record.update(
            {
                "pid": os.getpid(),
                "start_identity": "stale-identity",
                "child_pid": None,
                "child_start_identity": None,
            }
        )
        atomic_write_json(record_path, record)
        self.supervisor.reconcile()
        self.assertIsNotNone(process_start_identity(os.getpid()))
        instance = self.store.instance.get("t_process", "i_image_1")
        self.assertEqual(instance["status"], "CRASHED")


if __name__ == "__main__":
    unittest.main()
