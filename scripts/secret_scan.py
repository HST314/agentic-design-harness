"""Deterministic repository secret-pattern gate for source and fixtures."""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "provider-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "authorization": re.compile(r"\b(?:Basic|Bearer)\s+[A-Za-z0-9._~+/-]{16,}={0,2}\b", re.I),
}
SKIP_PARTS = {
    ".git",
    ".runtime",
    ".test-deps",
    ".venv",
    "node_modules",
    "dist",
    "__pycache__",
}
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".in",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
# The pinned upstream regression fixture proves that raw authorization values are
# discarded. Keep this allowlist exact so the same placeholder elsewhere still fails.
ALLOWED_REDACTION_FIXTURES = {
    Path("agents/image_agent_mvp/tests/test_refactor.py"): frozenset(
        {"Bearer " + "sensitive-provider-value"}
    )
}


def main(root: Path) -> int:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES or set(path.parts) & SKIP_PARTS:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.resolve().relative_to(root.resolve())
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                if match.group(0) in ALLOWED_REDACTION_FIXTURES.get(
                    relative, frozenset()
                ):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path}:{line}: {label}")
    if findings:
        print("Potential secrets found:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("Secret pattern scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")))
