#!/usr/bin/env python3
"""Run the production Image runtime attestation for launcher diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from harness.adapters.image_attestation import attest_image_runtime  # noqa: E402
from harness.adapters.image_lock import load_image_agent_lock  # noqa: E402
from harness.adapters.image_runtime import ImageRuntimeBuilder  # noqa: E402
from harness.core.errors import HarnessError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--harness-root", type=Path, required=True)
    parser.add_argument("--interpreter", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path)
    args = parser.parse_args(argv)
    try:
        attestation = attest_image_runtime(
            load_image_agent_lock(args.lock),
            source_root=args.source,
            dependency_root=args.dependencies,
            harness_root=args.harness_root,
            interpreter=args.interpreter,
        )
        result: dict[str, object] = attestation.as_dict()
        if args.cache_root is not None:
            started = time.monotonic()
            builder = ImageRuntimeBuilder.from_attestation(
                args.source,
                args.dependencies,
                attestation,
            )
            artifact_root = builder.prepare(args.cache_root)
            result.update(
                {
                    "artifact_root": str(artifact_root),
                    "artifact_cache_hit": builder.cache_hit,
                    "artifact_prepare_duration_seconds": round(
                        time.monotonic() - started, 3
                    ),
                }
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
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
