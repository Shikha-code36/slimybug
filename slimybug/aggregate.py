"""Aggregates one experiment's raw per-run artifacts into a flat summary table.

Reads every experiments/<experiment>/runs/<run_id>/{metadata,results}.json
and flattens them into one row per run -- the input to slimybug.analysis
and to each Experiment's analyze().
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

SUMMARY_FIELDNAMES = [
    "run_id",
    "rps",
    "injected_latency_ms",
    "retry_policy",
    "breaker_enabled",
    "breaker_window_size",
    "pool_size",
    "admission_control_enabled",
    "admission_control_mode",
    "admission_ewma_half_life_s",
    "admission_u_low",
    "admission_grace_ms",
    "throughput_rps",
    "error_rate",
    "p95_latency_ms",
    "p99_latency_ms",
    "amplification_factor",
    "retry_rate",
    "retry_success_rate",
    "probe_success_rate",
    "admission_rejection_rate",
    "a_offered_count",
    "a_timeout_count",
    "a_upstream_error_count",
    "b_received_count",
    "b_success_count",
    "b_pool_active_max",
    "b_pool_timeout_count",
    "b_query_timeout_count",
    "b_error_count",
    "b_admission_rejected_count",
    "b_admission_rejected_latency_p95_ms",
    "b_admission_ewma_utilization_max",
    "b_admission_ewma_utilization_mean",
    "saturated",
    "saturation_reasons",
]


def load_runs(runs_dir: Path) -> list[dict]:
    runs = []
    if not runs_dir.exists():
        return runs

    for run_dir in sorted(runs_dir.iterdir()):
        metadata_path = run_dir / "metadata.json"
        results_path = run_dir / "results.json"
        if not metadata_path.exists() or not results_path.exists():
            continue

        metadata = json.loads(metadata_path.read_text())
        results = json.loads(results_path.read_text())
        load = results.get("load_generator", {})
        app = results.get("application", {})
        service_a = app.get("service_a", {})
        service_b = app.get("service_b", {})

        runs.append(
            {
                "run_id": metadata["run_id"],
                "rps": metadata["rps"],
                "injected_latency_ms": metadata["injected_latency_ms"],
                "retry_policy": metadata.get("retry_policy", "none"),
                "breaker_enabled": metadata.get("breaker_enabled", False),
                "breaker_window_size": metadata.get("breaker_window_size"),
                "pool_size": metadata.get("pool_size"),
                "admission_control_enabled": metadata.get("admission_control_enabled", False),
                "admission_control_mode": metadata.get("admission_control_mode"),
                "admission_ewma_half_life_s": metadata.get("admission_ewma_half_life_s"),
                "admission_u_low": metadata.get("admission_u_low"),
                "admission_grace_ms": metadata.get("admission_grace_ms"),
                "throughput_rps": load.get("throughput_rps"),
                "error_rate": load.get("error_rate"),
                "p95_latency_ms": (load.get("latency_ms") or {}).get("p95"),
                "p99_latency_ms": (load.get("latency_ms") or {}).get("p99"),
                "amplification_factor": app.get("amplification_factor"),
                "retry_rate": app.get("retry_rate"),
                "retry_success_rate": app.get("retry_success_rate"),
                "probe_success_rate": app.get("probe_success_rate"),
                "admission_rejection_rate": app.get("admission_rejection_rate"),
                "a_offered_count": service_a.get("offered_count"),
                "a_timeout_count": service_a.get("timeout_count"),
                "a_upstream_error_count": service_a.get("upstream_error_count"),
                "b_received_count": service_b.get("received_count"),
                "b_success_count": service_b.get("success_count"),
                "b_pool_active_max": service_b.get("pool_active_max"),
                "b_pool_timeout_count": service_b.get("pool_timeout_count"),
                "b_query_timeout_count": service_b.get("query_timeout_count"),
                "b_error_count": service_b.get("error_count"),
                "b_admission_rejected_count": service_b.get("admission_rejected_count"),
                "b_admission_rejected_latency_p95_ms": service_b.get("admission_rejected_latency_p95_ms_max"),
                "b_admission_ewma_utilization_max": service_b.get("admission_ewma_utilization_max"),
                "b_admission_ewma_utilization_mean": service_b.get("admission_ewma_utilization_mean"),
            }
        )
    return runs


def write_csv(runs: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDNAMES)
        writer.writeheader()
        for r in runs:
            writer.writerow({k: r.get(k) for k in SUMMARY_FIELDNAMES})
