#!/usr/bin/env python3
"""Run the production Image runtime attestation for launcher diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from harness.adapters.image_attestation import attest_image_runtime  # noqa: E402
from harness.adapters.image_lock import load_image_agent_lock  # noqa: E402
from harness.core.errors import HarnessError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--harness-root", type=Path, required=True)
    parser.add_argument("--interpreter", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        attestation = attest_image_runtime(
            load_image_agent_lock(args.lock),
            source_root=args.source,
            dependency_root=args.dependencies,
            harness_root=args.harness_root,
            interpreter=args.interpreter,
        )
    except HarnessError as exc:
        print(
            json.dumps(
                {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(attestation.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
