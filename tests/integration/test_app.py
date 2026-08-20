from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings

ROOT = Path(__file__).resolve().parents[2]


class ApplicationTests(unittest.TestCase):
    def test_lifecycle_health_readiness_and_contract_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
            )
            app = create_app(settings)
            with TestClient(app) as client:
                health = client.get("/healthz")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()["status"], "ok")
                self.assertEqual(client.get("/readyz").json(), {"status": "ready"})
                invalid = client.post("/api/v1/contracts/main-task/validate", json={"payload": {}})
                self.assertEqual(invalid.status_code, 422)
                self.assertEqual(invalid.json()["error"]["code"], "VALIDATION_ERROR")
                self.assertNotIn("traceback", invalid.text.lower())
            self.assertFalse(app.state.container.store.writer_lease.acquired)
