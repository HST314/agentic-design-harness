from __future__ import annotations

import unittest

from harness.adapters import AdapterRegistry, AgentAdapter, PptAgentContractAdapter
from harness.core.errors import HarnessError


class AdapterContractTests(unittest.TestCase):
    def test_ppt_placeholder_implements_protocol_and_fails_operational_calls(self) -> None:
        adapter = PptAgentContractAdapter()
        self.assertIsInstance(adapter, AgentAdapter)
        self.assertTrue(
            adapter.validate_task_card({"agent_type": "ppt"}).valid
        )
        self.assertEqual(adapter.get_status("i_ppt").status, "UNAVAILABLE")
        self.assertIsNone(adapter.get_ui_url("i_ppt"))
        with self.assertRaises(HarnessError) as unavailable:
            adapter.start("i_ppt", "start_ppt")
        self.assertEqual(unavailable.exception.code, "ADAPTER_UNAVAILABLE")

    def test_registry_is_explicit_and_rejects_duplicate_ownership(self) -> None:
        adapter = PptAgentContractAdapter()
        registry = AdapterRegistry([adapter])
        self.assertIs(registry.get("ppt"), adapter)
        self.assertEqual(
            registry.describe(), [{"agent_type": "ppt", "available": False}]
        )
        with self.assertRaises(HarnessError) as missing:
            registry.get("image")
        self.assertEqual(missing.exception.code, "ADAPTER_UNAVAILABLE")
        with self.assertRaises(HarnessError) as duplicate:
            registry.register(PptAgentContractAdapter())
        self.assertEqual(duplicate.exception.code, "IDEMPOTENCY_CONFLICT")


if __name__ == "__main__":
    unittest.main()
