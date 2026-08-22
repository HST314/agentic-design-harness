from __future__ import annotations

import unittest

import starlette.testclient


class DependencyCompatibilityTests(unittest.TestCase):
    def test_starlette_testclient_uses_httpx2_without_deprecated_fallback(self) -> None:
        self.assertEqual(starlette.testclient.httpx.__name__, "httpx2")


if __name__ == "__main__":
    unittest.main()
