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
from contextlib import suppress
from pathlib import Path
from urllib.request import urlopen

from harness.core.errors import HarnessError, SimulatedCrash
from harness.services.configuration import ConfigurationService, GlobalConfigBody
from harness.services.credentials import CredentialPoolService
from harness.services.supervisor import ProcessSpec, ProcessSupervisor, process_start_identity
from harness.storage.atomic import atomic_write_json, read_json
from harness.storage.repository import Actor
from runtime_helpers import build_service, create_task, envelope, image_plan

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
CREDENTIAL_FIXTURE = FIXTURE_ROOT / "p1" / "credential-pairs.json"
FAKE_AGENT = FIXTURE_ROOT / "fake_agent_process.py"


def creation_summary(task_id: str, raw: dict) -> dict:
    return {
        "schema_version": "1.0",
        **raw,
        "task_id": task_id,
        "requirement_lifecycle": {
            "original_required": raw["required"],
            "first_activated_at": None,
            "authorized_downgrade": None,
        },
        "status": "CREATED",
        "process": None,
        "ui_url": None,
        "created_at": "2026-08-20T12:00:00Z",
    }


class ProcessSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        created = create_task(self.commands, "t_process", "auto")
        self.credentials = CredentialPoolService(self.store)
        pairs = json.loads(CREDENTIAL_FIXTURE.read_text(encoding="utf-8"))["pairs"]
        self.credentials.configure_pool(pairs)
        draft = image_plan("t_process", 3)
        for index, raw in enumerate(draft["instances"], start=1):
            assigned = self.credentials.create_instance(
                "t_process",
                creation_summary("t_process", raw),
                provider="fake",
                creation_id=f"process_creation_{index}",
                actor=Actor("human", "tester"),
            )
            raw["credential_pair_ref"] = assigned["credential"]["credential_pair_id"]
            raw["credential_pair_revision"] = assigned["credential"][
                "credential_pair_revision"
            ]
        self.commands.save_plan(
            "t_process",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope("save-process-plan", created["revision"]),
        )
        body = GlobalConfigBody.model_validate(
            {
                **GlobalConfigBody().model_dump(mode="json"),
                "supervisor": {
                    "port_range_start": 19100,
                    "port_range_end": 19130,
                    "startup_timeout_seconds": 2,
                    "health_interval_seconds": 0.05,
                    "shutdown_grace_seconds": 0.5,
                },
            }
        )
        self.configuration = ConfigurationService(self.store)
        self.configuration.initialize(body)
        self.supervisor = ProcessSupervisor(
            self.store,
            self.commands,
            self.credentials,
            self.configuration,
        )
        self.spec = ProcessSpec(
            command=(sys.executable, str(FAKE_AGENT)),
            agent_code_ref="fake-agent@1",
        )

    def tearDown(self) -> None:
        self.supervisor.close()
        for path in self.supervisor.launch_root.glob("*.json"):
            record = read_json(path)
            if record["state"] in {"PREPARED", "STARTING", "RUNNING"} and record.get("pid"):
                with suppress(ProcessLookupError):
                    os.killpg(record["pid"], signal.SIGKILL)
        for process in self.supervisor._children.values():
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
        self.store.close()
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
            self.assertTrue(identity["projects_root"].endswith(f"i_image_{index}/work"))

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
        self.assertEqual(third_root.stat().st_mode & 0o777, 0o500)
        claims = read_json(self.supervisor.port_allocator.path)["claims"]
        self.assertEqual(claims, {})

    def test_logs_are_redacted_and_launch_secret_file_is_removed(self) -> None:
        spec = ProcessSpec(
            command=self.spec.command,
            agent_code_ref=self.spec.agent_code_ref,
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
        self.assertNotIn("not-a-secret-p1-01", rendered)
        self.assertNotIn("https://provider-1.invalid/v1", rendered)
        instance_root = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_process"
            / "instances"
            / "i_image_1"
        )
        self.assertFalse((instance_root / "runtime" / ".launch_1.launch-secret.json").exists())
        self.assertNotIn(
            b"not-a-secret-p1-01",
            (instance_root / "logs" / "stdout.log").read_bytes(),
        )
        self.assertEqual(launch["state"], "RUNNING")

    def test_harness_reconcile_keeps_same_process_and_restart_keeps_credentials(self) -> None:
        launch = self._start(1)
        original = self.credentials.resolve_for_instance("t_process", "i_image_1")
        second_supervisor = ProcessSupervisor(
            self.store,
            self.commands,
            self.credentials,
            self.configuration,
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

        revision = self.configuration._read_instance_config(
            "t_process", "i_image_1"
        )["config_revision"]
        self.configuration.update_instance(
            "t_process",
            "i_image_1",
            {"image_runtime_policy": {"watermark": True}},
            expected_revision=revision,
            idempotency_key="runtime-config-restart",
            actor=Actor("human", "tester"),
        )
        restarted = second_supervisor.restart_instance(
            "t_process",
            "i_image_1",
            self.spec,
            launch_id="launch_restart_1",
            attempt_id="attempt_restart_1",
        )
        self.assertNotEqual(restarted["pid"], launch["pid"])
        current = self.credentials.resolve_for_instance("t_process", "i_image_1")
        self.assertEqual(
            (current.credential_pair_id, current.base_url, current.revision),
            (original.credential_pair_id, original.base_url, original.revision),
        )
        config = self.configuration._read_instance_config("t_process", "i_image_1")
        self.assertFalse(config["restart_required"])
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
            self.credentials,
            self.configuration,
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
            command=(sys.executable, str(FAKE_AGENT)),
            agent_code_ref="fake-agent@1",
            public_environment={"FAKE_HEALTHY": "0"},
        )
        with self.assertRaises(HarnessError) as captured:
            self._start(1, unhealthy)
        self.assertEqual(captured.exception.code, "PROCESS_START_FAILED")
        instance = self.store.instance.get("t_process", "i_image_1")
        self.assertEqual(instance["status"], "FAILED_TO_START")
        self.assertEqual(read_json(self.supervisor.port_allocator.path)["claims"], {})

    def test_code_change_is_rejected_before_the_live_process_is_stopped(self) -> None:
        pinned_code = self.root / "pinned-agent.py"
        shutil.copyfile(FAKE_AGENT, pinned_code)
        spec = ProcessSpec(
            command=(sys.executable, str(pinned_code)),
            agent_code_ref="pinned-agent@1",
        )
        launch = self._start(1, spec)
        pinned_code.write_text(
            pinned_code.read_text(encoding="utf-8") + "\n# changed\n",
            encoding="utf-8",
        )
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

    def test_manual_start_confirmation_cannot_be_bypassed(self) -> None:
        created = create_task(self.commands, "t_manual_process", "manual")
        draft = image_plan("t_manual_process")
        assigned = self.credentials.create_instance(
            "t_manual_process",
            creation_summary("t_manual_process", draft["instances"][0]),
            provider="fake",
            creation_id="manual_process_creation",
            actor=Actor("human", "tester"),
        )
        draft["instances"][0]["credential_pair_ref"] = assigned["credential"][
            "credential_pair_id"
        ]
        draft["instances"][0]["credential_pair_revision"] = assigned["credential"][
            "credential_pair_revision"
        ]
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
        os.killpg(launch["pid"], signal.SIGKILL)
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
