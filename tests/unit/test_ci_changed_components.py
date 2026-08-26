from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.ci_changed_components import select_components, write_github_outputs

ROOT = Path(__file__).resolve().parents[2]


def load_workflow(name: str) -> dict:
    path = ROOT / ".github" / "workflows" / name
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    if not isinstance(document, dict):
        raise AssertionError(f"{name} must contain one workflow object")
    return document


class CiChangedComponentsTests(unittest.TestCase):
    def test_workflow_changes_run_every_gate(self) -> None:
        self.assertEqual(
            select_components([".github/workflows/quality.yml"]),
            {
                "backend": True,
                "frontend": True,
                "windows": True,
                "documentation": True,
            },
        )

    def test_documentation_change_only_runs_documentation_gate(self) -> None:
        self.assertEqual(
            select_components(["docs/operations.md"]),
            {
                "backend": False,
                "frontend": False,
                "windows": False,
                "documentation": True,
            },
        )

    def test_frontend_change_only_runs_frontend_gate(self) -> None:
        self.assertEqual(
            select_components(["frontend/src/app/router.tsx"]),
            {
                "backend": False,
                "frontend": True,
                "windows": False,
                "documentation": False,
            },
        )

    def test_contract_change_runs_backend_and_frontend_gates(self) -> None:
        self.assertEqual(
            select_components(["contracts/v1/schemas/Task.schema.json"]),
            {
                "backend": True,
                "frontend": True,
                "windows": False,
                "documentation": False,
            },
        )

    def test_runtime_settings_change_runs_backend_and_windows_gates(self) -> None:
        self.assertEqual(
            select_components(
                ["backend/harness/services/instance_runtime_settings.py"]
            ),
            {
                "backend": True,
                "frontend": False,
                "windows": True,
                "documentation": False,
            },
        )

    def test_windows_diagnostics_change_runs_backend_and_windows_gates(self) -> None:
        self.assertEqual(
            select_components(["scripts/collect_windows_ci_diagnostics.ps1"]),
            {
                "backend": True,
                "frontend": False,
                "windows": True,
                "documentation": False,
            },
        )

    def test_unclassified_change_defaults_to_backend_gate(self) -> None:
        self.assertEqual(
            select_components([".gitignore"]),
            {
                "backend": True,
                "frontend": False,
                "windows": False,
                "documentation": False,
            },
        )

    def test_github_outputs_use_lowercase_booleans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "github-output"
            write_github_outputs(
                output_path,
                {
                    "backend": True,
                    "frontend": False,
                    "windows": True,
                    "documentation": False,
                },
            )
            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "backend=true\nfrontend=false\nwindows=true\ndocumentation=false\n",
            )

    def test_daily_and_release_workflows_have_distinct_triggers(self) -> None:
        daily = load_workflow("quality.yml")
        release = load_workflow("release-quality.yml")

        self.assertEqual(
            set(daily["on"]), {"push", "pull_request", "workflow_dispatch"}
        )
        self.assertEqual(set(release["on"]), {"schedule", "workflow_dispatch"})
        self.assertEqual(daily["concurrency"]["cancel-in-progress"], "true")
        self.assertEqual(release["concurrency"]["cancel-in-progress"], "true")

    def test_release_only_jobs_do_not_block_daily_quality(self) -> None:
        daily_jobs = set(load_workflow("quality.yml")["jobs"])
        release_jobs = set(load_workflow("release-quality.yml")["jobs"])

        release_only = {
            "backend_matrix",
            "frontend_matrix",
            "launcher",
            "p6_platform_acceptance",
            "workbench_real_image",
            "supply_chain",
        }
        self.assertTrue(release_only.isdisjoint(daily_jobs))
        self.assertTrue(release_only.issubset(release_jobs))

    def test_official_actions_use_current_node_runtime_major(self) -> None:
        for workflow_name in ("quality.yml", "release-quality.yml"):
            jobs = load_workflow(workflow_name)["jobs"]
            for job in jobs.values():
                for step in job.get("steps", []):
                    action = step.get("uses", "")
                    if action.startswith("actions/"):
                        self.assertTrue(
                            action.endswith("@v7"),
                            f"{workflow_name} has a stale official action: {action}",
                        )


if __name__ == "__main__":
    unittest.main()
