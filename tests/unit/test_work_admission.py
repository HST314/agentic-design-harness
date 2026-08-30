from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from harness.adapters.work_admission import (
    initialize_work_admission,
    write_work_admission,
)
from harness.core.errors import HarnessError
from harness.storage.atomic import atomic_write_json, read_json


class WorkAdmissionTests(unittest.TestCase):
    def test_initialize_and_toggle_persistent_gate(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "work-admission.json"
            initialize_work_admission(path, "instance_1")
            write_work_admission(
                path, "instance_1", "archive_instance_1", quiesced=True
            )

            admission = read_json(path)
            self.assertTrue(admission["quiesced"])
            self.assertEqual(
                admission["quiesce_operation_id"], "archive_instance_1"
            )

    def test_invalid_gate_fails_closed_at_adapter_boundary(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "work-admission.json"
            atomic_write_json(
                path,
                {"instance_id": "instance_1", "quiesced": "not-a-boolean"},
            )

            with self.assertRaises(HarnessError) as captured:
                initialize_work_admission(path, "instance_1")
            self.assertEqual(captured.exception.code, "PROCESS_START_FAILED")


if __name__ == "__main__":
    unittest.main()
