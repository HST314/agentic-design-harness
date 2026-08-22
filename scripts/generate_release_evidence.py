#!/usr/bin/env python3
"""Create one commit-bound release evidence artifact from the completed gate."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.run_g5_gate import STAGES
except ModuleNotFoundError:  # Direct execution adds scripts/, not the repository root.
    from run_g5_gate import STAGES

EXPECTED_GATE_STAGES = [(name, f"make {target}") for name, target in STAGES]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_gate_evidence(root: Path, commit: str) -> tuple[dict[str, Any], Path, Path]:
    result_path = root / "build" / "g5-gate-result.json"
    log_path = root / "build" / "g5-gate.log"
    if not result_path.is_file() or not log_path.is_file():
        raise SystemExit("run make g5-e2e to produce gate result and log evidence first")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    stages = result.get("stages")
    dependencies = result.get("dependencies")
    image = dependencies.get("image_agent") if isinstance(dependencies, dict) else None
    actual_stages = (
        [(item.get("name"), item.get("command")) for item in stages]
        if isinstance(stages, list) and all(isinstance(item, dict) for item in stages)
        else []
    )
    valid = (
        result.get("verification_command") == "make g5-e2e"
        and result.get("commit") == commit
        and result.get("status") == "PASSED"
        and actual_stages == EXPECTED_GATE_STAGES
        and all(item.get("exit_code") == 0 for item in stages or [])
        and isinstance(image, dict)
        and isinstance(image.get("commit"), str)
        and re.fullmatch(r"[0-9a-f]{40}", image["commit"]) is not None
        and image.get("worktree_clean") is True
        and isinstance(result.get("log"), dict)
        and result["log"].get("sha256") == sha256_file(log_path)
        and result["log"].get("size_bytes") == log_path.stat().st_size
    )
    if not valid:
        raise SystemExit("the G5 gate result is failed, incomplete or belongs to another commit")
    return result, result_path, log_path


def git_output(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    commit = git_output(root, "rev-parse", "HEAD")
    if git_output(root, "status", "--porcelain"):
        raise SystemExit("release evidence generation requires a clean worktree")
    gate, result_path, log_path = validate_gate_evidence(root, commit)
    artifacts = [result_path, log_path]
    for optional in (
        root / "build" / "capacity-benchmark.json",
        root / "build" / "documentation-check.json",
    ):
        if optional.is_file():
            artifacts.append(optional)
    output = {
        "schema_version": "release-evidence.v1",
        "commit": commit,
        "branch": git_output(root, "branch", "--show-current"),
        "generated_at": utc_now(),
        "verification_command": "make g5-e2e",
        "status": gate["status"],
        "stages": gate["stages"],
        "dependencies": gate["dependencies"],
        "artifacts": [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in artifacts
        ],
    }
    destination = root / "build" / "release-evidence.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Release evidence written to {destination.relative_to(root)}")


if __name__ == "__main__":
    main()
