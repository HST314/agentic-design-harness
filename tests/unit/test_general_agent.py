from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.services.plan_drafts import (
    master_response_schema,
    materialize_plan_draft,
    validate_master_response,
)

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "managed_general_agent", ROOT / "agents" / "general-agent" / "app.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GeneralAgentRuntimeTests(unittest.TestCase):
    def test_master_plan_materializes_general_execution_card(self) -> None:
        stage = {
            "type": "general",
            "title": "Summarize shared files",
            "required": True,
            "objective": "Read shared files and write a summary.",
            "instructions": ["Use only the shared folder."],
            "input_asset_ids": [],
            "expected_deliveries": [
                {
                    "kind": "document",
                    "role": "summary",
                    "required": True,
                    "accepted_mime_types": ["text/markdown"],
                }
            ],
            "parameters": {},
        }
        response = {
            "status": "PLAN_READY",
            "message": "The general task is ready.",
            "task_title": "Shared file summary",
            "plan_draft": {"stages": [stage]},
        }
        validate_master_response(master_response_schema([]), response)
        proposal = materialize_plan_draft(
            "task_general_plan",
            1,
            response["plan_draft"],
            created_at="2026-08-28T00:00:00Z",
            asset_ids=set(),
        )
        self.assertEqual(proposal["stages"][0]["type"], "general")
        self.assertEqual(proposal["execution_cards"][0]["agent_type"], "general")

    def test_first_message_is_task_card_instruction_and_tool_loop_writes_shared_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                "ARK_BASE_URL": "http://127.0.0.1:1",
                "ARK_API_KEY": "test-secret",
                "GENERAL_AGENT_MODEL": "test-model",
            },
        ):
            shared = Path(temporary)
            card = {
                "card_id": "card_general",
                "task_id": "task_general",
                "instance_id": "instance_general",
                "objective": "Create notes.txt",
                "instructions": ["Write the approved summary."],
            }
            agent = MODULE.GeneralAgent(card, shared, shared / ".state.json")
            first = agent.public_messages()[0]
            self.assertEqual(first["role"], "user")
            self.assertIn("Create notes.txt", first["content"])
            self.assertIn("Write the approved summary.", first["content"])

            responses = iter(
                [
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_write",
                                            "type": "function",
                                            "function": {
                                                "name": "write_file",
                                                "arguments": (
                                                    '{"path":"notes.txt",'
                                                    '"content":"approved"}'
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                    {
                        "id": "provider_response",
                        "choices": [{"message": {"content": "已写入 notes.txt。"}}],
                        "usage": {
                            "prompt_tokens": 9,
                            "completion_tokens": 3,
                            "total_tokens": 12,
                        },
                    },
                ]
            )
            agent._complete = lambda _round: next(responses)
            messages = agent.chat("开始执行")
            self.assertEqual((shared / "notes.txt").read_text(encoding="utf-8"), "approved")
            self.assertEqual(messages[-1]["role"], "assistant")
            self.assertIn("notes.txt", messages[-1]["content"])
            self.assertEqual(agent.state["usage"][0]["agent_type"], "general")
            self.assertEqual(agent.state["usage"][0]["total_tokens"], 12)

    def test_tools_reject_traversal_absolute_paths_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "shared"
            outside = Path(temporary) / "outside"
            root.mkdir()
            outside.mkdir()
            tools = MODULE.SharedFolderTools(root)
            for path in ("../escape.txt", str(outside / "escape.txt"), "nested/../../escape.txt"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    tools.write_file({"path": path, "content": "blocked"})
            (root / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                tools.write_file({"path": "link/escape.txt", "content": "blocked"})
            self.assertFalse((outside / "escape.txt").exists())
            with self.assertRaises(ValueError):
                tools.read_file({"path": ".general-agent-state/state_secret.json"})


if __name__ == "__main__":
    unittest.main()
