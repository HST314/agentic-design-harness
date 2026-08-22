#!/usr/bin/env python3
"""Repeatable file-store write, index-read and cold-recovery benchmark."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from harness.contracts import ContractRegistry
from harness.domain.commands import CommandEnvelope
from harness.domain.service import TaskCommandService
from harness.storage.atomic import read_json
from harness.storage.store import FileStateStore

ROOT = Path(__file__).resolve().parents[1]
SLO_PATH = ROOT / "config" / "single-machine-capacity-slo.json"


def _milliseconds(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _directory_bytes(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def _profile(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    slo = json.loads(SLO_PATH.read_text(encoding="utf-8"))
    profile = slo[f"{name}_profile"]
    return slo, profile


def run(profile_name: str) -> dict[str, Any]:
    slo, profile = _profile(profile_name)
    task_count = int(profile["tasks"])
    events_per_task = int(profile["events_per_task"])
    list_samples = int(profile["list_samples"])
    with tempfile.TemporaryDirectory(prefix="harness-capacity-") as temporary:
        root = Path(temporary)
        contracts = ContractRegistry(ROOT / "contracts" / "v1")
        store = FileStateStore(root / "control", root / "workspace", contracts, 5.0)
        store.start()
        commands = TaskCommandService(store, contracts)
        create_latencies: list[float] = []
        update_latencies: list[float] = []
        for task_number in range(task_count):
            task_id = f"benchmark_{task_number:05d}"
            started = time.perf_counter()
            commands.create_task(
                task_id=task_id,
                title=f"Benchmark task {task_number}",
                goal="Measure deterministic single-machine storage behavior.",
                master_owner="master_benchmark",
                start_policy="manual",
                input_manifest="inputs/manifests/input_0.json",
                envelope=CommandEnvelope(
                    idempotency_key=f"create_{task_number:05d}",
                    actor_type="system",
                    actor_id="capacity_benchmark",
                    expected_revision=0,
                ),
            )
            create_latencies.append(_milliseconds(started))
            revision = 1
            for event_number in range(1, events_per_task):
                started = time.perf_counter()
                commands.register_input_manifest(
                    task_id,
                    f"inputs/manifests/input_{event_number}.json",
                    CommandEnvelope(
                        idempotency_key=f"update_{task_number:05d}_{event_number:04d}",
                        actor_type="system",
                        actor_id="capacity_benchmark",
                        expected_revision=revision,
                    ),
                )
                update_latencies.append(_milliseconds(started))
                revision += 1

        index_latencies: list[float] = []
        task_index = store.layout.control_root / "indexes" / "task-index.json"
        for _ in range(list_samples):
            started = time.perf_counter()
            index = read_json(task_index)
            if len(index["tasks"]) != task_count:
                raise RuntimeError("task index is incomplete")
            index_latencies.append(_milliseconds(started))

        control_bytes = _directory_bytes(store.layout.control_root)
        store.close()
        recovered = FileStateStore(
            root / "control",
            root / "workspace",
            ContractRegistry(ROOT / "contracts" / "v1"),
            5.0,
        )
        started = time.perf_counter()
        warnings = recovered.start()
        recovery_ms = _milliseconds(started)
        recovered.close()
        if warnings:
            raise RuntimeError(f"clean benchmark recovery emitted warnings: {warnings!r}")

    metrics = {
        "task_create_p95_ms": round(_p95(create_latencies), 3),
        "task_update_p95_ms": round(_p95(update_latencies), 3),
        "task_index_read_p95_ms": round(_p95(index_latencies), 3),
        "cold_recovery_ms": round(recovery_ms, 3),
        "control_bytes_per_task": round(control_bytes / max(1, task_count), 3),
    }
    thresholds = profile["thresholds"]
    checks = {
        name: {
            "actual": value,
            "maximum": thresholds[name],
            "passed": value <= thresholds[name],
        }
        for name, value in metrics.items()
    }
    return {
        "schema_version": "capacity-benchmark.v1",
        "profile": profile_name,
        "scope": slo["scope"],
        "dataset": {
            "tasks": task_count,
            "events_per_task": events_per_task,
            "total_task_events": task_count * events_per_task,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "metrics": metrics,
        "checks": checks,
        "passed": all(item["passed"] for item in checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("ci", "qualification"), default="ci")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.profile)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
