"""Apply the managed write policy and replace this helper with the Agent."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        return 64
    roots = json.loads(sys.argv[1])
    if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
        return 64
    path = Path(__file__).with_name("write_sandbox.py")
    spec = importlib.util.spec_from_file_location("_harness_write_sandbox", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("The managed write sandbox module cannot be loaded.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.apply_write_sandbox(roots)
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
