from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from harness.adapters import PptAgentAdapter
from harness.adapters.ppt_attestation import dependency_lock_set_sha256
from harness.adapters.ppt_lock import load_ppt_agent_lock
from harness.core.errors import HarnessError

from tests.runtime_helpers import build_store

ROOT = Path(__file__).resolve().parents[2]


class PptRuntimeAttestationTests(unittest.TestCase):
    def test_dependency_lock_set_is_recomputed_and_detects_file_drift(self) -> None:
        release_lock = load_ppt_agent_lock(ROOT / "agents" / "ppt-agent.lock.json")
        self.assertEqual(
            dependency_lock_set_sha256(
                release_lock, ROOT, ROOT / "agents" / "ppt-agent"
            ),
            release_lock.dependency_lock_set_sha256,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements").mkdir()
            (root / "agents" / "ppt-agent").mkdir(parents=True)
            shutil.copyfile(
                ROOT / "requirements" / "ppt-agent.lock",
                root / "requirements" / "ppt-agent.lock",
            )
            shutil.copyfile(
                ROOT / "agents" / "ppt-agent" / "pyproject.toml",
                root / "agents" / "ppt-agent" / "pyproject.toml",
            )
            with (root / "requirements" / "ppt-agent.lock").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write("# drift\n")
            with self.assertRaisesRegex(HarnessError, "drifted"):
                dependency_lock_set_sha256(
                    release_lock, root, root / "agents" / "ppt-agent"
                )

    def test_adapter_rejects_missing_dependency_root_before_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = build_store(root)
            with self.assertRaisesRegex(HarnessError, "dependency directory is missing"):
                PptAgentAdapter(
                    store,
                    store.contracts,
                    source_root=ROOT / "agents" / "ppt-agent",
                    interpreter=Path(sys.executable).resolve(),
                    dependency_root=root / "missing-dependencies",
                    release_lock=load_ppt_agent_lock(
                        ROOT / "agents" / "ppt-agent.lock.json"
                    ),
                    runtime_policy=ROOT / "config" / "ppt_agent_runtime.yaml",
                    model_config=ROOT / "config" / "ppt_agent_model_config.yaml",
                )
            store.close()


if __name__ == "__main__":
    unittest.main()
