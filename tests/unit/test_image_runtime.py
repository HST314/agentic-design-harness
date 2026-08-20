from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from harness.adapters.image_runtime import (
    IMAGE_ENTRYPOINT,
    IMAGE_WEB_REQUIREMENTS,
    ImageRuntimeBuilder,
)
from harness.core.errors import HarnessError

ROOT = Path(__file__).resolve().parents[2]


class ImageRuntimeBuilderTests(unittest.TestCase):
    def test_artifact_is_idempotent_read_only_and_uses_the_install_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            dependencies = root / "dependencies"
            runtime = root / "runtime"
            source.mkdir()
            dependencies.mkdir()
            runtime.mkdir()
            (source / "main_front.py").write_text("app = object()\n", encoding="utf-8")
            (dependencies / "dependency.py").write_text("VALUE = 1\n", encoding="utf-8")
            builder = ImageRuntimeBuilder(
                source,
                dependencies,
                revision="revision_1",
                package_version="1.0.0",
            )

            artifact = builder.prepare(runtime)
            try:
                self.assertEqual(builder.prepare(runtime), artifact)
                self.assertTrue((artifact / IMAGE_ENTRYPOINT).is_file())
                self.assertEqual(
                    (artifact / IMAGE_WEB_REQUIREMENTS).read_text(encoding="utf-8"),
                    (ROOT / "requirements" / "image-agent-web.in").read_text(
                        encoding="utf-8"
                    ),
                )
                for path in artifact.rglob("*"):
                    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
                    self.assertFalse(path.stat().st_mode & writable)
            finally:
                builder._make_removable(artifact)

    def test_symbolic_link_fails_without_leaving_a_partial_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            dependencies = root / "dependencies"
            runtime = root / "runtime"
            source.mkdir()
            dependencies.mkdir()
            runtime.mkdir()
            target = root / "outside.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            (source / "escape.py").symlink_to(target)
            builder = ImageRuntimeBuilder(
                source,
                dependencies,
                revision="revision_1",
                package_version="1.0.0",
            )

            with self.assertRaisesRegex(HarnessError, "symbolic link"):
                builder.prepare(runtime)
            self.assertEqual(list(runtime.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
