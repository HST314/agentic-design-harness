from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

from harness.services.model_clients import (
    ModelClientFailure,
    OpenAICompatibleProviderAdapter,
)
from runtime_helpers import build_config_snapshot


class ProviderServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    responses: list[dict[str, Any]]


class ProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        server = self.server
        assert isinstance(server, ProviderServer)
        server.requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        response = server.responses.pop(0)
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


class ModelClientContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ProviderServer(("127.0.0.1", 0), ProviderHandler)
        self.server.requests = []
        self.server.responses = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host = self.server.server_address[0]
        port = self.server.server_address[1]
        self.snapshot = build_config_snapshot(base_url=f"http://{host}:{port}")
        self.adapter = OpenAICompatibleProviderAdapter()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_chat_request_maps_schema_tools_idempotency_and_usage(self) -> None:
        self.server.responses.append(
            {
                "id": "provider_request_1",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "status": "NEEDS_INPUT",
                                    "message": "Which size?",
                                    "task_title": None,
                                    "proposal": None,
                                }
                            ),
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                    "prompt_tokens_details": {"cached_tokens": 2},
                    "completion_tokens_details": {"reasoning_tokens": 1},
                },
            }
        )
        schema = {"type": "object"}
        tools = [
            {
                "name": "list_assets",
                "description": "List assets",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        client = self.adapter.text(
            self.snapshot, "ark-text-primary", timeout_seconds=3
        )
        result = client.complete_structured(
            messages=[{"role": "user", "content": "plan"}],
            tools=tools,
            response_schema=schema,
            idempotency_key="master-run-round-0",
        )

        request = self.server.requests[0]
        self.assertEqual(request["path"], "/chat/completions")
        self.assertEqual(request["headers"]["Idempotency-Key"], "master-run-round-0")
        self.assertEqual(request["body"]["model"], "text-model")
        self.assertEqual(request["body"]["tools"][0]["function"], tools[0])
        self.assertEqual(
            request["body"]["response_format"]["json_schema"]["schema"], schema
        )
        self.assertIsNotNone(result.output)
        output = result.output
        assert output is not None
        self.assertEqual(output["status"], "NEEDS_INPUT")
        self.assertEqual(result.usage.total_tokens, 16)
        self.assertEqual(result.usage.cached_input_tokens, 2)
        self.assertEqual(result.usage.reasoning_tokens, 1)

    def test_vision_request_uses_data_url_and_parses_structured_output(self) -> None:
        self.server.responses.append(
            {
                "id": "provider_request_vision",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"summary": "blue", "blocks": [], "warnings": []}
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }
        )
        client = self.adapter.vision(
            self.snapshot, "ark-vlm-primary", timeout_seconds=3
        )
        result = client.inspect_image(
            image_bytes=b"image-bytes",
            media_type="image/png",
            prompt="describe",
            response_schema={"type": "object"},
            idempotency_key="vision-page-1",
        )
        request = self.server.requests[0]
        content = request["body"]["messages"][0]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(result.call_type, "vision_language_model")
        self.assertIsNotNone(result.output)
        output = result.output
        assert output is not None
        self.assertEqual(output["summary"], "blue")

    def test_timeout_is_normalized_without_credentials(self) -> None:
        client = self.adapter.text(
            self.snapshot, "ark-text-primary", timeout_seconds=1
        )
        with (
            patch("harness.services.model_clients.urlopen", side_effect=TimeoutError),
            self.assertRaises(ModelClientFailure) as raised,
        ):
            client.complete_structured(
                messages=[{"role": "user", "content": "plan"}],
                tools=[],
                response_schema={"type": "object"},
                idempotency_key="timeout-request",
            )
        self.assertEqual(raised.exception.code, "MODEL_PROVIDER_UNAVAILABLE")
        self.assertNotIn("test-provider-secret-value", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
