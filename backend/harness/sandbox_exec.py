"""Apply the managed write policy and replace this helper with the Agent."""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
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
    if os.name == "nt":
        return _run_windows_python_child(sys.argv[2:])
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    return 70


def _run_windows_python_child(command: list[str]) -> int:
    """Run the pinned Python command without discarding the Windows audit hook."""

    if len(command) < 2 or Path(command[0]).resolve() != Path(sys.executable).resolve():
        raise RuntimeError("The Windows write sandbox only accepts the managed Python child.")
    if command[1] == "-c" and len(command) >= 3:
        sys.argv = ["-c", *command[3:]]
        namespace = {"__name__": "__main__", "__builtins__": __builtins__}
        exec(compile(command[2], "<managed-agent-launcher>", "exec"), namespace, namespace)
        return 0
    script = Path(command[1])
    if (
        not script.is_absolute()
        or script.suffix.lower() != ".py"
        or script.is_symlink()
        or not script.is_file()
    ):
        raise RuntimeError("The Windows write sandbox only accepts the managed Python child.")
    sys.argv = [str(script), *command[2:]]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
