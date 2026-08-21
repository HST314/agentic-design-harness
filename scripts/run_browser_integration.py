"""Run the production web bundle against a real Harness and Image subprocess."""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO

ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/final.png":
            self._send(PNG, "image/png")
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/chat/completions"):
            content = body["messages"][-1]["content"]
            if isinstance(content, list):
                payload = {
                    "passed": True,
                    "decision": "pass",
                    "deviations": [],
                    "rework_prompt_delta": "",
                    "overall_score": 96,
                    "dimension_scores": {"task_fit": 96, "visual_quality": 96},
                    "confidence": 0.99,
                }
            elif "TaskConfirmationDoc" in content:
                payload = {
                    "summary": "已形成可执行任务书。",
                    "confirmed_facts": [],
                    "default_handling_for_unknowns": [],
                    "forbidden_items": [],
                    "human_annotations": [],
                    "markdown_body": "# 创作任务书\n\n生成一张可追溯的最终视觉稿。\n",
                }
            else:
                payload = {"questions": []}
            self._send_json({
                "id": f"chatcmpl_browser_{time.time_ns()}",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model", "browser-provider"),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
        elif self.path.endswith("/images/generations"):
            host, port = self.server.server_address  # type: ignore[attr-defined]
            self._send_json({
                "id": f"image_browser_{time.time_ns()}",
                "created": 1,
                "data": [{"url": f"http://{host}:{port}/final.png"}],
            })
        else:
            self.send_error(404)

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(self, payload: dict[str, Any]) -> None:
        self._send(json.dumps(payload, ensure_ascii=False).encode(), "application/json")

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def provider() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for(url: str, process: subprocess.Popen[bytes], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"process exited before {url}: {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)
        return
    process.kill()
    process.wait(timeout=5)


def log_tail(stream: BinaryIO, label: str) -> None:
    stream.flush()
    stream.seek(0)
    lines = stream.read().decode("utf-8", errors="replace").splitlines()
    if lines:
        print(f"[{label} log tail]", file=sys.stderr)
        print("\n".join(lines[-80:]), file=sys.stderr)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--real-provider",
        action="store_true",
        help="use explicitly supplied real Provider credentials instead of the local emulator",
    )
    return parser.parse_args()


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for the controlled real-Provider smoke gate")
    return value


def main() -> int:
    options = arguments()
    image_root = Path(os.environ.get("HARNESS_IMAGE_AGENT_ROOT", ROOT.parent / "image_agent_mvp"))
    image_deps = Path(
        os.environ.get("HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT", ROOT / ".runtime/image-agent-deps")
    )
    image_python = os.environ.get("HARNESS_IMAGE_AGENT_PYTHON", sys.executable)
    if not image_root.is_dir() or not image_deps.is_dir():
        raise RuntimeError(
            "Image Agent source and dependency roots must exist before this gate runs"
        )

    backend_port, frontend_port = free_port(), free_port()
    while frontend_port == backend_port:
        frontend_port = free_port()
    backend_url = f"http://127.0.0.1:{backend_port}"
    frontend_url = f"http://127.0.0.1:{frontend_port}"

    if options.real_provider:
        provider_url = required_environment("HARNESS_REAL_PROVIDER_BASE_URL")
        provider_key = required_environment("HARNESS_REAL_PROVIDER_API_KEY")
        text_model = required_environment("HARNESS_REAL_PROVIDER_TEXT_MODEL")
        image_model = required_environment("HARNESS_REAL_PROVIDER_IMAGE_MODEL")
        vlm_model = required_environment("HARNESS_REAL_PROVIDER_VLM_MODEL")
        provider_scope = nullcontext(provider_url)
    else:
        provider_key = ""
        text_model = "browser-text"
        image_model = "browser-image"
        vlm_model = "browser-vlm"
        provider_scope = provider()

    with (
        tempfile.TemporaryDirectory(prefix="harness-browser-") as temporary,
        provider_scope as provider_url,
    ):
        runtime = Path(temporary)
        common_env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(ROOT / "backend"), str(ROOT / ".test-deps"))),
            "HARNESS_CONTROL_ROOT": str(runtime / "control-data"),
            "HARNESS_WORKSPACE_ROOT": str(runtime / "workspace"),
            "HARNESS_HOST": "127.0.0.1",
            "HARNESS_PORT": str(backend_port),
            "HARNESS_LOG_LEVEL": "WARNING",
            "HARNESS_IMAGE_AGENT_ROOT": str(image_root.resolve()),
            "HARNESS_IMAGE_AGENT_PYTHON": image_python,
            "HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT": str(image_deps.resolve()),
            "HARNESS_BACKEND_URL": backend_url,
        }
        with tempfile.TemporaryFile() as backend_log, tempfile.TemporaryFile() as frontend_log:
            backend = subprocess.Popen(
                [sys.executable, "-m", "harness"],
                cwd=ROOT,
                env=common_env,
                stdout=backend_log,
                stderr=subprocess.STDOUT,
            )
            frontend: subprocess.Popen[bytes] | None = None
            try:
                wait_for(f"{backend_url}/readyz", backend)
                subprocess.run(
                    ["npm", "--prefix", "frontend", "run", "build"],
                    cwd=ROOT,
                    env=common_env,
                    check=True,
                )
                frontend = subprocess.Popen(
                    [
                        "npm", "--prefix", "frontend", "run", "preview", "--",
                        "--host", "127.0.0.1", "--port", str(frontend_port),
                    ],
                    cwd=ROOT,
                    env=common_env,
                    stdout=frontend_log,
                    stderr=subprocess.STDOUT,
                )
                wait_for(frontend_url, frontend)
                browser_env = {
                    **common_env,
                    "PLAYWRIGHT_BASE_URL": frontend_url,
                    "HARNESS_BROWSER_PROVIDER_URL": provider_url,
                    "HARNESS_BROWSER_TEXT_MODEL": text_model,
                    "HARNESS_BROWSER_IMAGE_MODEL": image_model,
                    "HARNESS_BROWSER_VLM_MODEL": vlm_model,
                }
                if options.real_provider:
                    browser_env["HARNESS_BROWSER_REAL_PROVIDER"] = "1"
                    browser_env["HARNESS_BROWSER_PROVIDER_API_KEY"] = provider_key
                subprocess.run(
                    [
                        "npm", "exec", "--", "playwright", "test",
                        "--config", "playwright.integration.config.ts",
                        "e2e/real-stack.spec.ts",
                    ],
                    cwd=ROOT / "frontend",
                    env=browser_env,
                    check=True,
                )
            except Exception:
                log_tail(backend_log, "backend")
                log_tail(frontend_log, "frontend")
                raise
            finally:
                if frontend is not None:
                    stop(frontend)
                stop(backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
