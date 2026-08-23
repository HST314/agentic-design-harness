"""Deterministic localhost Agent process used by supervisor integration tests."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in {"/healthz", "/readyz"}:
            status = 200 if os.environ.get("FAKE_HEALTHY", "1") == "1" else 503
            self._json(status, {"status": "ok" if status == 200 else "unhealthy"})
            return
        if self.path == "/identity":
            self._json(
                200,
                {
                    "pid": os.getpid(),
                    "cwd": os.getcwd(),
                    "task_id": os.environ.get("HARNESS_TASK_ID"),
                    "instance_id": os.environ.get("HARNESS_INSTANCE_ID"),
                    "port": int(os.environ["HARNESS_INSTANCE_PORT"]),
                    "projects_root": os.environ.get("IMAGE_AGENT_PROJECTS_ROOT"),
                    "unrelated_secret": os.environ.get("UNRELATED_SECRET", "missing"),
                },
            )
            return
        self._json(404, {"status": "not_found"})

    def log_message(self, _: str, *args: object) -> None:
        return

    def _json(self, status: int, value: dict[str, object]) -> None:
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    if os.environ.get("FAKE_LONG_LOG") == "1":
        secret = os.environ.get("ARK_API_KEY", os.environ.get("FAKE_API_KEY", "")).encode()
        os.write(1, b"x" * (1024 * 1024 - 5) + secret + b"\n")
    print(
        f"credential={os.environ.get('ARK_API_KEY', os.environ.get('FAKE_API_KEY'))}",
        flush=True,
    )
    print(
        f"endpoint={os.environ.get('ARK_BASE_URL', os.environ.get('FAKE_BASE_URL'))}",
        flush=True,
    )
    port = int(os.environ["HARNESS_INSTANCE_PORT"])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever(poll_interval=0.05)


if __name__ == "__main__":
    main()
