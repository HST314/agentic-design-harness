from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters.image_attestation import attest_image_runtime
from harness.adapters.image_lock import ImageAgentReleaseLock, LockedDependencyFile
from harness.adapters.image_runtime import content_tree_sha256
from harness.core.errors import HarnessError
from harness.runtime_identity import PythonInterpreterIdentity, RuntimePackageIdentity


class ImageRuntimeAttestationTests(unittest.TestCase):
    def test_attestation_binds_release_inputs_and_accepts_a_new_dependency_tree(self) -> None:
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
            harness_lock.write_bytes(b"uvicorn==0.35.0\n")
            image_lock.write_bytes(
                b"fastapi==0.141.1\nhttpx==0.28.1\nopenai==1.109.1\n"
                b"pillow==12.3.0\nportalocker==3.2.0\npydantic==2.13.4\n"
                b"pyyaml==6.0.3\n"
            )
            (source / "pyproject.toml").write_text(
                '[project]\nname = "image-agent-mvp"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
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
                schema_version="1.1",
                repository="https://github.com/example/image.git",
                revision="1" * 40,
                package_version="1.0.0",
                contract_version="1.0.0",
                embedded_path="agents/image",
                source_content_sha256=content_tree_sha256(source),
                dependency_files=files,
                dependency_lock_set_sha256=lock_set.hexdigest(),
            )

            identity = PythonInterpreterIdentity(
                implementation="cpython",
                cache_tag="cpython-313",
                version="3.13.7",
                executable=str(root / "image-python"),
                is_virtual_environment=True,
            )
            packages = RuntimePackageIdentity(
                python_executable=identity.executable,
                imports={"main_front": str(source / "main_front.py")},
                distributions={
                    "fastapi": "0.141.1",
                    "httpx": "0.28.1",
                    "openai": "1.109.1",
                    "pillow": "12.3.0",
                    "portalocker": "3.2.0",
                    "pydantic": "2.13.4",
                    "pyyaml": "6.0.3",
                    "uvicorn": "0.35.0",
                },
            )
            with patch(
                "harness.adapters.image_attestation.inspect_python_interpreter",
                return_value=identity,
            ), patch(
                "harness.adapters.image_attestation.inspect_runtime_packages",
                return_value=packages,
            ):
                first = attest_image_runtime(
                    release_lock,
                    source_root=source,
                    dependency_root=dependencies,
                    harness_root=harness,
                    interpreter=root / "image-python",
                )
                replay = attest_image_runtime(
                    release_lock,
                    source_root=source,
                    dependency_root=dependencies,
                    harness_root=harness,
                    interpreter=root / "image-python",
                )

                self.assertEqual(first.identity_sha256, replay.identity_sha256)
                self.assertEqual(first.source_sha256, release_lock.source_content_sha256)
                (dependencies / "package.py").write_text(
                    "VALUE = 2\n", encoding="utf-8"
                )
                changed = attest_image_runtime(
                    release_lock,
                    source_root=source,
                    dependency_root=dependencies,
                    harness_root=harness,
                    interpreter=root / "image-python",
                )
                self.assertNotEqual(first.dependency_sha256, changed.dependency_sha256)
                self.assertNotEqual(first.identity_sha256, changed.identity_sha256)
                (source / "main.py").write_text("MUTATED = True\n", encoding="utf-8")
                with self.assertRaises(HarnessError) as rejected:
                    attest_image_runtime(
                        release_lock,
                        source_root=source,
                        dependency_root=dependencies,
                        harness_root=harness,
                        interpreter=root / "image-python",
                    )
            self.assertEqual(
                rejected.exception.code, "IMAGE_RUNTIME_ATTESTATION_FAILED"
            )


if __name__ == "__main__":
    unittest.main()
