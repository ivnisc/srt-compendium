#!/usr/bin/env python3
"""Determina si algún umbral escalar satisface la compuerta de desarrollo v2."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import analyze
import calibrate_controller as pilot
import select_state_model as selection


def diagnose(args: argparse.Namespace) -> dict:
    model = json.loads(Path(args.model).read_text())
    parameters = {name: float(value) for name, value in model["parameters"].items()}
    records = []
    for variant in args.variants.split(","):
        for path in pilot.find_runs(Path(args.results), variant.strip()):
            item = pilot.load_series(path, args.warmup_ms)
            metadata = analyze.read_key_value(path / "run_metadata.csv")
            transition, recovery = analyze.phase_bounds_ms(path, metadata)
            trace = selection.run_filter(
                item, model["model"], parameters, float(model["p0_diagonal"][0])
            )
            forecast = selection.forecast_scores(
                trace, float(model["horizon_ms"]), model["model"], parameters
            )
            reference = analyze.median(item.rtt_ms[:args.baseline_samples])
            records.append({
                "id": f"{variant.strip()}/{path.name}",
                "item": item,
                "transition": transition,
                "recovery": recovery,
                "absolute": forecast,
                "relative": [value - reference for value in forecast],
                "increment": [
                    value - level for value, level in zip(forecast, trace.level)
                ],
            })

    required = int(args.required_detection_fraction * len(records) + 0.999999)
    modes = {}
    for mode in ("absolute", "relative", "increment"):
        stable = []
        for record in records:
            stable.extend(
                score for elapsed, score in zip(
                    record["item"].elapsed_ms, record[mode]
                )
                if elapsed < record["transition"] - args.transition_guard_ms
                or elapsed >= record["recovery"] + args.transition_guard_ms
            )
        thresholds = sorted(set(stable))
        passing_false_limit = []
        for threshold in thresholds:
            rows = [
                selection.transition_metrics(
                    record["item"], record[mode], model["endpoint"], threshold,
                    record["transition"], record["recovery"],
                    float(model["minimum_useful_lead_ms"]),
                    int(model["trigger_samples"]), args.transition_guard_ms,
                ) for record in records
            ]
            high_false = analyze.median([
                row["stable_high_active_fraction"] for row in rows
            ])
            recovery_false = analyze.median([
                row["recovery_active_fraction"] for row in rows
            ])
            if high_false > args.maximum_false_fraction \
                    or recovery_false > args.maximum_false_fraction:
                continue
            detected = sum(row["detected"] for row in rows)
            leads = [row["lead_ms"] for row in rows if row["detected"]]
            passing_false_limit.append({
                "threshold": threshold,
                "detected_runs": detected,
                "detection_rate": detected / len(records),
                "median_lead_ms": analyze.median(leads),
                "median_stable_high_active_fraction": high_false,
                "median_recovery_active_fraction": recovery_false,
                "detected_run_ids": [
                    record["id"] for record, row in zip(records, rows)
                    if row["detected"]
                ],
            })
        best = max(
            passing_false_limit,
            key=lambda row: (row["detected_runs"], row["median_lead_ms"]),
        ) if passing_false_limit else None
        operational = [
            row for row in passing_false_limit
            if row["detected_runs"] >= required
        ]
        modes[mode] = {
            "thresholds_examined": len(thresholds),
            "thresholds_respecting_false_limit": len(passing_false_limit),
            "best": best,
            "operational_thresholds": len(operational),
            "operational_threshold_minimum": (
                min(row["threshold"] for row in operational)
                if operational else None
            ),
            "operational_threshold_maximum": (
                max(row["threshold"] for row in operational)
                if operational else None
            ),
            "operational_threshold_width": (
                max(row["threshold"] for row in operational)
                - min(row["threshold"] for row in operational)
                if operational else None
            ),
        }

    report = {
        "status": "post_hoc_feasibility_only",
        "runs": len(records),
        "required_detected_runs": required,
        "maximum_false_fraction": args.maximum_false_fraction,
        "modes": modes,
        "any_scalar_score_feasible": any(
            value["best"] and value["best"]["detected_runs"] >= required
            for value in modes.values()
        ),
        "validation_reusable": False,
    }
    output = Path(args.output)
    analyze.write_json(output, report)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--results", default="results")
    result.add_argument("--variants", default="fixed25,modelval25")
    result.add_argument(
        "--model", default="results/model_development/provisional_model.json"
    )
    result.add_argument(
        "--output", default="results/model_validation/score_feasibility.json"
    )
    result.add_argument("--warmup-ms", type=float, default=3000.0)
    result.add_argument("--baseline-samples", type=int, default=30)
    result.add_argument("--transition-guard-ms", type=float, default=500.0)
    result.add_argument("--maximum-false-fraction", type=float, default=0.05)
    result.add_argument("--required-detection-fraction", type=float, default=0.8)
    return result


if __name__ == "__main__":
    print(json.dumps(diagnose(parser().parse_args()), indent=2, sort_keys=True))
