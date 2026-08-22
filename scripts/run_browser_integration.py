"""Run the production web bundle against a real Harness and Image subprocess."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
REAL_PROVIDER_ENVIRONMENT = {
    "HARNESS_REAL_PROVIDER_BASE_URL": ("MODEL_BASE_URL",),
    "HARNESS_REAL_PROVIDER_API_KEY": ("MODEL_API_KEY",),
    "HARNESS_REAL_PROVIDER_TEXT_MODEL": ("MODEL_TEXT_MODEL", "TEXT_MODEL"),
    "HARNESS_REAL_PROVIDER_IMAGE_MODEL": ("MODEL_IMAGE_MODEL", "IMAGE_MODEL"),
    "HARNESS_REAL_PROVIDER_VLM_MODEL": ("MODEL_VLM_MODEL", "VLM_MODEL"),
}
REAL_PROVIDER_INPUT_NAMES = frozenset(
    name
    for canonical, aliases in REAL_PROVIDER_ENVIRONMENT.items()
    for name in (canonical, *aliases)
)


@dataclass(frozen=True)
class RealProviderConfiguration:
    base_url: str
    api_key: str
    text_model: str
    image_model: str
    vlm_model: str


@dataclass(frozen=True)
class BrowserRuntime:
    playwright_version: str
    chromium_revision: str
    browser_version: str
    executable: Path


@dataclass(frozen=True)
class SourceBaseline:
    harness_commit: str
    image_agent_commit: str
    harness_worktree_clean: bool
    image_agent_worktree_clean: bool


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
                if not isinstance(self.server, ProviderServer):
                    raise RuntimeError("deterministic Provider server type changed")
                clarification_number = self.server.next_clarification_number()
                payload = {
                    "questions": [
                        {
                            "field": "browser_acceptance_tone",
                            "question": "本次验收稿采用哪种视觉语气?",
                            "options": [
                                {
                                    "option_id": "A",
                                    "label": "清晰克制",
                                    "description": "使用清晰克制的企业级视觉语气。",
                                    "requires_free_text": False,
                                },
                                {
                                    "option_id": "B",
                                    "label": "鲜明活力",
                                    "description": "使用鲜明活力的视觉语气。",
                                    "requires_free_text": False,
                                },
                            ],
                            "recommended_option_id": "A",
                            "impact": "影响最终画面的视觉表达。",
                            "evidence": "验收任务书未指定视觉语气。",
                            "missing": True,
                            "has_safe_default": False,
                            "blocking": True,
                            "semantic_fingerprint": "browser-acceptance-tone-v1",
                        }
                    ] if clarification_number == 0 else [],
                }
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


class ProviderServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), ProviderHandler)
        self._clarification_number = 0
        self._clarification_lock = threading.Lock()

    def next_clarification_number(self) -> int:
        with self._clarification_lock:
            number = self._clarification_number
            self._clarification_number += 1
            return number


@contextmanager
def provider() -> Iterator[str]:
    server = ProviderServer()
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


def log_tail(stream: BinaryIO, label: str, persistent_log: BinaryIO | None = None) -> None:
    stream.flush()
    stream.seek(0)
    lines = stream.read().decode("utf-8", errors="replace").splitlines()
    if lines:
        message = f"[{label} log tail]\n" + "\n".join(lines[-80:]) + "\n"
        print(message, file=sys.stderr, end="")
        if persistent_log is not None:
            persistent_log.write(message.encode("utf-8"))
            persistent_log.flush()


def _dotenv_value(raw: str, line_number: int) -> str:
    def valid_suffix(suffix: str) -> bool:
        stripped = suffix.strip()
        return not stripped or stripped.startswith("#")

    value = raw.strip()
    if not value:
        return ""
    if value.startswith("\""):
        try:
            parsed, end = json.JSONDecoder().raw_decode(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"invalid double-quoted value on env-file line {line_number}"
            ) from exc
        if not isinstance(parsed, str) or not valid_suffix(value[end:]):
            raise RuntimeError(f"invalid suffix on env-file line {line_number}")
        return parsed
    if value.startswith("'"):
        closing = value.find("'", 1)
        if closing < 0 or not valid_suffix(value[closing + 1 :]):
            raise RuntimeError(f"invalid single-quoted value on env-file line {line_number}")
        return value[1:closing]
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def parse_environment_file(path: Path) -> dict[str, str]:
    """Parse shell-free dotenv assignments with universal-newline CRLF handling."""
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RuntimeError(f"cannot read env file: {path}") from exc
    parsed: dict[str, str] = {}
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeError(f"missing '=' on env-file line {line_number}")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not ENVIRONMENT_NAME.fullmatch(name):
            raise RuntimeError(f"invalid variable name on env-file line {line_number}")
        parsed[name] = _dotenv_value(raw_value, line_number)
    return parsed


def load_real_provider_environment(path: Path, environment: MutableMapping[str, str]) -> None:
    """Load only the gate's allowlisted values; an existing canonical value wins."""
    parsed = parse_environment_file(path)
    for canonical, aliases in REAL_PROVIDER_ENVIRONMENT.items():
        if canonical in environment:
            continue
        for candidate in (canonical, *aliases):
            if candidate in parsed:
                environment[canonical] = parsed[candidate]
                break


def child_process_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Remove real-Provider inputs before starting processes that do not need them."""
    return {
        name: value
        for name, value in environment.items()
        if name not in REAL_PROVIDER_INPUT_NAMES
    }


def _validated_value(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or value == "":
        raise RuntimeError(f"{name} is required for the controlled real-Provider smoke gate")
    if value != value.strip():
        raise RuntimeError(f"{name} contains leading or trailing whitespace")
    if CONTROL_CHARACTER.search(value):
        raise RuntimeError(f"{name} contains an ASCII control character")
    return value


def validate_real_provider_environment(
    environment: Mapping[str, str],
) -> RealProviderConfiguration:
    base_url = _validated_value(environment, "HARNESS_REAL_PROVIDER_BASE_URL")
    api_key = _validated_value(environment, "HARNESS_REAL_PROVIDER_API_KEY")
    text_model = _validated_value(environment, "HARNESS_REAL_PROVIDER_TEXT_MODEL")
    image_model = _validated_value(environment, "HARNESS_REAL_PROVIDER_IMAGE_MODEL")
    vlm_model = _validated_value(environment, "HARNESS_REAL_PROVIDER_VLM_MODEL")
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise RuntimeError(
            "HARNESS_REAL_PROVIDER_BASE_URL must be an HTTPS origin/path without credentials, "
            "query, or fragment"
        )
    for name, model in (
        ("HARNESS_REAL_PROVIDER_TEXT_MODEL", text_model),
        ("HARNESS_REAL_PROVIDER_IMAGE_MODEL", image_model),
        ("HARNESS_REAL_PROVIDER_VLM_MODEL", vlm_model),
    ):
        if len(model) > 200 or re.search(r"\s", model):
            raise RuntimeError(f"{name} must be a non-whitespace model identifier")
    return RealProviderConfiguration(base_url, api_key, text_model, image_model, vlm_model)


def browser_executable_candidates(browser_root: Path, revision: str) -> list[Path]:
    return [
        browser_root
        / f"chromium_headless_shell-{revision}"
        / "chrome-headless-shell-linux64"
        / "chrome-headless-shell",
        browser_root / f"chromium-{revision}" / "chrome-linux64" / "chrome",
        browser_root / f"chromium-{revision}" / "chrome-linux" / "chrome",
        browser_root
        / f"chromium_headless_shell-{revision}"
        / "chrome-headless-shell-mac-arm64"
        / "chrome-headless-shell",
        browser_root
        / f"chromium_headless_shell-{revision}"
        / "chrome-headless-shell-mac-x64"
        / "chrome-headless-shell",
        browser_root
        / f"chromium_headless_shell-{revision}"
        / "chrome-headless-shell-win64"
        / "chrome-headless-shell.exe",
    ]


def _playwright_installation() -> tuple[str, str, str]:
    frontend = ROOT / "frontend"
    package_path = frontend / "package.json"
    installed_path = frontend / "node_modules" / "@playwright" / "test" / "package.json"
    browsers_path = frontend / "node_modules" / "playwright-core" / "browsers.json"
    if not installed_path.is_file() or not browsers_path.is_file():
        raise RuntimeError(
            "Playwright is not installed; run: npm --prefix frontend ci"
        )
    expected = json.loads(package_path.read_text(encoding="utf-8"))["devDependencies"][
        "@playwright/test"
    ]
    installed = json.loads(installed_path.read_text(encoding="utf-8"))["version"]
    if installed != expected:
        raise RuntimeError(
            f"Playwright package mismatch (expected {expected}, found {installed}); "
            "run: npm --prefix frontend ci"
        )
    browsers = json.loads(browsers_path.read_text(encoding="utf-8"))["browsers"]
    chromium = next((item for item in browsers if item.get("name") == "chromium"), None)
    if chromium is None or not chromium.get("revision") or not chromium.get("browserVersion"):
        raise RuntimeError("Playwright Chromium revision metadata is unavailable")
    return installed, str(chromium["revision"]), str(chromium["browserVersion"])


def _node_browser_executable(environment: Mapping[str, str]) -> Path | None:
    node_environment = {
        name: environment[name]
        for name in ("HOME", "PATH", "PLAYWRIGHT_BROWSERS_PATH")
        if name in environment
    }
    try:
        completed = subprocess.run(
            [
                "node",
                "-e",
                "const { chromium } = require('playwright'); "
                "process.stdout.write(chromium.executablePath());",
            ],
            cwd=ROOT / "frontend",
            env=node_environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
    except OSError:
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return Path(completed.stdout.strip())


def _browser_version(executable: Path, expected_version: str) -> str:
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("browser executable is missing or not executable")
    safe_environment = {
        name: os.environ[name]
        for name in ("HOME", "PATH", "LD_LIBRARY_PATH")
        if name in os.environ
    }
    completed = subprocess.run(
        [str(executable), "--version"],
        env=safe_environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=15,
    )
    version = completed.stdout.strip()
    if completed.returncode != 0 or expected_version not in version:
        raise RuntimeError(
            f"browser version does not match Playwright Chromium {expected_version}"
        )
    if sys.platform.startswith("linux"):
        with executable.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                raise RuntimeError("Linux browser executable is not an ELF binary")
        ldd = shutil.which("ldd")
        if ldd is None:
            raise RuntimeError("ldd is required to preflight Chromium shared libraries")
        libraries = subprocess.run(
            [ldd, str(executable)],
            env=safe_environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
        missing = [line.strip() for line in libraries.stdout.splitlines() if "not found" in line]
        if libraries.returncode != 0 or missing:
            detail = ", ".join(missing) if missing else "ldd inspection failed"
            raise RuntimeError(f"Chromium shared-library preflight failed: {detail}")
    return version


def _playwright_launch_check(executable: Path, environment: Mapping[str, str]) -> None:
    launch_environment = {
        name: environment[name]
        for name in ("HOME", "PATH", "LD_LIBRARY_PATH")
        if name in environment
    }
    completed = subprocess.run(
        [
            "node",
            "-e",
            "const { chromium } = require('playwright'); "
            "chromium.launch({ executablePath: process.argv[1], headless: true })"
            ".then(async browser => { await browser.close(); })"
            ".catch(error => { console.error(error.message); process.exit(1); });",
            str(executable),
        ],
        cwd=ROOT / "frontend",
        env=launch_environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stdout.strip().splitlines()
        summary = next((line for line in detail if "][err]" in line), None)
        if summary is None:
            summary = detail[-1] if detail else "browser launch failed"
        raise RuntimeError(f"Playwright could not launch the browser: {summary}")


def preflight_browser(environment: Mapping[str, str]) -> BrowserRuntime:
    playwright_version, revision, expected_browser_version = _playwright_installation()
    explicit = environment.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    node_executable = _node_browser_executable(environment)
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    else:
        roots: list[Path] = []
        browsers_path = environment.get("PLAYWRIGHT_BROWSERS_PATH")
        if browsers_path and browsers_path != "0":
            roots.append(Path(browsers_path).expanduser())
        if node_executable is not None:
            candidates.append(node_executable)
            if len(node_executable.parents) >= 3:
                roots.append(node_executable.parents[2])
        for root in roots:
            candidates.extend(browser_executable_candidates(root, revision))
    unique_candidates = list(dict.fromkeys(path.resolve() for path in candidates))
    last_error: RuntimeError | None = None
    for executable in unique_candidates:
        try:
            version = _browser_version(executable, expected_browser_version)
            _playwright_launch_check(executable, environment)
            return BrowserRuntime(playwright_version, revision, version, executable)
        except RuntimeError as exc:
            last_error = exc
            if explicit:
                raise RuntimeError(
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH failed preflight: " + str(exc)
                ) from exc
    reason = f": {last_error}" if last_error else ""
    raise RuntimeError(
        f"Playwright Chromium revision {revision} is unavailable{reason}; run: "
        "npm --prefix frontend exec -- playwright install chromium"
    )


def git_commit(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
    )
    commit = completed.stdout.strip()
    if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", commit):
        return commit
    return "UNKNOWN"


def git_is_clean(path: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
    )
    return completed.returncode == 0 and completed.stdout == ""


def capture_source_baseline(harness_root: Path, image_agent_root: Path) -> SourceBaseline:
    return SourceBaseline(
        harness_commit=git_commit(harness_root),
        image_agent_commit=git_commit(image_agent_root),
        harness_worktree_clean=git_is_clean(harness_root),
        image_agent_worktree_clean=git_is_clean(image_agent_root),
    )


@contextmanager
def open_private_persistent_log(path: Path) -> Iterator[BinaryIO]:
    """Atomically replace path with a private file and keep its descriptor open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path: Path | None = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            temporary_path.chmod(0o600)
        if os.name == "nt":
            original_descriptor = descriptor
            descriptor = -1
            descriptor = _reopen_windows_file_with_delete_sharing(
                temporary_path,
                original_descriptor,
            )
        os.replace(temporary_path, path)
        temporary_path = None
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                temporary_path.unlink()


def _reopen_windows_file_with_delete_sharing(path: Path, descriptor: int) -> int:
    """Reopen a temporary file so Windows permits atomic replacement while open."""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    os.close(descriptor)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x40000000,  # GENERIC_WRITE
        0x00000001 | 0x00000002 | 0x00000004,  # read, write, and delete sharing
        None,
        3,  # OPEN_EXISTING
        0x00000080 | 0x00200000,  # normal file, do not follow reparse points
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), f"cannot reopen private log: {path}")
    try:
        reopened = msvcrt.open_osfhandle(
            handle,
            os.O_WRONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    if not stat.S_ISREG(os.fstat(reopened).st_mode):
        os.close(reopened)
        raise OSError(f"private log temporary path is not a regular file: {path}")
    return reopened


def run_logged(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    persistent_log: BinaryIO | None,
) -> None:
    if persistent_log is None:
        subprocess.run(command, cwd=cwd, env=dict(environment), check=True)
        return
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdout is None:
        raise RuntimeError("gate command output stream is unavailable")
    for chunk in iter(lambda: process.stdout.read(64 * 1024), b""):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        persistent_log.write(chunk)
        persistent_log.flush()
    return_code = process.wait()
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)


def assert_redacted_file(path: Path, forbidden_values: Sequence[str]) -> None:
    content = path.read_bytes()
    if any(value and value.encode("utf-8") in content for value in forbidden_values):
        with open_private_persistent_log(path) as stream:
            stream.write(b"Sensitive value detected; persisted output suppressed.\n")
        raise RuntimeError(f"persisted output failed exact-value redaction scan: {path.name}")


def publish_evidence(
    staging: Path,
    destination: Path,
    forbidden_values: Sequence[str],
    *,
    expected_source_baseline: SourceBaseline | None = None,
    harness_root: Path | None = None,
    image_agent_root: Path | None = None,
) -> None:
    assert_redacted_file(staging, forbidden_values)
    payload = json.loads(staging.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != "real-provider-browser-evidence.v2"
        or payload.get("execution", {}).get("result") != "PASSED"
    ):
        raise RuntimeError("browser evidence did not record a passed v2 gate")
    provider_mode = payload.get("execution", {}).get("provider_mode")
    if provider_mode == "real_external" and expected_source_baseline is None:
        raise RuntimeError("real-provider evidence requires a verified source baseline")
    if expected_source_baseline is not None:
        if provider_mode != "real_external":
            raise RuntimeError("real-provider evidence did not record the real Provider mode")
        if harness_root is None or image_agent_root is None:
            raise RuntimeError("real-provider evidence requires a verified source baseline")
        evidence_baseline = payload.get("baseline", {})
        if (
            evidence_baseline.get("harness_commit")
            != expected_source_baseline.harness_commit
            or evidence_baseline.get("image_agent_commit")
            != expected_source_baseline.image_agent_commit
            or evidence_baseline.get("harness_worktree_clean") is not True
            or evidence_baseline.get("image_agent_worktree_clean") is not True
        ):
            raise RuntimeError("real-provider evidence does not match its source baseline")
        current_source_baseline = capture_source_baseline(harness_root, image_agent_root)
        if (
            current_source_baseline != expected_source_baseline
            or not current_source_baseline.harness_worktree_clean
            or not current_source_baseline.image_agent_worktree_clean
        ):
            raise RuntimeError(
                "source repositories changed during the browser gate; refusing to publish evidence"
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, destination)
    destination.chmod(0o600)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--real-provider",
        action="store_true",
        help="use explicitly supplied real Provider credentials instead of the local emulator",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="load allowlisted real-Provider settings without shell sourcing",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="validate Provider settings and the Playwright browser runtime, then exit",
    )
    parser.add_argument(
        "--evidence-path",
        type=Path,
        help="persist an atomic, redacted JSON success record",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="tee build and Playwright output without changing their exit status",
    )
    return parser.parse_args()


def main() -> int:
    options = arguments()
    environment = dict(os.environ)
    if options.env_file is not None:
        load_real_provider_environment(options.env_file, environment)
    real_provider = (
        validate_real_provider_environment(environment) if options.real_provider else None
    )
    browser_runtime = preflight_browser(environment)
    print(
        "Browser preflight passed: "
        f"Playwright {browser_runtime.playwright_version}, "
        f"Chromium revision {browser_runtime.chromium_revision}, "
        f"{browser_runtime.browser_version}"
    )
    if options.preflight_only:
        return 0

    image_root = Path(
        environment.get("HARNESS_IMAGE_AGENT_ROOT", ROOT.parent / "image_agent_mvp")
    )
    image_deps = Path(
        environment.get(
            "HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT", ROOT / ".runtime/image-agent-deps"
        )
    )
    image_python = environment.get("HARNESS_IMAGE_AGENT_PYTHON", sys.executable)
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
        if real_provider is None:
            raise RuntimeError("real-Provider configuration was not validated")
        provider_url = real_provider.base_url
        provider_key = real_provider.api_key
        text_model = real_provider.text_model
        image_model = real_provider.image_model
        vlm_model = real_provider.vlm_model
        provider_scope = nullcontext(provider_url)
    else:
        provider_key = ""
        text_model = "browser-text"
        image_model = "browser-image"
        vlm_model = "browser-vlm"
        provider_scope = provider()

    source_baseline = capture_source_baseline(ROOT, image_root)
    if options.real_provider and options.evidence_path is not None and (
        source_baseline.harness_commit == "UNKNOWN"
        or source_baseline.image_agent_commit == "UNKNOWN"
        or not source_baseline.harness_worktree_clean
        or not source_baseline.image_agent_worktree_clean
    ):
        raise RuntimeError(
            "real-provider evidence requires clean, committed Harness and Image Agent worktrees"
        )

    evidence_destination = options.evidence_path.resolve() if options.evidence_path else None
    evidence_staging: Path | None = None
    if evidence_destination is not None:
        if evidence_destination.suffix != ".json":
            raise RuntimeError("--evidence-path must end in .json")
        evidence_destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_evidence = tempfile.mkstemp(
            prefix=".real-provider-evidence-",
            suffix=".json",
            dir=evidence_destination.parent,
        )
        os.close(descriptor)
        evidence_staging = Path(temporary_evidence)

    log_path = Path(os.path.abspath(options.log_file)) if options.log_file else None
    log_context = (
        open_private_persistent_log(log_path) if log_path is not None else nullcontext(None)
    )
    forbidden_values = (provider_key, provider_url) if options.real_provider else ()

    try:
        with (
            tempfile.TemporaryDirectory(prefix="harness-browser-") as temporary,
            provider_scope as provider_url,
            log_context as persistent_log,
        ):
            runtime = Path(temporary)
            child_environment = child_process_environment(environment)
            common_env = {
                **child_environment,
                "PYTHONPATH": os.pathsep.join(
                    (str(ROOT / "backend"), str(ROOT / ".test-deps"))
                ),
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
            with (
                tempfile.TemporaryFile() as backend_log,
                tempfile.TemporaryFile() as frontend_log,
            ):
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
                    run_logged(
                        ["npm", "--prefix", "frontend", "run", "build"],
                        cwd=ROOT,
                        environment=common_env,
                        persistent_log=persistent_log,
                    )
                    frontend = subprocess.Popen(
                        [
                            "npm",
                            "--prefix",
                            "frontend",
                            "run",
                            "preview",
                            "--",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(frontend_port),
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
                        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": str(
                            browser_runtime.executable
                        ),
                        "HARNESS_BROWSER_PROVIDER_URL": provider_url,
                        "HARNESS_BROWSER_TEXT_MODEL": text_model,
                        "HARNESS_BROWSER_IMAGE_MODEL": image_model,
                        "HARNESS_BROWSER_VLM_MODEL": vlm_model,
                        "HARNESS_BROWSER_PLAYWRIGHT_VERSION": (
                            browser_runtime.playwright_version
                        ),
                        "HARNESS_BROWSER_CHROMIUM_REVISION": (
                            browser_runtime.chromium_revision
                        ),
                        "HARNESS_BROWSER_VERSION": browser_runtime.browser_version,
                        "HARNESS_BROWSER_HARNESS_COMMIT": source_baseline.harness_commit,
                        "HARNESS_BROWSER_IMAGE_AGENT_COMMIT": (
                            source_baseline.image_agent_commit
                        ),
                        "HARNESS_BROWSER_HARNESS_CLEAN": (
                            "1" if source_baseline.harness_worktree_clean else "0"
                        ),
                        "HARNESS_BROWSER_IMAGE_AGENT_CLEAN": (
                            "1" if source_baseline.image_agent_worktree_clean else "0"
                        ),
                    }
                    if evidence_staging is not None:
                        browser_env["HARNESS_BROWSER_EVIDENCE_PATH"] = str(evidence_staging)
                    if options.real_provider:
                        browser_env["HARNESS_BROWSER_REAL_PROVIDER"] = "1"
                        browser_env["HARNESS_BROWSER_PROVIDER_API_KEY"] = provider_key
                    run_logged(
                        [
                            "npm",
                            "exec",
                            "--",
                            "playwright",
                            "test",
                            "--config",
                            "playwright.integration.config.ts",
                            "e2e/real-stack.spec.ts",
                        ],
                        cwd=ROOT / "frontend",
                        environment=browser_env,
                        persistent_log=persistent_log,
                    )
                except Exception:
                    log_tail(backend_log, "backend", persistent_log)
                    log_tail(frontend_log, "frontend", persistent_log)
                    raise
                finally:
                    if frontend is not None:
                        stop(frontend)
                    stop(backend)
        if evidence_staging is not None and evidence_destination is not None:
            publish_evidence(
                evidence_staging,
                evidence_destination,
                forbidden_values,
                expected_source_baseline=source_baseline if options.real_provider else None,
                harness_root=ROOT,
                image_agent_root=image_root,
            )
            evidence_staging = None
            try:
                evidence_label = evidence_destination.relative_to(ROOT)
            except ValueError:
                evidence_label = evidence_destination
            print(f"Redacted evidence written to {evidence_label}")
    finally:
        if evidence_staging is not None:
            with suppress(FileNotFoundError):
                evidence_staging.unlink()
        if log_path is not None and log_path.is_file():
            assert_redacted_file(log_path, forbidden_values)
            log_path.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
