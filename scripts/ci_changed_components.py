from __future__ import annotations

import argparse
import subprocess
from collections.abc import Iterable
from pathlib import Path

COMPONENTS = ("backend", "frontend", "windows", "documentation")
ZERO_SHA = "0" * 40


def _matches(path: str, *, prefixes: tuple[str, ...], exact: tuple[str, ...] = ()) -> bool:
    return path in exact or path.startswith(prefixes)


def select_components(paths: Iterable[str]) -> dict[str, bool]:
    normalized = {
        path.replace("\\", "/").removeprefix("./") for path in paths if path
    }
    if not normalized or any(path.startswith(".github/") for path in normalized):
        return {component: True for component in COMPONENTS}

    selection = {component: False for component in COMPONENTS}
    for path in normalized:
        if _matches(
            path,
            prefixes=("docs/",),
            exact=("README.md", "QUICKSTART.md", "CONTRIBUTING.md"),
        ):
            selection["documentation"] = True

        if _matches(
            path,
            prefixes=("frontend/", "contracts/"),
            exact=("scripts/generate_frontend_contracts.py",),
        ):
            selection["frontend"] = True

        if _matches(
            path,
            prefixes=("backend/", "tests/", "scripts/", "contracts/", "requirements/"),
            exact=(
                "Makefile",
                "pyproject.toml",
                "requirements-dev.txt",
                "requirements-runtime.txt",
                "runtime.yaml",
                "provider.yaml",
                "agents/image-agent.lock.json",
                "agents/image_agent_mvp",
            ),
        ):
            selection["backend"] = True

        if _matches(
            path,
            prefixes=(
                "backend/harness/runtime",
                "backend/harness/services/agent_config_materialization.py",
                "backend/harness/services/instance_runtime_settings.py",
                "backend/harness/services/process_",
                "backend/harness/services/runtime_config_",
                "backend/harness/services/start_operations.py",
                "backend/harness/services/supervisor",
                "backend/harness/services/task_config",
                "backend/harness/storage/atomic.py",
                "backend/harness/storage/locks.py",
                "backend/harness/storage/paths.py",
                "backend/harness/storage/safe_open.py",
                "tests/integration/test_image_agent_config.py",
                "tests/integration/test_runtime_settings_control_plane.py",
                "tests/integration/test_supervisor.py",
                "tests/unit/test_dev_launcher.py",
                "tests/unit/test_windows_runtime.py",
            ),
            exact=(
                "scripts/collect_windows_ci_diagnostics.ps1",
                "scripts/dev.py",
                "pyproject.toml",
                "requirements-dev.txt",
                "runtime.yaml",
                "provider.yaml",
                "agents/image-agent.lock.json",
                "agents/image_agent_mvp",
            ),
        ):
            selection["windows"] = True

    if not any(selection.values()):
        selection["backend"] = True
    return selection


def changed_paths(base: str, head: str) -> list[str]:
    if not head:
        raise ValueError("head commit is required")
    if not base or base == ZERO_SHA:
        command = [
            "git",
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "--diff-filter=ACMRTUXB",
            "-r",
            head,
        ]
    else:
        command = [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMRTUXB",
            base,
            head,
        ]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.splitlines()


def write_github_outputs(path: Path, selection: dict[str, bool]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        for component in COMPONENTS:
            output.write(f"{component}={str(selection[component]).lower()}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Select CI jobs from a Git diff")
    parser.add_argument("--base", default="")
    parser.add_argument("--head", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()

    selection = select_components(changed_paths(args.base, args.head))
    write_github_outputs(args.github_output, selection)
    for component in COMPONENTS:
        print(f"{component}: {selection[component]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
