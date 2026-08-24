from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from harness.adapters.image_runtime import (
    IMAGE_ENTRYPOINT,
    IMAGE_WEB_REQUIREMENTS,
    ImageRuntimeBuilder,
    content_tree_sha256,
    dependency_tree_sha256,
)
from harness.core.errors import HarnessError
from harness.storage.atomic import read_json

ROOT = Path(__file__).resolve().parents[2]


class ImageRuntimeBuilderTests(unittest.TestCase):
    def test_tree_digest_normalizes_text_eol_but_preserves_binary_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            linux = root / "linux"
            windows = root / "windows"
            linux.mkdir()
            windows.mkdir()
            (linux / "module.py").write_bytes(b"VALUE = 1\n")
            (windows / "module.py").write_bytes(b"VALUE = 1\r\n")

            self.assertEqual(content_tree_sha256(linux), content_tree_sha256(windows))
            self.assertNotEqual(
                dependency_tree_sha256(linux), dependency_tree_sha256(windows)
            )

            (linux / "payload.bin").write_bytes(b"\0data\n")
            (windows / "payload.bin").write_bytes(b"\0data\r\n")
            self.assertNotEqual(content_tree_sha256(linux), content_tree_sha256(windows))

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
            source_sha256 = content_tree_sha256(source)
            dependency_sha256 = dependency_tree_sha256(dependencies)
            builder = ImageRuntimeBuilder(
                source,
                dependencies,
                revision="revision_1",
                package_version="1.0.0",
                source_content_sha256=source_sha256,
                dependency_content_sha256=dependency_sha256,
            )

            artifact = builder.prepare(runtime)
            try:
                self.assertEqual(builder.prepare(runtime), artifact)
                self.assertTrue((artifact / IMAGE_ENTRYPOINT).is_file())
                marker = read_json(artifact / ".harness-runtime-artifact.json")
                self.assertEqual(marker["source_content_sha256"], source_sha256)
                self.assertEqual(marker["dependency_content_sha256"], dependency_sha256)
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
                source_content_sha256="0" * 64,
                dependency_content_sha256="0" * 64,
            )

            with self.assertRaisesRegex(HarnessError, "symbolic link"):
                builder.prepare(runtime)
            self.assertEqual(list(runtime.iterdir()), [])

    def test_cached_artifact_rejects_permission_drift_and_root_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            dependencies = root / "dependencies"
            runtime = root / "runtime"
            source.mkdir()
            dependencies.mkdir()
            (source / "main_front.py").write_text("app = object()\n", encoding="utf-8")
            (dependencies / "dependency.py").write_text("VALUE = 1\n", encoding="utf-8")
            builder = ImageRuntimeBuilder(
                source,
                dependencies,
                revision="revision_1",
                package_version="1.0.0",
                source_content_sha256=content_tree_sha256(source),
                dependency_content_sha256=dependency_tree_sha256(dependencies),
            )

            artifact = builder.prepare(runtime)
            try:
                (artifact / "main_front.py").chmod(0o644)
                with self.assertRaisesRegex(HarnessError, "not read-only"):
                    builder.prepare(runtime)
            finally:
                builder._make_removable(artifact)

            outside = root / "outside"
            outside.mkdir()
            symlink_runtime = root / "symlink-runtime"
            symlink_artifacts = symlink_runtime / "image-artifacts"
            symlink_artifacts.mkdir(parents=True)
            (symlink_artifacts / builder.identity_sha256).symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaisesRegex(HarnessError, "not a safe directory"):
                builder.prepare(symlink_runtime)

    def test_mutated_source_or_dependency_fails_content_attestation(self) -> None:
        for mutated_tree in ("source", "dependency"):
            with (
                self.subTest(mutated_tree=mutated_tree),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                source = root / "source"
                dependencies = root / "dependencies"
                runtime = root / "runtime"
                source.mkdir()
                dependencies.mkdir()
                runtime.mkdir()
                source_file = source / "main_front.py"
                dependency_file = dependencies / "dependency.py"
                source_file.write_text("app = object()\n", encoding="utf-8")
                dependency_file.write_text("VALUE = 1\n", encoding="utf-8")
                builder = ImageRuntimeBuilder(
                    source,
                    dependencies,
                    revision="revision_1",
                    package_version="1.0.0",
                    source_content_sha256=content_tree_sha256(source),
                    dependency_content_sha256=dependency_tree_sha256(dependencies),
                )

                target = source_file if mutated_tree == "source" else dependency_file
                target.write_text("MUTATED = True\n", encoding="utf-8")

                with self.assertRaisesRegex(
                    HarnessError, f"{mutated_tree} content"
                ) as rejected:
                    builder.prepare(runtime)
                self.assertEqual(rejected.exception.code, "PROCESS_START_FAILED")
                self.assertEqual(list(runtime.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
