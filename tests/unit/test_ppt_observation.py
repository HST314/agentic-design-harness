from __future__ import annotations

import unittest
from unittest.mock import patch

from harness.adapters.ppt import PptAgentAdapter


class PptObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = PptAgentAdapter.__new__(PptAgentAdapter)

    def observation(self, view: dict) -> object:
        with (
            patch.object(self.adapter, "_task_id_for_instance", return_value="task_ppt"),
            patch.object(self.adapter, "_base_url", return_value="http://127.0.0.1:19000"),
            patch.object(self.adapter, "_request", return_value=view),
        ):
            return self.adapter.get_status("instance_ppt")

    def test_every_ppt_human_gate_maps_to_waiting_approval(self) -> None:
        cases = [
            ("intake_clarify", "waiting_clarification", ["answer_clarification"]),
            (
                "narrative_structure",
                "waiting_human_approval",
                ["approve_narrative"],
            ),
            ("slide_outline", "waiting_human_approval", ["approve_outline"]),
            ("ppt_sample", "waiting_human_approval", ["approve_sample"]),
            ("ppt_full", "waiting_human_approval", ["approve_full_deck"]),
        ]
        for state, phase, capabilities in cases:
            with self.subTest(state=state):
                observed = self.observation(
                    {
                        "state": state,
                        "phase": phase,
                        "checkpoint_id": f"checkpoint_{state}",
                        "capabilities": capabilities,
                        "active_job": None,
                    }
                )
                self.assertEqual(observed.status, "WAITING_APPROVAL")
                self.assertEqual(observed.step_id, f"{state}:{phase}")
                self.assertEqual(observed.details["job_id"], f"checkpoint_{state}")

    def test_active_generation_overrides_a_stale_waiting_snapshot(self) -> None:
        observed = self.observation(
            {
                "state": "ppt_sample",
                "phase": "waiting_human_approval",
                "checkpoint_id": "checkpoint_sample",
                "capabilities": ["approve_sample"],
                "active_job": {"status": "running"},
            }
        )
        self.assertEqual(observed.status, "RUNNING")
        self.assertFalse(observed.details["completed"])

    def test_acceptance_is_the_completed_ppt_projection(self) -> None:
        observed = self.observation(
            {
                "state": "acceptance",
                "phase": "ready_for_review",
                "checkpoint_id": "checkpoint_acceptance",
                "capabilities": ["inspect_full_deck"],
                "active_job": None,
            }
        )
        self.assertEqual(observed.status, "RUNNING")
        self.assertTrue(observed.details["completed"])


if __name__ == "__main__":
    unittest.main()
