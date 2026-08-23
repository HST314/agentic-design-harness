from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.domain.commands import CommandEnvelope
from runtime_helpers import create_task, envelope, image_plan, register_model_call_attempt

ROOT = Path(__file__).resolve().parents[2]


class G4ApiTests(unittest.TestCase):
    def test_usage_and_budget_remain_available_while_legacy_config_routes_are_gone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(
                HarnessSettings(
                    control_root=root / "control-data",
                    workspace_root=root / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                )
            )
            with TestClient(app) as client:
                for method, route in (
                    ("post", "/api/v1/config/diagnostics/paid-smoke"),
                    ("post", "/api/v1/config/diagnostics/preflight"),
                    ("get", "/api/v1/config/global"),
                    ("get", "/api/v1/key-pool"),
                    ("get", "/api/v1/instances/i_image_1/config"),
                ):
                    with self.subTest(route=route):
                        response = (
                            client.post(route, json={})
                            if method == "post"
                            else client.get(route)
                        )
                        self.assertEqual(response.status_code, 404, response.text)
                container = app.state.container
                create_task(container.commands, "t_g4_api")
                container.commands.save_plan(
                    "t_g4_api",
                    **image_plan("t_g4_api"),
                    envelope=envelope("save-g4-api", 1),
                )
                register_model_call_attempt(
                    container.store,
                    "t_g4_api",
                    "i_image_1",
                    "attempt_initial",
                )

                usage = {
                    "schema_version": "1.0",
                    "event_id": "usage_g4_api",
                    "task_id": "t_g4_api",
                    "instance_id": "i_image_1",
                    "agent_type": "image",
                    "request_id": "provider_request_g4",
                    "model": "image-model",
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "cached_input_tokens": 5,
                    "reasoning_tokens": 2,
                    "total_tokens": 30,
                    "occurred_at": "2026-08-21T02:00:00Z",
                }
                ingested = client.post(
                    "/api/v1/internal/instances/i_image_1/usage-events",
                    json={
                        "events": [usage],
                        "collection_complete": True,
                        "envelope": self._envelope(
                            "report-g4-usage", 0, "adapter", "image_adapter"
                        ),
                    },
                )
                self.assertEqual(ingested.status_code, 200, ingested.text)
                summary = client.get("/api/v1/tasks/t_g4_api/usage?refresh=false")
                self.assertEqual(summary.json()["tokens"]["total_tokens"], 30)
                self.assertEqual(summary.json()["cost"]["completeness"], "UNKNOWN")

                budget = client.put(
                    "/api/v1/tasks/t_g4_api/retry-budget",
                    json={
                        "retry_policy": {
                            "max_auto_retries_per_retry_group": 1,
                            "max_auto_retry_tokens_task": 100,
                            "retry_token_reservation_by_agent": {"image": 100},
                            "max_auto_retry_cost_micros": None,
                            "price_catalog_revision": None,
                        },
                        "operation_id": "configure_g4_api_budget",
                        "envelope": self._envelope(
                            "configure-g4-api-budget", 0, "human"
                        ),
                    },
                )
                self.assertEqual(budget.status_code, 200, budget.text)
                caller_group = self._retry(
                    "attempt_g4_api_forged_group", "request-g4-api-forged-group"
                )
                caller_group["retry_group_id"] = "retry_group_caller_controlled"
                rejected_group = client.post(
                    "/api/v1/instances/i_image_1/retries",
                    json=caller_group,
                )
                self.assertEqual(rejected_group.status_code, 422, rejected_group.text)
                first_retry = client.post(
                    "/api/v1/instances/i_image_1/retries",
                    json=self._retry("attempt_g4_api_1", "request-g4-api-retry-1"),
                )
                self.assertEqual(first_retry.status_code, 200, first_retry.text)
                denied_retry = client.post(
                    "/api/v1/instances/i_image_1/retries",
                    json=self._retry("attempt_g4_api_2", "request-g4-api-retry-2"),
                )
                self.assertEqual(denied_retry.status_code, 409, denied_retry.text)
                self.assertEqual(
                    denied_retry.json()["error"]["code"], "BUDGET_GATE_DENIED"
                )
                approval_id = denied_retry.json()["error"]["details"]["approval_id"]
                approval = client.get(f"/api/v1/approvals/{approval_id}").json()
                resolved = client.post(
                    f"/api/v1/approvals/{approval_id}/resolve",
                    json={
                        "decision": "APPROVED",
                        "action": "approve_once",
                        "payload": {},
                        "operation_id": "approve_g4_api_budget",
                        "envelope": self._envelope(
                            "approve-g4-api-budget",
                            approval["approval_revision"],
                            "human",
                        ),
                    },
                )
                self.assertEqual(resolved.status_code, 200, resolved.text)
                self.assertEqual(resolved.json()["attempt"]["status"], "AUTHORIZED")

    @staticmethod
    def _envelope(
        key: str,
        expected_revision: int,
        actor_type: str,
        actor_id: str = "api_operator",
    ) -> dict[str, object]:
        return CommandEnvelope(
            idempotency_key=key,
            actor_type=actor_type,
            actor_id=actor_id,
            expected_revision=expected_revision,
        ).model_dump(mode="json")

    def _retry(self, attempt_id: str, operation_id: str) -> dict[str, object]:
        return {
            "attempt_id": attempt_id,
            "retry_of_attempt_id": "attempt_initial",
            "operation_id": operation_id,
            "envelope": self._envelope(
                f"{operation_id}-envelope", 0, "master", "master_default"
            ),
        }
