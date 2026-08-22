"""Build a commit-bound RFC v0.3 workbench acceptance evidence index."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.generate_phase1_evidence import validate_gate_evidence
except ModuleNotFoundError:  # Direct execution adds scripts/, not the repository root.
    from generate_phase1_evidence import validate_gate_evidence


def validate_manifest(root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    items = manifest.get("acceptance_items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise SystemExit("workbench evidence manifest has malformed acceptance items")
    if [item.get("rfc_item") for item in items] != list(range(1, 16)):
        raise SystemExit("workbench evidence manifest must contain RFC items 1 through 15 in order")
    for item in items:
        if not item.get("claim") or not item.get("commands") or not item.get("evidence_files"):
            raise SystemExit("every workbench acceptance item needs a claim, command and evidence")
    evidence_files = sorted(
        {
            path
            for item in items
            for path in item["evidence_files"]
            if isinstance(path, str)
        }
    )
    missing = [path for path in evidence_files if not (root / path).is_file()]
    if missing:
        raise SystemExit("missing workbench evidence files: " + ", ".join(missing))
    return items


def git_output(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / "docs" / "verification" / "workbench-f5-evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = validate_manifest(root, manifest)
    commit = git_output(root, "rev-parse", "HEAD")
    if git_output(root, "status", "--porcelain"):
        raise SystemExit("workbench evidence generation requires a clean worktree")
    gate_result, gate_result_path, gate_log_sha256 = validate_gate_evidence(root, commit)
    evidence_files = sorted(
        {path for item in items for path in item["evidence_files"]}
    )
    gate_log_path = root / "build" / "g5-gate.log"
    output = {
        "schema_version": "1.0",
        "rfc": "docs/rfc-v0.3-workbench-design.md",
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
    destination = root / "build" / "workbench-f5-evidence.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Workbench F5 evidence index written to {destination.relative_to(root)}")


if __name__ == "__main__":
    main()
