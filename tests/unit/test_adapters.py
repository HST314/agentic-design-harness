from __future__ import annotations

import unittest

from harness.adapters import AdapterRegistry, AgentAdapter, PptAgentAdapter
from harness.core.errors import HarnessError


class AdapterContractTests(unittest.TestCase):
    def test_ppt_adapter_implements_protocol(self) -> None:
        adapter = object.__new__(PptAgentAdapter)
        self.assertIsInstance(adapter, AgentAdapter)

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
