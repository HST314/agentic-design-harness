from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.domain.commands import CommandEnvelope
from runtime_helpers import create_task, envelope, image_plan, register_model_call_attempt

ROOT = Path(__file__).resolve().parents[2]


class G4ApiTests(unittest.TestCase):
    def test_usage_budget_config_and_key_pool_are_exposed_without_secret_echo(self) -> None:
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
                paid_smoke = client.post(
                    "/api/v1/config/diagnostics/paid-smoke",
                    json={},
                )
                self.assertEqual(paid_smoke.status_code, 404, paid_smoke.text)
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

                global_config = client.get("/api/v1/config/global")
                self.assertEqual(global_config.status_code, 200, global_config.text)
                config = deepcopy(global_config.json()["config"])
                revision = config.pop("revision")
                config["image_runtime_policy"]["max_auto_questions"] = 4
                updated_config = client.put(
                    "/api/v1/config/global",
                    json={
                        "config": config,
                        "operation_id": "update_g4_config",
                        "envelope": self._envelope(
                            "update-g4-config-envelope", revision, "human"
                        ),
                    },
                )
                self.assertEqual(updated_config.status_code, 200, updated_config.text)
                self.assertEqual(
                    updated_config.json()["config"]["image_runtime_policy"][
                        "max_auto_questions"
                    ],
                    4,
                )
                instance_config = client.get("/api/v1/instances/i_image_1/config")
                self.assertEqual(instance_config.status_code, 200, instance_config.text)
                self.assertEqual(
                    instance_config.json()["config"]["source_global_revision"],
                    revision + 1,
                )

                secret = "synthetic-g4-api-key"
                key_pool = client.put(
                    "/api/v1/key-pool",
                    json={
                        "pairs": [
                            {
                                "credential_pair_id": "cred_g4_api",
                                "provider": "fake",
                                "key_id": "key_g4_api",
                                "base_url": "https://provider.invalid/v1",
                                "api_key": secret,
                                "api_key_env": "FAKE_API_KEY",
                                "base_url_env": "FAKE_BASE_URL",
                                "revision": 1,
                                "enabled": True,
                            }
                        ],
                        "envelope": self._envelope("update-key-pool", 0, "human"),
                    },
                )
                self.assertEqual(key_pool.status_code, 200, key_pool.text)
                self.assertNotIn(secret, key_pool.text)
                redacted = client.get("/api/v1/key-pool")
                self.assertNotIn(secret, redacted.text)
                self.assertEqual(redacted.json()["items"][0]["key_tail"], "-key")

                usage = {
                    "schema_version": "1.0",
                    "event_id": "usage_g4_api",
                    "task_id": "t_g4_api",
                    "instance_id": "i_image_1",
                    "agent_type": "image",
                    "request_id": "provider_request_g4",
                    "model": "image-model",
                    "credential_pair_ref": "cred_test_01",
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
