from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.generate_workbench_evidence import validate_manifest


class WorkbenchEvidenceTests(unittest.TestCase):
    def test_manifest_requires_all_ordered_acceptance_items_and_existing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence.txt"
            evidence.write_text("verified", encoding="utf-8")
            manifest = {
                "acceptance_items": [
                    {
                        "rfc_item": number,
                        "claim": f"claim {number}",
                        "commands": ["make verify"],
                        "evidence_files": ["evidence.txt"],
                    }
                    for number in range(1, 16)
                ]
            }

            items = validate_manifest(root, manifest)
            self.assertEqual(len(items), 15)

            manifest["acceptance_items"] = list(reversed(manifest["acceptance_items"]))
            with self.assertRaisesRegex(SystemExit, "items 1 through 15"):
                validate_manifest(root, manifest)

    def test_manifest_rejects_missing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = {
                "acceptance_items": [
                    {
                        "rfc_item": number,
                        "claim": f"claim {number}",
                        "commands": ["make verify"],
                        "evidence_files": ["missing.txt"],
                    }
                    for number in range(1, 16)
                ]
            }

            with self.assertRaisesRegex(SystemExit, "missing workbench evidence files"):
                validate_manifest(root, manifest)


if __name__ == "__main__":
    unittest.main()
