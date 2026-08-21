from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.generate_phase1_evidence import (
    EXPECTED_GATE_STAGES,
    validate_gate_evidence,
)


class Phase1EvidenceTests(unittest.TestCase):
    def test_gate_evidence_binds_passed_exit_codes_commit_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = self._write_gate(root, commit="candidate_sha")

            result, actual_path, log_sha256 = validate_gate_evidence(
                root, "candidate_sha"
            )

            self.assertEqual(actual_path, result_path)
            self.assertEqual(result["status"], "PASSED")
            self.assertEqual(result["log"]["sha256"], log_sha256)

    def test_gate_evidence_rejects_failed_stage_wrong_commit_and_changed_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = self._write_gate(root, commit="candidate_sha")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["stages"][2]["exit_code"] = 1
            result_path.write_text(json.dumps(result), encoding="utf-8")
            with self.assertRaises(SystemExit):
                validate_gate_evidence(root, "candidate_sha")

            self._write_gate(root, commit="candidate_sha")
            with self.assertRaises(SystemExit):
                validate_gate_evidence(root, "another_sha")

            self._write_gate(root, commit="candidate_sha")
            (root / "build" / "g5-gate.log").write_text(
                "changed gate log\n", encoding="utf-8"
            )
            with self.assertRaises(SystemExit):
                validate_gate_evidence(root, "candidate_sha")

    @staticmethod
    def _write_gate(root: Path, *, commit: str) -> Path:
        build = root / "build"
        build.mkdir(parents=True, exist_ok=True)
        log_path = build / "g5-gate.log"
        log_path.write_text("verified gate log\n", encoding="utf-8")
        result = {
            "schema_version": "1.0",
            "verification_command": "make g5-e2e",
            "commit": commit,
            "status": "PASSED",
            "started_at": "2026-08-21T00:00:00Z",
            "completed_at": "2026-08-21T00:01:00Z",
            "stages": [
                {"name": name, "command": command, "exit_code": 0}
                for name, command in EXPECTED_GATE_STAGES
            ],
            "log": {
                "path": "build/g5-gate.log",
                "sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
                "size_bytes": log_path.stat().st_size,
            },
        }
        result_path = build / "g5-gate-result.json"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        return result_path


if __name__ == "__main__":
    unittest.main()
