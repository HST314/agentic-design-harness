"""Fail when Harness runtime imports an Agent implementation package."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

FORBIDDEN = ("image_agent", "image_agent_mvp", "ppt_agent", "ppt_agent_mvp")


def main(root: Path) -> int:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.startswith(FORBIDDEN):
                    violations.append(f"{path}:{node.lineno}: {name}")
    if violations:
        print("Forbidden Agent implementation imports:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    print(f"Agent import boundary verified across {root}.")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("backend/harness")
    raise SystemExit(main(target))
