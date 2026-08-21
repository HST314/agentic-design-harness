"""Build a reproducible RFC v0.2 Phase 1 evidence index."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


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
    output = {
        "schema_version": "1.0",
        "rfc": "docs/rfc-v0.2.md",
        "commit": commit,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "verification_command": "make g5-e2e",
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
