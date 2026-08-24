from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from harness.adapters.image_attestation import attest_image_runtime
from harness.adapters.image_lock import ImageAgentReleaseLock, LockedDependencyFile
from harness.adapters.image_runtime import content_tree_sha256, dependency_tree_sha256
from harness.core.errors import HarnessError


class ImageRuntimeAttestationTests(unittest.TestCase):
    def test_attestation_binds_source_dependencies_and_lock_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / "harness"
            source = root / "image"
            dependencies = root / "dependencies"
            harness.mkdir()
            source.mkdir()
            dependencies.mkdir()
            harness_lock = harness / "requirements-runtime.txt"
            image_lock = source / "requirements.lock"
            harness_lock.write_bytes(b"harness==1\n")
            image_lock.write_bytes(b"image==1\n")
            (source / "main.py").write_bytes(b"VALUE = 1\n")
            (dependencies / "package.py").write_bytes(b"VALUE = 1\n")
            files = (
                LockedDependencyFile(
                    "harness",
                    harness_lock.name,
                    hashlib.sha256(harness_lock.read_bytes()).hexdigest(),
                ),
                LockedDependencyFile(
                    "image_agent",
                    image_lock.name,
                    hashlib.sha256(image_lock.read_bytes()).hexdigest(),
                ),
            )
            lock_set = hashlib.sha256()
            for item, path in ((files[0], harness_lock), (files[1], image_lock)):
                lock_set.update(f"{item.scope}:{item.path}".encode())
                lock_set.update(b"\0")
                lock_set.update(path.read_bytes())
                lock_set.update(b"\0")
            release_lock = ImageAgentReleaseLock(
                schema_version="1.0",
                repository="https://github.com/example/image.git",
                revision="1" * 40,
                package_version="1.0.0",
                contract_version="1.0.0",
                embedded_path="agents/image",
                source_content_sha256=content_tree_sha256(source),
                dependency_files=files,
                dependency_lock_set_sha256=lock_set.hexdigest(),
                runtime_dependency_tree_sha256=dependency_tree_sha256(dependencies),
            )

            first = attest_image_runtime(
                release_lock,
                source_root=source,
                dependency_root=dependencies,
                harness_root=harness,
            )
            replay = attest_image_runtime(
                release_lock,
                source_root=source,
                dependency_root=dependencies,
                harness_root=harness,
            )

            self.assertEqual(first.identity_sha256, replay.identity_sha256)
            self.assertEqual(first.source_sha256, release_lock.source_content_sha256)
            (source / "main.py").write_text("MUTATED = True\n", encoding="utf-8")
            with self.assertRaises(HarnessError) as rejected:
                attest_image_runtime(
                    release_lock,
                    source_root=source,
                    dependency_root=dependencies,
                    harness_root=harness,
                )
            self.assertEqual(
                rejected.exception.code, "IMAGE_RUNTIME_ATTESTATION_FAILED"
            )


if __name__ == "__main__":
    unittest.main()
