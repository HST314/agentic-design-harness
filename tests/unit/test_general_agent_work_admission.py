from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

APP_PATH = Path(__file__).resolve().parents[2] / "agents" / "general-agent" / "app.py"
SPEC = importlib.util.spec_from_file_location("managed_general_agent", APP_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GeneralAgentWorkAdmissionTests(unittest.TestCase):
    def test_quiesce_file_is_observed_and_corruption_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "work-admission.json"
            agent = MODULE.GeneralAgent.__new__(MODULE.GeneralAgent)
            agent.work_admission_path = path

            path.write_text(json.dumps({"quiesced": True}), encoding="utf-8")
            self.assertTrue(agent.is_quiesced())

            path.write_text("not-json", encoding="utf-8")
            self.assertTrue(agent.is_quiesced())

    def test_quiesced_chat_rejects_before_persisting_a_message(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "work-admission.json"
            path.write_text(json.dumps({"quiesced": True}), encoding="utf-8")
            agent = MODULE.GeneralAgent.__new__(MODULE.GeneralAgent)
            agent.work_admission_path = path
            agent.lock = threading.RLock()
            agent.running = False

            with self.assertRaises(MODULE.AgentQuiescedError):
                agent.chat("new work")


if __name__ == "__main__":
    unittest.main()
