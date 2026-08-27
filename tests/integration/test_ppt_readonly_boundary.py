from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from harness.adapters import PptAgentAdapter, PrepareRequest
from harness.adapters.ppt_lock import load_ppt_agent_lock
from harness.process_worker import _mirror_until_stopped

from tests.runtime_helpers import build_store

ROOT = Path(__file__).resolve().parents[2]


class PptReadOnlyBoundaryTests(unittest.TestCase):
    def test_worker_updates_running_child_view_without_shared_write_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            shared = root / "shared"
            mirror = root / "mirror"
            projects = root / "projects"
            for path in (shared, mirror, projects):
                path.mkdir()
            original = shared / "bundle_a.png"
            original.write_bytes(b"bundle-a")
            spec_path = root / "launch.json"
            child_code = (
                "import sys,time; from pathlib import Path; "
                "mirror,projects=map(Path,sys.argv[1:]); deadline=time.monotonic()+5; "
                "added=mirror/'bundle_b.png'; "
                "\nwhile not added.exists() and time.monotonic()<deadline: time.sleep(.02)"
                "\nif added.read_bytes()!=b'bundle-b': raise SystemExit(2)"
                "\ntry: (mirror/'tamper.txt').write_text('bad'); raise SystemExit(3)"
                "\nexcept PermissionError: pass"
                "\n(projects/'allowed.txt').write_text('ok')"
            )
            spec_path.write_text(
                json.dumps(
                    {
                        "environment": {
                            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "PYTHONUNBUFFERED": "1",
                        },
                        "secret_environment_names": [],
                        "inherited_fds": [],
                        "writable_roots": [str(projects)],
                        "read_only_mirrors": [
                            {"source": str(shared), "destination": str(mirror)}
                        ],
                        "command": [
                            sys.executable,
                            "-c",
                            child_code,
                            str(mirror),
                            str(projects),
                        ],
                        "cwd": str(root),
                        "stdout_path": str(root / "stdout.log"),
                        "stderr_path": str(root / "stderr.log"),
                        "handshake_path": str(root / "child.json"),
                    }
                ),
                encoding="utf-8",
            )
            worker = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "backend" / "harness" / "process_worker.py"),
                    str(spec_path),
                ]
            )
            try:
                deadline = time.monotonic() + 3
                while not (mirror / original.name).exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                added = shared / "bundle_b.png"
                added.write_bytes(b"bundle-b")
                self.assertEqual(worker.wait(timeout=8), 0)
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=3)
            self.assertEqual(original.read_bytes(), b"bundle-a")
            self.assertEqual(added.read_bytes(), b"bundle-b")
            self.assertFalse((shared / "tamper.txt").exists())
            self.assertFalse((mirror / "tamper.txt").exists())
            self.assertEqual((projects / "allowed.txt").read_text(), "ok")

    def test_shared_write_fails_and_original_asset_remains_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = build_store(root)
            adapter = PptAgentAdapter(
                store,
                store.contracts,
                source_root=ROOT / "agents" / "ppt-agent",
                interpreter=Path(sys.executable).resolve(),
                dependency_root=ROOT / ".runtime" / "ppt-agent-deps",
                release_lock=load_ppt_agent_lock(ROOT / "agents" / "ppt-agent.lock.json"),
                runtime_policy=ROOT / "config" / "ppt_agent_runtime.yaml",
                model_config=ROOT / "config" / "ppt_agent_model_config.yaml",
            )
            task_id, instance_id = "task_read_only", "ppt_read_only"
            task_root = store.layout.workspace_root / "tasks" / task_id
            shared = task_root / "resources" / "shared"
            shared.mkdir(parents=True)
            original = shared / "approved.png"
            original.write_bytes(b"approved-asset")
            before = hashlib.sha256(original.read_bytes()).hexdigest()
            card = {
                "schema_version": "1.0",
                "card_id": "card_read_only",
                "revision": 1,
                "task_id": task_id,
                "stage_id": "stage_ppt",
                "instance_id": instance_id,
                "agent_type": "ppt",
                "objective": "Create a deck",
                "instructions": [],
                "input_assets": [],
                "expected_deliveries": [{
                    "kind": "archive",
                    "role": "html_ppt",
                    "required": True,
                    "accepted_mime_types": ["application/zip"],
                }],
                "parameters": {"input_source": "shared"},
                "created_at": "2026-08-27T00:00:00Z",
            }
            instance = {
                "instance_id": instance_id,
                "task_id": task_id,
                "stage_id": "stage_ppt",
                "agent_type": "ppt",
            }
            spec = adapter.prepare(
                PrepareRequest(instance, card, task_root, task_root / "unused.yaml")
            )
            snapshot = Path(spec.public_environment["PPT_AGENT_IMAGES_ROOT"])
            self.assertEqual(spec.read_only_mirrors, ((shared.resolve(), snapshot),))
            stop = threading.Event()
            failed = threading.Event()
            mirror = threading.Thread(
                target=_mirror_until_stopped,
                args=(spec.read_only_mirrors, stop, failed),
            )
            mirror.start()
            added = shared / "bundle_after_start.png"
            added.write_bytes(b"new-approved-asset")
            deadline = time.monotonic() + 3
            while not (snapshot / added.name).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            stop.set()
            mirror.join(timeout=1)
            self.assertFalse(failed.is_set())
            self.assertEqual((snapshot / added.name).read_bytes(), b"new-approved-asset")
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json,sys; from pathlib import Path; "
                    "sys.path.insert(0,sys.argv[1]); "
                    "from write_sandbox import apply_write_sandbox; "
                    "apply_write_sandbox([sys.argv[2]]); p=Path(sys.argv[3]); "
                    "\ntry: p.write_bytes(b'tampered'); ok=True\n"
                    "except PermissionError: ok=False\n"
                    "Path(sys.argv[2], 'allowed.txt').write_text('ok'); "
                    "print(json.dumps({'write_succeeded':ok}))",
                    str(ROOT / "backend" / "harness"),
                    str(spec.writable_roots[0]),
                    str(snapshot / original.name),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertFalse(json.loads(probe.stdout)["write_succeeded"])
            self.assertEqual(original.read_bytes(), b"approved-asset")
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), before)
            self.assertEqual(added.read_bytes(), b"new-approved-asset")
            self.assertEqual((spec.writable_roots[0] / "allowed.txt").read_text(), "ok")
            store.close()


if __name__ == "__main__":
    unittest.main()
