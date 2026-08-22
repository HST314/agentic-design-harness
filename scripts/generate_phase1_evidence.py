"""Build a reproducible RFC v0.2 Phase 1 evidence index."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

EXPECTED_GATE_STAGES = [
    ("verify", "make verify"),
    ("g3-real-closure", "make g3-e2e"),
    ("g4-multi-instance", "make g4-e2e"),
    ("frontend-browser", "make frontend-e2e"),
    ("workbench-real-stack", "make frontend-integration"),
]


def validate_gate_evidence(
    root: Path, commit: str
) -> tuple[dict[str, object], Path, str]:
    gate_result_path = root / "build" / "g5-gate-result.json"
    gate_log_path = root / "build" / "g5-gate.log"
    if not gate_result_path.is_file() or not gate_log_path.is_file():
        raise SystemExit("run make g5-e2e to produce gate result and log evidence first")
    gate_result = json.loads(gate_result_path.read_text(encoding="utf-8"))
    stages = gate_result.get("stages")
    if not isinstance(stages, list) or not all(
        isinstance(item, dict) for item in stages
    ):
        raise SystemExit("the G5 gate result has a malformed stage summary")
    actual_stages = [
        (item.get("name"), item.get("command")) for item in stages
    ]
    dependencies = gate_result.get("dependencies")
    image_baseline = (
        dependencies.get("image_agent") if isinstance(dependencies, dict) else None
    )
    valid_gate = (
        gate_result.get("verification_command") == "make g5-e2e"
        and gate_result.get("commit") == commit
        and gate_result.get("status") == "PASSED"
        and actual_stages == EXPECTED_GATE_STAGES
        and all(item.get("exit_code") == 0 for item in stages)
        and isinstance(image_baseline, dict)
        and isinstance(image_baseline.get("commit"), str)
        and re.fullmatch(r"[0-9a-f]{40}", image_baseline["commit"]) is not None
        and image_baseline.get("worktree_clean") is True
    )
    if not valid_gate:
        raise SystemExit("the G5 gate result is failed, incomplete or for another commit")
    gate_log_sha256 = hashlib.sha256(gate_log_path.read_bytes()).hexdigest()
    log_summary = gate_result.get("log")
    if (
        not isinstance(log_summary, dict)
        or log_summary.get("sha256") != gate_log_sha256
    ):
        raise SystemExit("the G5 gate log does not match its recorded digest")
    return gate_result, gate_result_path, gate_log_sha256


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / "docs" / "verification" / "phase1-evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest.get("acceptance_items")
    if not isinstance(items, list) or [item.get("rfc_item") for item in items] != list(
        range(1, 19)
    ):
        raise SystemExit("evidence manifest must contain RFC items 1 through 18 in order")
    evidence_files = sorted(
        {
            path
            for item in items
            for path in item.get("evidence_files", [])
            if isinstance(path, str)
        }
    )
    missing = [path for path in evidence_files if not (root / path).is_file()]
    if missing:
        raise SystemExit("missing evidence files: " + ", ".join(missing))
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise SystemExit("evidence generation requires a clean worktree")
    gate_log_path = root / "build" / "g5-gate.log"
    gate_result, gate_result_path, gate_log_sha256 = validate_gate_evidence(
        root, commit
    )
    output = {
        "schema_version": "1.0",
        "rfc": "docs/rfc-v0.2.md",
        "commit": commit,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "verification_command": "make g5-e2e",
        "gate_result": {
            "path": "build/g5-gate-result.json",
            "sha256": hashlib.sha256(gate_result_path.read_bytes()).hexdigest(),
            "status": gate_result["status"],
            "started_at": gate_result["started_at"],
            "completed_at": gate_result["completed_at"],
            "stages": gate_result["stages"],
            "dependencies": gate_result["dependencies"],
        },
        "gate_log": {
            "path": "build/g5-gate.log",
            "sha256": gate_log_sha256,
            "size_bytes": gate_log_path.stat().st_size,
        },
        "acceptance_items": items,
        "file_sha256": {
            path: hashlib.sha256((root / path).read_bytes()).hexdigest()
            for path in evidence_files
        },
    }
    destination = root / "build" / "phase1-evidence.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Phase 1 evidence index written to {destination.relative_to(root)}")


if __name__ == "__main__":
    main()
