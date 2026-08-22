#!/usr/bin/env python3
"""Generate and validate backend and frontend CycloneDX SBOMs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "build" / "sbom"


def _read_cyclonedx(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("bomFormat") != "CycloneDX":
        raise RuntimeError(f"{path.name} is not a CycloneDX document")
    components = document.get("components")
    if not isinstance(components, list) or not components:
        raise RuntimeError(f"{path.name} has no components")
    return document


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    python_output = OUTPUT_ROOT / "python-runtime.cdx.json"
    frontend_output = OUTPUT_ROOT / "frontend.cdx.json"

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip_audit",
            "-r",
            str(ROOT / "requirements-runtime.txt"),
            "--require-hashes",
            "--disable-pip",
            "--progress-spinner",
            "off",
            "--cache-dir",
            str(OUTPUT_ROOT / ".pip-audit-cache"),
            "--format",
            "cyclonedx-json",
            "--output",
            str(python_output),
        ],
        cwd=ROOT,
        check=True,
    )
    npm_result = subprocess.run(
        [
            "npm",
            "sbom",
            "--package-lock-only",
            "--sbom-format",
            "cyclonedx",
        ],
        cwd=ROOT / "frontend",
        check=True,
        capture_output=True,
        text=True,
    )
    frontend_output.write_text(npm_result.stdout, encoding="utf-8")

    python_document = _read_cyclonedx(python_output)
    frontend_document = _read_cyclonedx(frontend_output)
    print(
        "SBOM generation passed: "
        f"python={len(python_document['components'])} components, "
        f"frontend={len(frontend_document['components'])} components."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
