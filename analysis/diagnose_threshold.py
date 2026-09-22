#!/usr/bin/env python3
"""Explora post hoc el compromiso del umbral sin producir una política válida."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import analyze
import calibrate_controller as pilot
import select_state_model as selection


def diagnose(args: argparse.Namespace) -> dict:
    model = json.loads(Path(args.model).read_text())
    paths = pilot.find_runs(Path(args.results), args.variant)
    series = [pilot.load_series(path, args.warmup_ms) for path in paths]
    phases = {}
    for path, item in zip(paths, series):
        metadata = analyze.read_key_value(path / "run_metadata.csv")
        transition, recovery = analyze.phase_bounds_ms(path, metadata)
        phases[item.run_id] = (transition, recovery)

    parameters = {name: float(value) for name, value in model["parameters"].items()}
    traces = [
        selection.run_filter(
            item, model["model"], parameters, float(model["p0_diagonal"][0])
        ) for item in series
    ]
    scores = [
        selection.forecast_scores(
            trace, float(model["horizon_ms"]), model["model"], parameters
        ) for trace in traces
    ]
    baselines = {
        "persistence": [item.rtt_ms for item in series],
        "ewma": [
            selection.ewma_scores(
                item, float(model["baselines"]["ewma"]["alpha"])
            ) for item in series
        ],
    }
    baseline_rows = {}
    for name, by_run in baselines.items():
        threshold = float(model["baselines"][name]["threshold_ms"])
        baseline_rows[name] = [
            selection.transition_metrics(
                item, run_scores, model["endpoint"], threshold,
                *phases[item.run_id], float(model["minimum_useful_lead_ms"]),
                int(model["trigger_samples"]), args.transition_guard_ms,
            ) for item, run_scores in zip(series, by_run)
        ]

    stable_scores = []
    for item, run_scores in zip(series, scores):
        transition, recovery = phases[item.run_id]
        stable_scores.extend(
            score for elapsed, score in zip(item.elapsed_ms, run_scores)
            if elapsed < transition - args.transition_guard_ms
            or elapsed >= recovery + args.transition_guard_ms
        )
    thresholds = sorted(set(
        [float(model["trigger_threshold_ms"])]
        + [analyze.percentile(stable_scores, percentile / 1000.0)
           for percentile in range(500, 1000)]
    ))
    rows = []
    for threshold in thresholds:
        metrics = [
            selection.transition_metrics(
                item, run_scores, model["endpoint"], threshold,
                *phases[item.run_id], float(model["minimum_useful_lead_ms"]),
                int(model["trigger_samples"]), args.transition_guard_ms,
            ) for item, run_scores in zip(series, scores)
        ]
        feasible = sum(row["first_event_feasible"] for row in metrics)
        detected = sum(row["detected"] for row in metrics)
        leads = [row["lead_ms"] for row in metrics if row["detected"]]
        wins = 0
        for index, row in enumerate(metrics):
            baseline_detected = max(
                baseline_rows["persistence"][index]["detected"],
                baseline_rows["ewma"][index]["detected"],
            )
            baseline_lead = max(
                baseline_rows["persistence"][index]["lead_ms"],
                baseline_rows["ewma"][index]["lead_ms"],
            )
            wins += bool(
                row["detected"]
                and (not baseline_detected or row["lead_ms"] > baseline_lead)
            )
        result = {
            "threshold_ms": threshold,
            "detection_rate": detected / feasible if feasible else 0.0,
            "transition_win_fraction": wins / len(series),
            "median_lead_ms": analyze.median(leads),
            "median_stable_high_active_fraction": analyze.median([
                row["stable_high_active_fraction"] for row in metrics
            ]),
            "median_recovery_active_fraction": analyze.median([
                row["recovery_active_fraction"] for row in metrics
            ]),
        }
        result["passes_operational_gates"] = (
            result["detection_rate"] >= 0.8
            and result["transition_win_fraction"] >= 0.6
            and result["median_lead_ms"] > float(model["minimum_useful_lead_ms"])
            and result["median_stable_high_active_fraction"] <= 0.05
            and result["median_recovery_active_fraction"] <= 0.05
        )
        rows.append(result)

    current = min(
        rows, key=lambda row: abs(
            row["threshold_ms"] - float(model["trigger_threshold_ms"])
        )
    )
    passing = [row for row in rows if row["passes_operational_gates"]]
    conservative = max(passing, key=lambda row: row["threshold_ms"]) if passing else None
    quantile_checkpoints = {}
    for quantile in (0.95, 0.975, 0.99, 0.995):
        threshold = analyze.percentile(stable_scores, quantile)
        quantile_checkpoints[str(quantile)] = min(
            rows, key=lambda row: abs(row["threshold_ms"] - threshold)
        )
    output = Path(args.output_dir)
    analyze.write_csv(output / "threshold_sensitivity.csv", rows)
    report = {
        "status": "post_hoc_diagnostic",
        "validation_reusable": False,
        "model_parameters_changed": False,
        "current_threshold": current,
        "passing_thresholds": len(passing),
        "passing_threshold_minimum_ms": (
            min(row["threshold_ms"] for row in passing) if passing else None
        ),
        "passing_threshold_maximum_ms": (
            max(row["threshold_ms"] for row in passing) if passing else None
        ),
        "most_conservative_passing_threshold": conservative,
        "stable_score_quantile_checkpoints": quantile_checkpoints,
        "interpretation": (
            "La existencia de un umbral post hoc sólo justifica una nueva "
            "iteración de desarrollo y otra validación independiente."
        ),
    }
    analyze.write_json(output / "threshold_diagnostic.json", report)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--results", default="results")
    result.add_argument("--variant", default="modelval25")
    result.add_argument(
        "--model", default="results/model_development/provisional_model.json"
    )
    result.add_argument("--output-dir", default="results/model_validation")
    result.add_argument("--warmup-ms", type=float, default=3000.0)
    result.add_argument("--transition-guard-ms", type=float, default=500.0)
    return result


if __name__ == "__main__":
    print(json.dumps(diagnose(parser().parse_args()), indent=2, sort_keys=True))
