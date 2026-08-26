#!/usr/bin/env python3
"""Run the non-paid P6 acceptance slice and emit redacted platform evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REAL_PROVIDER_INPUTS = (
    "HARNESS_REAL_PROVIDER_BASE_URL",
    "HARNESS_REAL_PROVIDER_API_KEY",
    "HARNESS_REAL_PROVIDER_TEXT_MODEL",
    "HARNESS_REAL_PROVIDER_IMAGE_MODEL",
    "HARNESS_REAL_PROVIDER_VLM_MODEL",
)
COST_CONFIRMATION_NAME = "HARNESS_P6_ARK_COST_CONFIRMATION"
COST_CONFIRMATION_VALUE = "I_CONFIRM_ONE_PAID_IMAGE"


def git_output(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def test_environment() -> dict[str, str]:
    environment = os.environ.copy()
    python_paths = [
        str(ROOT / "backend"),
        str(ROOT / ".test-deps"),
        str(ROOT / "tests"),
        str(ROOT),
    ]
    existing = environment.get("PYTHONPATH")
    if existing:
        python_paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    venv_python = (
        ROOT / ".venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else ROOT / ".venv" / "bin" / "python"
    )
    environment.update(
        {
            "HARNESS_IMAGE_AGENT_ROOT": str(ROOT / "agents" / "image_agent_mvp"),
            "HARNESS_IMAGE_AGENT_PYTHON": str(venv_python),
            "HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT": str(
                ROOT / ".runtime" / "image-agent-deps"
            ),
        }
    )
    return environment


def provider_gate(environment: dict[str, str]) -> dict[str, Any]:
    configured = {
        name: bool(environment.get(name, "").strip()) for name in REAL_PROVIDER_INPUTS
    }
    cost_confirmed = (
        environment.get(COST_CONFIRMATION_NAME) == COST_CONFIRMATION_VALUE
    )
    if not all(configured.values()):
        status = "BLOCKED_MISSING_CONFIGURATION"
        reason = "A complete Ark configuration was not present."
    elif not cost_confirmed:
        status = "BLOCKED_COST_NOT_CONFIRMED"
        reason = "The exact one-image cost confirmation was not present."
    else:
        status = "READY_FOR_SEPARATE_PAID_GATE"
        reason = "Configuration and cost confirmation are present; use the paid gate."
    return {
        "status": status,
        "configuration_fields_present": configured,
        "cost_confirmation_present": cost_confirmed,
        "real_request_performed": False,
        "reason": reason,
        "non_substitution_statement": (
            "The deterministic HTTP fixture validates process and contract integration only; "
            "it is not recorded as Ark acceptance."
        ),
    }


def run_check(
    name: str,
    command: list[str],
    *,
    environment: dict[str, str],
) -> dict[str, Any]:
    print(f"[p6] {name}", flush=True)
    started = time.monotonic()
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    duration_ms = round((time.monotonic() - started) * 1000)
    return {
        "name": name,
        "exit_code": result.returncode,
        "duration_ms": duration_ms,
        "status": "PASSED" if result.returncode == 0 else "FAILED",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the cross-platform P6 acceptance slice. This command never performs a paid "
            "Ark request; paid evidence is produced by the separately authorized provider gate."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "p6-platform-acceptance.json",
    )
    options = parser.parse_args()
    system = platform.system()
    if system not in {"Linux", "Windows"}:
        raise SystemExit(f"P6 supports Linux and Windows; found {system}.")
    environment = test_environment()
    checks = [
        (
            "launcher_doctor",
            [sys.executable, "scripts/dev.py", "doctor"],
        ),
        (
            "launcher_health_smoke",
            [
                sys.executable,
                "scripts/dev.py",
                "start",
                "--check",
                "--timeout",
                "60",
            ],
        ),
        (
            "root_configuration_and_fixed_image_path",
            [
                sys.executable,
                "-m",
                "unittest",
                (
                    "tests.unit.test_foundation.FoundationTests."
                    "test_image_agent_path_resolves_from_the_repository_root"
                ),
                (
                    "tests.unit.test_config_kernel.ConfigKernelTests."
                    "test_config_check_command_is_local_and_zero_cost"
                ),
                "-v",
            ],
        ),
        (
            "multi_branch_atomic_publication_and_crash_recovery",
            [
                sys.executable,
                "-m",
                "unittest",
                (
                    "tests.integration.test_application_service."
                    "HarnessApplicationServiceTests."
                    "test_bundle_delivery_waits_for_human_and_publishes_two_assets_atomically"
                ),
                (
                    "tests.integration.test_application_service."
                    "HarnessApplicationServiceTests."
                    "test_bundle_publication_recovers_after_manifest_write_without_half_visibility"
                ),
                "-v",
            ],
        ),
        (
            "retired_configuration_routes_and_launch_redaction",
            [
                sys.executable,
                "-m",
                "unittest",
                (
                    "tests.integration.test_g4_api.G4ApiTests."
                    "test_usage_and_budget_remain_available_while_legacy_config_routes_are_gone"
                ),
                (
                    "tests.integration.test_supervisor.ProcessSupervisorTests."
                    "test_logs_are_redacted_and_launch_spec_file_is_removed"
                ),
                "-v",
            ],
        ),
        (
            "real_image_agent_process_bundle_closure",
            [
                sys.executable,
                "-m",
                "unittest",
                (
                    "tests.e2e.test_g3_real_image_agent.RealImageAdapterG3Tests."
                    "test_real_http_approval_to_finalize_publish_and_complete"
                ),
                "-v",
            ],
        ),
    ]
    results: list[dict[str, Any]] = []
    for name, command in checks:
        result = run_check(name, command, environment=environment)
        results.append(result)
        if result["exit_code"] != 0:
            break
    ark = provider_gate(environment)
    passed = len(results) == len(checks) and all(
        item["status"] == "PASSED" for item in results
    )
    status = "FAILED"
    if passed:
        status = (
            "PASSED_WITH_ARK_BLOCKED"
            if ark["status"].startswith("BLOCKED_")
            else "PASSED_AWAITING_SEPARATE_ARK_GATE"
        )
    server_url = environment.get("GITHUB_SERVER_URL")
    repository = environment.get("GITHUB_REPOSITORY")
    run_id = environment.get("GITHUB_RUN_ID")
    run_url = (
        f"{server_url}/{repository}/actions/runs/{run_id}"
        if server_url and repository and run_id
        else None
    )
    evidence = {
        "schema_version": "p6-platform-acceptance.v1",
        "status": status,
        "platform": {
            "system": system,
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "runner_os": environment.get("RUNNER_OS"),
        },
        "source": {
            "harness_commit": git_output("rev-parse", "HEAD"),
            "image_agent_commit": git_output(
                "-C", "agents/image_agent_mvp", "rev-parse", "HEAD"
            ),
            "image_agent_lock_sha256": sha256_file(
                ROOT / "agents" / "image-agent.lock.json"
            ),
            "worktree_clean": not bool(git_output("status", "--porcelain")),
        },
        "configuration_architecture": {
            "sources": [
                ".env",
                "config/provider.yaml",
                "config/model_list.yaml",
                "config/runtime.yaml",
                "config/image_agent_runtime.yaml",
            ],
            "image_agent_path": "agents/image_agent_mvp",
            "delivery_format": "bundle_only",
            "legacy_configuration_read": False,
            "runtime_configuration_ui": True,
        },
        "checks": results,
        "ark": ark,
        "ci_run_url": run_url,
    }
    destination = options.output
    if not destination.is_absolute():
        destination = ROOT / destination
    write_json(destination, evidence)
    print(f"P6 evidence written to {destination.relative_to(ROOT)}", flush=True)
    if not passed:
        raise SystemExit("P6 platform acceptance failed.")


if __name__ == "__main__":
    main()
