#!/usr/bin/env python3
"""Congela K3-v2 recalibrando sólo umbrales con una regla cuantílica."""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import analyze
import calibrate_controller as pilot
import select_state_model as selection


def freeze(args: argparse.Namespace) -> dict:
    parent_path = Path(args.parent_model)
    parent_bytes = parent_path.read_bytes()
    parent = json.loads(parent_bytes)
    variants = [value.strip() for value in args.development_variants.split(",")]
    records = []
    for variant in variants:
        for path in pilot.find_runs(Path(args.results), variant):
            item = pilot.load_series(path, args.warmup_ms)
            metadata = analyze.read_key_value(path / "run_metadata.csv")
            transition, recovery = analyze.phase_bounds_ms(path, metadata)
            records.append((variant, path.name, item, transition, recovery))
    if len(records) < args.minimum_development_runs:
        raise SystemExit(
            f"Se requieren {args.minimum_development_runs} corridas; hay {len(records)}"
        )

    parameters = {
        name: float(value) for name, value in parent["parameters"].items()
    }
    kalman_scores = []
    kalman_levels = []
    persistence_scores = []
    ewma_scores = []
    for _, _, item, _, _ in records:
        trace = selection.run_filter(
            item, parent["model"], parameters, float(parent["p0_diagonal"][0])
        )
        kalman_scores.append(selection.forecast_scores(
            trace, float(parent["horizon_ms"]), parent["model"], parameters
        ))
        kalman_levels.append(trace.level)
        persistence_scores.append(item.rtt_ms)
        ewma_scores.append(selection.ewma_scores(
            item, float(parent["baselines"]["ewma"]["alpha"])
        ))

    references = [
        analyze.median(item.rtt_ms[:args.baseline_samples])
        for _, _, item, _, _ in records
    ]
    if args.score_mode == "relative":
        kalman_scores = [
            [score - reference for score in run_scores]
            for run_scores, reference in zip(kalman_scores, references)
        ]
        persistence_scores = [
            [score - reference for score in run_scores]
            for run_scores, reference in zip(persistence_scores, references)
        ]
        ewma_scores = [
            [score - reference for score in run_scores]
            for run_scores, reference in zip(ewma_scores, references)
        ]
    elif args.score_mode == "kalman_increment":
        kalman_scores = [
            [score - level for score, level in zip(run_scores, levels)]
            for run_scores, levels in zip(kalman_scores, kalman_levels)
        ]
        persistence_scores = [
            [score - reference for score in run_scores]
            for run_scores, reference in zip(persistence_scores, references)
        ]
        ewma_scores = [
            [score - reference for score in run_scores]
            for run_scores, reference in zip(ewma_scores, references)
        ]

    by_method = {
        "kalman": kalman_scores,
        "persistence": persistence_scores,
        "ewma": ewma_scores,
    }
    thresholds = {}
    diagnostics = {}
    transition_rows = {}
    for method, by_run in by_method.items():
        stable = []
        for (_, _, item, transition, recovery), scores in zip(records, by_run):
            stable.extend(
                score for elapsed, score in zip(item.elapsed_ms, scores)
                if elapsed < transition - args.transition_guard_ms
                or elapsed >= recovery + args.transition_guard_ms
            )
        candidates = []
        for threshold in sorted(set(stable)):
            rows = [
                selection.transition_metrics(
                    item, scores, parent["endpoint"], threshold,
                    transition, recovery,
                    float(parent["minimum_useful_lead_ms"]),
                    int(parent["trigger_samples"]), args.transition_guard_ms,
                )
                for (_, _, item, transition, recovery), scores
                in zip(records, by_run)
            ]
            high_false = analyze.median([
                row["stable_high_active_fraction"] for row in rows
            ])
            recovery_false = analyze.median([
                row["recovery_active_fraction"] for row in rows
            ])
            if high_false <= args.maximum_false_fraction \
                    and recovery_false <= args.maximum_false_fraction:
                candidates.append({
                    "threshold": threshold,
                    "detected": sum(row["detected"] for row in rows),
                })
        if not candidates:
            raise SystemExit(f"Ningún umbral de {method} respeta el límite falso")
        maximum_detected = max(row["detected"] for row in candidates)
        plateau = [
            row["threshold"] for row in candidates
            if row["detected"] == maximum_detected
        ]
        threshold = 0.5 * (min(plateau) + max(plateau))
        thresholds[method] = threshold
        rows = []
        for (_, _, item, transition, recovery), scores in zip(records, by_run):
            rows.append(selection.transition_metrics(
                item, scores, parent["endpoint"], threshold,
                transition, recovery, float(parent["minimum_useful_lead_ms"]),
                int(parent["trigger_samples"]), args.transition_guard_ms,
            ))
        transition_rows[method] = rows
        feasible = sum(row["first_event_feasible"] for row in rows)
        detected = sum(row["detected"] for row in rows)
        leads = [row["lead_ms"] for row in rows if row["detected"]]
        diagnostics[method] = {
            "threshold_ms": threshold,
            "selection_rule": "maximum_detection_plateau_midpoint",
            "plateau_minimum_ms": min(plateau),
            "plateau_maximum_ms": max(plateau),
            "plateau_width_ms": max(plateau) - min(plateau),
            "feasible_runs": feasible,
            "detected_runs": detected,
            "detection_rate": detected / feasible if feasible else 0.0,
            "median_lead_ms": analyze.median(leads),
            "median_stable_high_active_fraction": analyze.median([
                row["stable_high_active_fraction"] for row in rows
            ]),
            "median_recovery_active_fraction": analyze.median([
                row["recovery_active_fraction"] for row in rows
            ]),
            "detected_run_ids": [
                f"{variant}/{run}"
                for (variant, run, _, _, _), row in zip(records, rows)
                if row["detected"]
            ],
            "missed_run_ids": [
                f"{variant}/{run}"
                for (variant, run, _, _, _), row in zip(records, rows)
                if not row["detected"]
            ],
        }
    wins = 0
    for index, row in enumerate(transition_rows["kalman"]):
        baseline_detected = max(
            transition_rows["persistence"][index]["detected"],
            transition_rows["ewma"][index]["detected"],
        )
        baseline_lead = max(
            transition_rows["persistence"][index]["lead_ms"],
            transition_rows["ewma"][index]["lead_ms"],
        )
        wins += bool(
            row["detected"]
            and (not baseline_detected or row["lead_ms"] > baseline_lead)
        )
    diagnostics["kalman"]["transition_wins"] = wins
    diagnostics["kalman"]["transition_win_fraction"] = wins / len(records)
    kalman_diagnostics = diagnostics["kalman"]
    eligible = (
        kalman_diagnostics["detection_rate"] >= 0.8
        and kalman_diagnostics["transition_win_fraction"] >= 0.6
        and kalman_diagnostics["median_stable_high_active_fraction"]
        <= args.maximum_false_fraction
        and kalman_diagnostics["median_recovery_active_fraction"]
        <= args.maximum_false_fraction
    )

    revised = deepcopy(parent)
    revised.update({
        "status": (
            "threshold_recalibrated_requires_fresh_validation"
            if eligible else "no_eligible_threshold_revision"
        ),
        "version": "K3-v2",
        "parent_model": str(parent_path),
        "parent_model_sha256": hashlib.sha256(parent_bytes).hexdigest(),
        "parameters_refit": False,
        "threshold_revision": {
            "rule": "maximum_detection_plateau_midpoint_under_false_limit",
            "score_mode": args.score_mode,
            "baseline_samples": args.baseline_samples,
            "transition_guard_ms": args.transition_guard_ms,
            "development_variants": variants,
            "development_runs": [
                f"{variant}/{run}" for variant, run, _, _, _ in records
            ],
            "prior_validation_reusable": False,
            "only_revision_allowed": True,
        },
        "trigger_threshold_ms": thresholds["kalman"],
        "development_v2_diagnostics": diagnostics,
    })
    revised["baselines"]["persistence"]["threshold_ms"] = thresholds["persistence"]
    revised["baselines"]["ewma"]["threshold_ms"] = thresholds["ewma"]

    output = Path(args.output)
    analyze.write_json(output, revised)
    report = {
        "status": revised["status"],
        "version": revised["version"],
        "development_runs": len(records),
        "score_mode": args.score_mode,
        "baseline_samples": args.baseline_samples,
        "model_parameters_changed": False,
        "thresholds_ms": thresholds,
        "diagnostics": diagnostics,
        "output": str(output),
        "warning": "K3-v2 requiere otra validación independiente.",
    }
    analyze.write_json(output.parent / "threshold_v2_report.json", report)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--results", default="results")
    result.add_argument(
        "--parent-model", default="results/model_development/provisional_model.json"
    )
    result.add_argument(
        "--development-variants", default="fixed25,modelval25"
    )
    result.add_argument(
        "--output", default="results/model_development_v2/frozen_model.json"
    )
    result.add_argument("--minimum-development-runs", type=int, default=15)
    result.add_argument(
        "--score-mode",
        choices=("absolute", "relative", "kalman_increment"),
        default="absolute",
    )
    result.add_argument("--baseline-samples", type=int, default=30)
    result.add_argument("--warmup-ms", type=float, default=3000.0)
    result.add_argument("--transition-guard-ms", type=float, default=500.0)
    result.add_argument("--maximum-false-fraction", type=float, default=0.05)
    return result


if __name__ == "__main__":
    print(json.dumps(freeze(parser().parse_args()), indent=2, sort_keys=True))
