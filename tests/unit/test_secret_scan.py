from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.secret_scan import main


class SecretScanTests(unittest.TestCase):
    def test_locked_redaction_fixture_is_allowed_only_at_its_exact_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "agents" / "image_agent_mvp" / "tests" / "test_refactor.py"
            fixture.parent.mkdir(parents=True)
            placeholder = "Bearer " + "sensitive-provider-value"
            fixture.write_text(f'value = "{placeholder}"\n', encoding="utf-8")

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(root), 0)

            copied = root / "copied.py"
            copied.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(root), 1)

    def test_locked_ppt_redaction_fixtures_are_allowed_only_at_exact_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "agents" / "ppt-agent" / "tests" / "test_sqlite_storage.py"
            fixture.parent.mkdir(parents=True)
            placeholders = (
                "Bearer " + "audit-secret-token",
                "Basic " + "YmFzaWMtc2VjcmV0LTAz",
                "Bearer " + "bearer-secret-04",
            )
            fixture.write_text("\n".join(placeholders) + "\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(root), 0)

            copied = root / "copied.py"
            copied.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(root), 1)


if __name__ == "__main__":
    unittest.main()
