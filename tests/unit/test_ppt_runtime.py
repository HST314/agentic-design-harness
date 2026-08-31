from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters.image_runtime import content_tree_sha256, dependency_tree_sha256
from harness.adapters.ppt_runtime import PptRuntimeBuilder
from harness.core.errors import HarnessError


class SimulatedWindowsOSError(OSError):
    winerror: int


class PptRuntimeBuilderTests(unittest.TestCase):
    @staticmethod
    def _builder(root: Path) -> PptRuntimeBuilder:
        source = root / "source"
        dependencies = root / "dependencies"
        requirements = root / "ppt-agent.lock"
        source.mkdir()
        dependencies.mkdir()
        (source / "main_front.py").write_text("app = object()\n", encoding="utf-8")
        (source / "pyproject.toml").write_text(
            '[project]\nname = "ppt-agent"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        (dependencies / "dependency.py").write_text("VALUE = 1\n", encoding="utf-8")
        requirements.write_text("dependency==1 --hash=sha256:abc\n", encoding="utf-8")
        return PptRuntimeBuilder(
            source,
            dependencies,
            requirements,
            revision="revision_1",
            package_version="1.0.0",
            source_content_sha256=content_tree_sha256(source),
            dependency_content_sha256=dependency_tree_sha256(dependencies),
        )

    def test_artifact_is_read_only_idempotent_and_recovers_permission_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = self._builder(root)
            cache = root / "runtime"

            artifact = builder.prepare(cache)
            self.assertFalse(builder.cache_hit)
            self.assertEqual(builder.prepare(cache), artifact)
            self.assertTrue(builder.cache_hit)
            writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
            self.assertFalse(artifact.stat().st_mode & writable)
            self.assertFalse((artifact / "main_front.py").stat().st_mode & writable)

            (artifact / "main_front.py").chmod(0o644)
            (artifact / "main_front.py").write_text("CORRUPTED = True\n", encoding="utf-8")

            self.assertEqual(builder.prepare(cache), artifact)
            self.assertFalse(builder.cache_hit)
            self.assertEqual(
                (artifact / "main_front.py").read_text(encoding="utf-8"),
                "app = object()\n",
            )
            builder._remove_tree(artifact)

    def test_interrupted_read_only_artifact_is_cleaned_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = self._builder(root)
            cache = root / "runtime"
            cache.mkdir()
            interrupted = cache / f".{builder.identity_sha256}-{'f' * 32}"
            interrupted.mkdir()
            partial = interrupted / "partial.py"
            partial.write_text("PARTIAL = True\n", encoding="utf-8")
            partial.chmod(0o444)
            interrupted.chmod(0o555)

            artifact = builder.prepare(cache)

            self.assertFalse(interrupted.exists())
            self.assertTrue(artifact.is_dir())
            builder._remove_tree(artifact)

    def test_publish_failure_preserves_safe_io_diagnostics_and_cleans_partial_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = self._builder(root)
            cache = root / "runtime"
            failure = SimulatedWindowsOSError(13, "simulated rename failure")
            failure.winerror = 5

            with (
                patch.object(builder, "_publish_artifact", side_effect=failure),
                self.assertRaises(HarnessError) as rejected,
            ):
                builder.prepare(cache)

            self.assertEqual(rejected.exception.code, "ADAPTER_UNAVAILABLE")
            self.assertEqual(rejected.exception.details["stage"], "publish_artifact")
            self.assertEqual(rejected.exception.details["errno"], 13)
            self.assertEqual(rejected.exception.details["winerror"], 5)
            self.assertEqual(list(cache.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
