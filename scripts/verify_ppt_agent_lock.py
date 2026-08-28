#!/usr/bin/env python3
"""Verify the PPT release lock, deterministic inputs, and embedded revision."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from verify_image_agent_lock import content_tree_sha256, portable_file_bytes

ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def main() -> int:
    lock: Any = json.loads(
        (ROOT / "agents" / "ppt-agent.lock.json").read_text(encoding="utf-8")
    )
    embedded_path = lock["embedded_path"]
    revision = lock["revision"]
    source = ROOT / embedded_path
    gitlink = _git("ls-files", "--stage", "--", embedded_path).split()
    if (
        len(gitlink) != 4
        or gitlink[0] != "160000"
        or gitlink[1] != revision
        or gitlink[2] != "0"
    ):
        raise ValueError("PPT Agent gitlink does not match the release revision")
    if _git("rev-parse", "HEAD", cwd=source) != revision:
        raise ValueError("PPT Agent checkout does not match the release revision")
    if content_tree_sha256(source) != lock["source_content_sha256"]:
        raise ValueError("PPT Agent source content does not match the release lock")
    digest = hashlib.sha256()
    for item in lock["dependencies"]["files"]:
        base = ROOT if item["scope"] == "harness" else source
        content = portable_file_bytes(base / item["path"])
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("PPT Agent dependency file digest does not match")
        digest.update(f"{item['scope']}:{item['path']}".encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    if digest.hexdigest() != lock["dependencies"]["lock_set_sha256"]:
        raise ValueError("PPT Agent dependency lock set does not match the release lock")
    print("PPT Agent release lock, deterministic dependencies and checkout verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
