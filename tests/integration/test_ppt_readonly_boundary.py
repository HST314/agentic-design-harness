from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.adapters import PptAgentAdapter, PrepareRequest
from harness.adapters.ppt_lock import load_ppt_agent_lock

from tests.runtime_helpers import build_store

ROOT = Path(__file__).resolve().parents[2]


class PptReadOnlyBoundaryTests(unittest.TestCase):
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
            self.assertEqual((spec.writable_roots[0] / "allowed.txt").read_text(), "ok")
            store.close()


if __name__ == "__main__":
    unittest.main()
