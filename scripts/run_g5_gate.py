"""Run every G5 gate and persist commit-bound exit and log evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STAGES = (
    ("verify", "verify"),
    ("g3-real-closure", "g3-e2e"),
    ("g4-multi-instance", "g4-e2e"),
    ("frontend-browser", "frontend-e2e"),
    ("workbench-real-stack", "frontend-integration"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def git_output(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def require_clean_commit(root: Path) -> tuple[str, str]:
    dirty = git_output(root, "status", "--porcelain")
    if dirty:
        raise SystemExit(
            "G5 evidence requires a clean worktree; commit the candidate first."
        )
    return git_output(root, "rev-parse", "HEAD"), git_output(
        root, "branch", "--show-current"
    )


def require_clean_image_baseline(root: Path) -> str:
    if not root.is_dir():
        raise SystemExit(f"G5 Image Agent root does not exist: {root}")
    dirty = git_output(root, "status", "--porcelain")
    if dirty:
        raise SystemExit("G5 evidence requires a clean Image Agent worktree.")
    return git_output(root, "rev-parse", "HEAD")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    build = root / "build"
    build.mkdir(parents=True, exist_ok=True)
    log_path = build / "g5-gate.log"
    result_path = build / "g5-gate-result.json"
    commit, branch = require_clean_commit(root)
    image_root_value = os.environ.get("G5_IMAGE_AGENT_ROOT")
    if not image_root_value:
        raise SystemExit("G5_IMAGE_AGENT_ROOT must point to the release Image Agent source.")
    image_root = Path(image_root_value).resolve()
    image_commit = require_clean_image_baseline(image_root)
    make_prefix = shlex.split(os.environ.get("G5_MAKE", "make"))
    if not make_prefix:
        raise SystemExit("G5_MAKE must name the make executable.")
    started_at = utc_now()
    stage_results: list[dict[str, Any]] = []
    final_exit = 0
    with log_path.open("w", encoding="utf-8") as log:
        for name, target in STAGES:
            command = [*make_prefix, target]
            display = f"make {target}"
            stage_started_at = utc_now()
            monotonic_started = time.monotonic()
            digest = hashlib.sha256()
            line_count = 0
            header = f"\n=== G5 stage: {name} ({display}) ===\n"
            sys.stdout.write(header)
            log.write(header)
            log.flush()
            process = subprocess.Popen(
                command,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
                log.flush()
                digest.update(line.encode("utf-8"))
                line_count += 1
            exit_code = process.wait()
            stage_results.append(
                {
                    "name": name,
                    "command": display,
                    "exit_code": exit_code,
                    "started_at": stage_started_at,
                    "completed_at": utc_now(),
                    "duration_seconds": round(time.monotonic() - monotonic_started, 3),
                    "output_line_count": line_count,
                    "output_sha256": digest.hexdigest(),
                }
            )
            if exit_code != 0:
                final_exit = exit_code
                break
    completed_at = utc_now()
    stable_candidate = False
    if final_exit == 0 and len(stage_results) == len(STAGES):
        try:
            final_commit, final_branch = require_clean_commit(root)
            final_image_commit = require_clean_image_baseline(image_root)
            stable_candidate = (
                (final_commit, final_branch) == (commit, branch)
                and final_image_commit == image_commit
            )
        except SystemExit:
            stable_candidate = False
    result = {
        "schema_version": "1.1",
        "verification_command": "make g5-e2e",
        "commit": commit,
        "branch": branch,
        "status": "PASSED" if stable_candidate else "FAILED",
        "started_at": started_at,
        "completed_at": completed_at,
        "stages": stage_results,
        "dependencies": {
            "image_agent": {
                "commit": image_commit,
                "worktree_clean": True,
            }
        },
        "log": {
            "path": "build/g5-gate.log",
            "sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
            "size_bytes": log_path.stat().st_size,
        },
    }
    write_json(result_path, result)
    if final_exit != 0:
        raise SystemExit(final_exit)
    if not stable_candidate:
        raise SystemExit(
            "The checked commit, branch, or Image Agent baseline changed during the G5 gate."
        )
    print(f"G5 gate result written to {result_path.relative_to(root)}")


if __name__ == "__main__":
    main()
