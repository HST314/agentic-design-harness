from __future__ import annotations

import json
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from harness.adapters import (
    AdapterRegistry,
    AgentAdapter,
    AgentWorkState,
    GeneralAgentAdapter,
    PptAgentAdapter,
    UnavailableAgentAdapter,
)
from harness.core.errors import HarnessError


class AdapterContractTests(unittest.TestCase):
    @staticmethod
    def _general_adapter(process: dict | None) -> GeneralAgentAdapter:
        adapter = object.__new__(GeneralAgentAdapter)
        adapter.host = "127.0.0.1"
        adapter.store = SimpleNamespace(
            instance=SimpleNamespace(
                get=lambda task_id, instance_id: {"process": process}
            )
        )
        adapter._task_id_for_instance = lambda instance_id: "task_general"
        return adapter

    def test_general_work_probe_reports_real_running_flag(self) -> None:
        adapter = self._general_adapter(
            {"state": "RUNNING", "port": 8123}
        )

        for running, expected in (
            (True, AgentWorkState.ACTIVE),
            (False, AgentWorkState.IDLE),
        ):
            response = BytesIO(json.dumps({"running": running}).encode())
            with self.subTest(running=running), patch(
                "harness.adapters.general.urlopen", return_value=response
            ):
                self.assertEqual(adapter.probe_work_state("instance_general"), expected)

    def test_general_work_probe_failure_is_unknown(self) -> None:
        stopped = self._general_adapter(None)
        self.assertEqual(
            stopped.probe_work_state("instance_general"), AgentWorkState.UNKNOWN
        )

        running = self._general_adapter({"state": "RUNNING", "port": 8123})
        with patch("harness.adapters.general.urlopen", side_effect=OSError("offline")):
            self.assertEqual(
                running.probe_work_state("instance_general"), AgentWorkState.UNKNOWN
            )

    def test_ppt_adapter_implements_protocol(self) -> None:
        adapter = object.__new__(PptAgentAdapter)
        self.assertIsInstance(adapter, AgentAdapter)

    def test_unavailable_adapter_implements_protocol(self) -> None:
        adapter = UnavailableAgentAdapter(
            "ppt",
            HarnessError("ADAPTER_UNAVAILABLE", "PPT is not available."),
            "Repair the PPT runtime.",
        )

        self.assertIsInstance(adapter, AgentAdapter)
        self.assertFalse(adapter.available)
        self.assertEqual(adapter.recover({}).status, "UNAVAILABLE")

    def test_registry_is_explicit_and_rejects_duplicate_ownership(self) -> None:
        adapter = object.__new__(PptAgentAdapter)
        registry = AdapterRegistry([adapter])
        self.assertIs(registry.get("ppt"), adapter)
        self.assertEqual(registry.describe(), [{"agent_type": "ppt", "available": True}])
        with self.assertRaises(HarnessError) as missing:
            registry.get("image")
        self.assertEqual(missing.exception.code, "ADAPTER_UNAVAILABLE")
        with self.assertRaises(HarnessError) as duplicate:
            registry.register(object.__new__(PptAgentAdapter))
        self.assertEqual(duplicate.exception.code, "IDEMPOTENCY_CONFLICT")


if __name__ == "__main__":
    unittest.main()
