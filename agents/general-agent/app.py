"""Managed general-purpose Agent with a two-tool filesystem sandbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MAX_BODY_BYTES = 64 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_MODEL_REQUEST_BYTES = 16 * 1024 * 1024
MAX_HISTORY_MESSAGES = 200
MAX_TOOL_CALLS_PER_ROUND = 16
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one UTF-8 text file from the current task shared folder.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 512},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or replace one UTF-8 text file in the current task shared folder."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 512},
                    "content": {"type": "string", "maxLength": MAX_FILE_BYTES},
                },
            },
        },
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SharedFolderTools:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir() or self.root.is_symlink():
            raise RuntimeError("The shared workspace is not a safe directory.")

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "read_file":
            return self.read_file(arguments)
        if name == "write_file":
            return self.write_file(arguments)
        raise ValueError("Only read_file and write_file are available.")

    def read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._keys(arguments, {"path"})
        path = self._existing_file(arguments.get("path"))
        raw = path.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("The requested file exceeds the read limit.")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Only UTF-8 text files can be read.") from exc
        return {"path": path.relative_to(self.root).as_posix(), "content": content}

    def write_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._keys(arguments, {"path", "content"})
        content = arguments.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("The file content exceeds the write limit.")
        path = self._write_path(arguments.get("path"))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "path": path.relative_to(self.root).as_posix(),
            "size_bytes": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def _keys(arguments: dict[str, Any], expected: set[str]) -> None:
        if not isinstance(arguments, dict) or set(arguments) != expected:
            raise ValueError("Tool arguments do not match the declared schema.")

    def _relative(self, value: Any) -> Path:
        if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
            raise ValueError("The shared file path is invalid.")
        path = Path(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("The shared file path must be a normalized relative path.")
        if path.parts[0] == ".general-agent-state":
            raise ValueError("The requested path is reserved for managed Agent state.")
        return path

    def _existing_file(self, value: Any) -> Path:
        candidate = self.root / self._relative(value)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError("The requested shared file does not exist.") from exc
        if (
            not resolved.is_relative_to(self.root)
            or not resolved.is_file()
            or candidate.is_symlink()
        ):
            raise ValueError("The requested path is outside the shared folder.")
        return resolved

    def _write_path(self, value: Any) -> Path:
        relative = self._relative(value)
        candidate = self.root / relative
        ancestor = candidate.parent
        while not ancestor.exists() and ancestor != self.root:
            ancestor = ancestor.parent
        resolved_ancestor = ancestor.resolve(strict=True)
        if not resolved_ancestor.is_relative_to(self.root):
            raise ValueError("The requested path is outside the shared folder.")
        if candidate.exists() and (candidate.is_symlink() or not candidate.is_file()):
            raise ValueError("The requested output path is not a regular file.")
        return candidate


class AgentBusyError(RuntimeError):
    """Raised when a chat message arrives while the Agent is still running."""


class GeneralAgent:
    def __init__(self, task_card: dict[str, Any], shared_root: Path, state_path: Path) -> None:
        self.task_card = task_card
        self.tools = SharedFolderTools(shared_root)
        self.state_path = state_path
        self.lock = threading.RLock()
        self.base_url = os.environ["ARK_BASE_URL"].rstrip("/")
        self.api_key = os.environ["ARK_API_KEY"]
        self.model = os.environ["GENERAL_AGENT_MODEL"]
        self.timeout = int(os.environ.get("GENERAL_AGENT_TIMEOUT_SECONDS", "180"))
        self.max_rounds = int(os.environ.get("GENERAL_AGENT_MAX_TOOL_ROUNDS", "8"))
        self.running = False
        self.state = self._load_or_initialize()

    def _load_or_initialize(self) -> dict[str, Any]:
        instruction = self.task_card["objective"]
        details = self.task_card.get("instructions", [])
        if details:
            instruction += "\n\n" + "\n".join(f"- {item}" for item in details)
        system = (
            "You are a general-purpose task Agent. Complete the user's request autonomously. "
            "Your only tools are read_file and write_file, both scoped to the current task shared "
            "folder. Use an iterative tool loop when files must be inspected or produced. Never "
            "claim a file change unless the tool succeeded. Keep the final response concise."
        )
        if self.state_path.exists():
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if loaded.get("task_card_id") != self.task_card["card_id"]:
                raise RuntimeError("The persisted chat belongs to another task card.")
            return loaded
        initial = {
            "schema_version": "1.0",
            "task_card_id": self.task_card["card_id"],
            "messages": [self._message("user", instruction)],
            "model_messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": instruction},
            ],
            "usage": [],
        }
        self._persist(initial)
        return initial

    def public_messages(self) -> list[dict[str, str]]:
        with self.lock:
            return [dict(item) for item in self.state["messages"]]

    def is_running(self) -> bool:
        with self.lock:
            return self.running

    def chat(self, content: str) -> list[dict[str, str]]:
        if not isinstance(content, str) or not content.strip() or len(content) > 20_000:
            raise ValueError("Message content must contain 1 to 20000 characters.")
        with self.lock:
            if self.running:
                raise AgentBusyError(
                    "The Agent is still processing the previous message."
                )
            if len(self.state["messages"]) >= MAX_HISTORY_MESSAGES:
                raise ValueError("The chat history has reached its managed limit.")
            cleaned = content.strip()
            self.state["messages"].append(self._message("user", cleaned))
            self.state["model_messages"].append({"role": "user", "content": cleaned})
            self._persist(self.state)
            self.running = True
        self._start_background_run()
        return self.public_messages()

    def autostart(self) -> None:
        """Run the latest instruction if it never received a reply.

        Covers both first boot (the task-card instruction is the only message)
        and crash recovery (a user message was persisted but the process died
        before the assistant reply was appended).
        """
        with self.lock:
            if self.running:
                return
            messages = self.state["messages"]
            if not messages or messages[-1].get("role") != "user":
                return
            self.running = True
        self._start_background_run()

    def _start_background_run(self) -> None:
        threading.Thread(
            target=self._execute, name="general-agent-run", daemon=True
        ).start()

    def _execute(self) -> None:
        try:
            answer = self._run_loop()
        except Exception:
            with self.lock:
                self.state["messages"].append(
                    self._message(
                        "assistant", "模型暂时未能完成本轮任务; 请稍后重试。", status="error"
                    )
                )
                self._persist(self.state)
        else:
            with self.lock:
                self.state["messages"].append(self._message("assistant", answer))
                self.state["model_messages"].append({"role": "assistant", "content": answer})
                self._persist(self.state)
        finally:
            with self.lock:
                self.running = False

    def _run_loop(self) -> str:
        for round_index in range(1, self.max_rounds + 1):
            response = self._complete(round_index)
            self._record_usage(response, round_index)
            message = response["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("The model returned an empty response.")
                return content.strip()
            if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS_PER_ROUND:
                raise RuntimeError("The model returned too many tool calls.")
            self.state["model_messages"].append(
                {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
            )
            for call in calls:
                call_id = call.get("id")
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(call_id, str) or not isinstance(function, dict):
                    raise RuntimeError("The model returned an invalid tool call.")
                name = function.get("name")
                try:
                    arguments = json.loads(function.get("arguments", "{}"))
                    result = {"ok": True, "result": self.tools.execute(name, arguments)}
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    result = {"ok": False, "error": str(exc)}
                self.state["model_messages"].append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    }
                )
            self._persist(self.state)
        raise RuntimeError("The Agent exceeded its tool-round limit.")

    def _complete(self, round_index: int) -> dict[str, Any]:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": self.state["model_messages"],
                "tools": TOOLS,
                "tool_choice": "auto",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(payload) > MAX_MODEL_REQUEST_BYTES:
            raise RuntimeError("The model request exceeded the size limit.")
        request = Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": (
                    f"general-{self.task_card['instance_id']}-"
                    f"{len(self.state['messages'])}-{round_index}"
                ),
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise RuntimeError(f"The model provider returned HTTP {exc.code}.") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise RuntimeError("The model provider is unavailable.") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("The model response exceeded the size limit.")
        try:
            decoded = json.loads(raw)
            choices = decoded["choices"]
            if (
                not isinstance(choices, list)
                or len(choices) != 1
                or not isinstance(choices[0]["message"], dict)
            ):
                raise ValueError
            return decoded
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("The model provider returned an invalid response.") from exc

    def _record_usage(self, response: dict[str, Any], round_index: int) -> None:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return
        input_tokens = self._usage_int(usage.get("prompt_tokens"))
        output_tokens = self._usage_int(usage.get("completion_tokens"))
        total_tokens = self._usage_int(
            usage.get("total_tokens"), default=input_tokens + output_tokens
        )
        prompt_details = usage.get("prompt_tokens_details")
        completion_details = usage.get("completion_tokens_details")
        cached_tokens = self._usage_int(
            prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
        )
        reasoning_tokens = self._usage_int(
            completion_details.get("reasoning_tokens")
            if isinstance(completion_details, dict)
            else None
        )
        seed = (
            f"{self.task_card['instance_id']}:{len(self.state['messages'])}:"
            f"{round_index}:{len(self.state['usage'])}"
        )
        request_id = f"general_{hashlib.sha256(seed.encode()).hexdigest()[:24]}"
        provider_id = response.get("id")
        self.state["usage"].append(
            {
                "schema_version": "1.1",
                "event_id": f"usage_{hashlib.sha256((seed + ':usage').encode()).hexdigest()[:24]}",
                "task_id": self.task_card["task_id"],
                "instance_id": self.task_card["instance_id"],
                "agent_type": "general",
                "request_id": request_id,
                "provider_request_id": provider_id if isinstance(provider_id, str) else None,
                "provider": "ark",
                "model": self.model,
                "call_type": "reasoning_llm",
                "usage_basis": "tokens",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": min(cached_tokens, input_tokens),
                "reasoning_tokens": min(reasoning_tokens, output_tokens),
                "total_tokens": total_tokens,
                "billing_units": [],
                "raw_usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": total_tokens,
                },
                "occurred_at": utc_now(),
            }
        )

    @staticmethod
    def _usage_int(value: Any, *, default: int = 0) -> int:
        return value if type(value) is int and value >= 0 else default

    def _persist(self, state: dict[str, Any]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.state_path)

    @staticmethod
    def _message(role: str, content: str, *, status: str = "complete") -> dict[str, str]:
        return {
            "message_id": f"msg_{secrets.token_hex(12)}",
            "role": role,
            "content": content,
            "status": status,
            "created_at": utc_now(),
        }


class Handler(BaseHTTPRequestHandler):
    server: GeneralAgentServer

    def do_GET(self) -> None:
        if self.path in {"/healthz", "/readyz"}:
            self._json(HTTPStatus.OK, {"status": "ok"})
        elif self.path == "/api/messages":
            self._json(
                HTTPStatus.OK,
                {
                    "messages": self.server.agent.public_messages(),
                    "running": self.server.agent.is_running(),
                },
            )
        elif self.path == "/api/usage":
            with self.server.agent.lock:
                self._json(
                    HTTPStatus.OK,
                    {"events": list(self.server.agent.state.get("usage", []))},
                )
        elif self.path == "/":
            html = (Path(__file__).with_name("index.html")).read_bytes()
            self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/api/messages":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("The request body size is invalid.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("The request body must be an object.")
            messages = self.server.agent.chat(body.get("content"))
            self._json(
                HTTPStatus.ACCEPTED, {"messages": messages, "running": True}
            )
        except AgentBusyError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            self._json(
                HTTPStatus.BAD_GATEWAY, {"error": "模型暂时不可用; 请稍后重试。"}
            )

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False).encode(),
            "application/json; charset=utf-8",
        )

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; frame-ancestors *",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class GeneralAgentServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], agent: GeneralAgent) -> None:
        super().__init__(address, Handler)
        self.agent = agent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    task_card_path = Path(os.environ["GENERAL_AGENT_TASK_CARD"])
    shared_root = Path(os.environ["GENERAL_AGENT_SHARED_ROOT"])
    state_path = Path(os.environ["GENERAL_AGENT_STATE_PATH"])
    task_card = json.loads(task_card_path.read_text(encoding="utf-8"))
    agent = GeneralAgent(task_card, shared_root, state_path)
    server = GeneralAgentServer((args.host, args.port), agent)
    agent.autostart()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
