"""Frontend contract generation remains deterministic and current."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class FrontendContractGenerationTests(unittest.TestCase):
    def test_generated_types_match_frozen_contracts(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/generate_frontend_contracts.py", "--check"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
