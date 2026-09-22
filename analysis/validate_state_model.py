#!/usr/bin/env python3
"""Valida un modelo congelado sin reajustar matrices, umbrales ni baselines."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import analyze
import calibrate_controller as pilot
import select_state_model as selection


def median(rows: list[dict], field: str) -> float:
    return analyze.median([analyze.numeric(row[field]) for row in rows])


def validate(args: argparse.Namespace) -> dict:
    model_path = Path(args.model)
    model = json.loads(model_path.read_text())
    if model.get("status") not in (
            "development_candidate_not_validated",
            "threshold_recalibrated_requires_fresh_validation"):
        raise SystemExit("El archivo no contiene un candidato de desarrollo congelable")
    paths = pilot.find_runs(Path(args.results), args.variant)
    minimum_runs = int(model["minimum_fresh_validation_runs"])
    if len(paths) < minimum_runs:
        raise SystemExit(
            f"Se requieren {minimum_runs} corridas nuevas; hay {len(paths)}"
        )
    series = [pilot.load_series(path, args.warmup_ms) for path in paths]
    phases = {}
    run_metadata = []
    tx_metadata = []
    for path, item in zip(paths, series):
        metadata = analyze.read_key_value(path / "run_metadata.csv")
        sender = analyze.read_key_value(path / "tx_metadata.csv")
        run_metadata.append(metadata)
        tx_metadata.append(sender)
        transition_ms, recovery_ms = analyze.phase_bounds_ms(path, metadata)
        phases[item.run_id] = (transition_ms, recovery_ms)

    constant_fields = (
        "scenario", "variant", "input_rate_bps", "source_probe_bps",
        "duration_s", "phase_high_s", "phase_low_s", "latency_ms",
        "delay_ms", "loss_percent", "sample_ms", "topology",
        "high_capacity_percent", "low_capacity_percent", "source_sha256",
        "srt_git_commit",
    )
    integrity = {
        "configuration_consistent": all(
            len({metadata.get(field, "") for metadata in run_metadata}) == 1
            for field in constant_fields
        ),
        "payload_identical": (
            len({sender.get("payload_bytes_read", "") for sender in tx_metadata}) == 1
            and all(
                sender.get("payload_bytes_read") == sender.get("payload_budget_bytes")
                for sender in tx_metadata
            )
        ),
        "controller_absent": all(
            sender.get("mode") == "vanilla"
            and analyze.numeric(sender.get("controller_changes")) == 0.0
            and analyze.numeric(sender.get("final_ohead")) == 25.0
            for sender in tx_metadata
        ),
        "sender_without_drops": all(
            analyze.numeric(sender.get("pktSndDropTotal")) == 0.0
            for sender in tx_metadata
        ),
        "srt_version_consistent": (
            len({sender.get("srt_version_hex", "") for sender in tx_metadata}) == 1
        ),
    }

    model_name = model["model"]
    parameters = {name: float(value) for name, value in model["parameters"].items()}
    horizon_ms = float(model["horizon_ms"])
    endpoint = model["endpoint"]
    minimum_lead_ms = float(model["minimum_useful_lead_ms"])
    trigger_samples = int(model["trigger_samples"])
    burn_in = int(model["burn_in_samples"])
    p0_scale = float(model["p0_diagonal"][0])
    traces = [
        selection.run_filter(item, model_name, parameters, p0_scale)
        for item in series
    ]
    scores = {
        "kalman": [
            selection.forecast_scores(trace, horizon_ms, model_name, parameters)
            for trace in traces
        ],
        "persistence": [item.rtt_ms for item in series],
        "ewma": [
            selection.ewma_scores(
                item, float(model["baselines"]["ewma"]["alpha"])
            ) for item in series
        ],
    }
    thresholds = {
        "kalman": float(model["trigger_threshold_ms"]),
        "persistence": float(model["baselines"]["persistence"]["threshold_ms"]),
        "ewma": float(model["baselines"]["ewma"]["threshold_ms"]),
    }
    rows = []
    for index, item in enumerate(series):
        row: dict[str, float | str] = {"run": item.run_id}
        transition_ms, recovery_ms = phases[item.run_id]
        for method, by_run in scores.items():
            transition = selection.transition_metrics(
                item, by_run[index], endpoint, thresholds[method],
                transition_ms, recovery_ms, minimum_lead_ms, trigger_samples,
                args.transition_guard_ms,
            )
            labels, valid = selection.future_episode_labels(
                item, endpoint, args.episode_quiet_ms,
                minimum_lead_ms, horizon_ms,
            )
            classification = selection.classification(
                by_run[index], labels, valid, thresholds[method]
            )
            forecast = selection.forecast_metrics(
                item, by_run[index], horizon_ms, burn_in
            )
            for key, value in transition.items():
                row[f"{method}_transition_{key}"] = value
            for key, value in classification.items():
                row[f"{method}_{key}"] = value
            row[f"{method}_forecast_rmse"] = forecast["rmse"]
        innovation = selection.standardized_innovation_metrics(
            traces[index], burn_in
        )
        row.update(innovation)
        baseline_detected = max(
            analyze.numeric(row["persistence_transition_detected"]),
            analyze.numeric(row["ewma_transition_detected"]),
        )
        baseline_lead = max(
            analyze.numeric(row["persistence_transition_lead_ms"]),
            analyze.numeric(row["ewma_transition_lead_ms"]),
        )
        row["kalman_transition_win"] = float(
            analyze.numeric(row["kalman_transition_detected"])
            and (
                not baseline_detected
                or analyze.numeric(row["kalman_transition_lead_ms"]) > baseline_lead
            )
        )
        rows.append(row)

    runs = len(rows)
    feasible = sum(
        analyze.numeric(row["kalman_transition_first_event_feasible"])
        for row in rows
    )
    detections = sum(
        analyze.numeric(row["kalman_transition_detected"]) for row in rows
    )
    wins = sum(analyze.numeric(row["kalman_transition_win"]) for row in rows)
    leads = [
        analyze.numeric(row["kalman_transition_lead_ms"]) for row in rows
        if analyze.numeric(row["kalman_transition_detected"])
    ]
    summary = {
        "runs": runs,
        "feasible_transition_runs": feasible,
        "feasible_transition_fraction": feasible / runs,
        "kalman_transition_detections": detections,
        "kalman_detection_rate": detections / feasible if feasible else 0.0,
        "persistence_transition_detections": sum(
            analyze.numeric(row["persistence_transition_detected"]) for row in rows
        ),
        "ewma_transition_detections": sum(
            analyze.numeric(row["ewma_transition_detected"]) for row in rows
        ),
        "transition_wins": wins,
        "transition_win_fraction": wins / runs,
        "median_lead_ms": analyze.median(leads),
        "median_stable_high_active_fraction": median(
            rows, "kalman_transition_stable_high_active_fraction"
        ),
        "median_recovery_active_fraction": median(
            rows, "kalman_transition_recovery_active_fraction"
        ),
        "median_forecast_rmse": median(rows, "kalman_forecast_rmse"),
        "median_persistence_rmse": median(rows, "persistence_forecast_rmse"),
        "median_ewma_rmse": median(rows, "ewma_forecast_rmse"),
        "median_kalman_f1": median(rows, "kalman_f1"),
        "median_persistence_f1": median(rows, "persistence_f1"),
        "median_ewma_f1": median(rows, "ewma_f1"),
        "median_nis_mean": median(rows, "nis_mean"),
        "median_coverage_95": median(rows, "coverage_95"),
        "median_innovation_lag1": median(rows, "innovation_lag1"),
    }
    gates = model["validation_gates"]
    criteria = {
        "data_integrity": all(integrity.values()),
        "minimum_runs": runs >= minimum_runs,
        "endpoint_timing_feasible": (
            summary["feasible_transition_fraction"]
            >= gates["feasible_transition_fraction_minimum"]
        ),
        "detection_rate": (
            summary["kalman_detection_rate"] >= gates["detection_rate_minimum"]
        ),
        "transition_advantage": (
            summary["transition_win_fraction"]
            >= gates["transition_win_fraction_minimum"]
        ),
        "useful_lead": (
            summary["median_lead_ms"]
            > gates["median_lead_ms_strictly_greater_than"]
        ),
        "stable_false_activity": (
            summary["median_stable_high_active_fraction"]
            <= gates["median_stable_high_active_fraction_maximum"]
        ),
        "recovery_false_activity": (
            summary["median_recovery_active_fraction"]
            <= gates["median_recovery_active_fraction_maximum"]
        ),
        "forecast_rmse": (
            summary["median_forecast_rmse"]
            < min(summary["median_persistence_rmse"], summary["median_ewma_rmse"])
        ),
        "innovation_nis": (
            gates["innovation_nis_interval"][0]
            <= summary["median_nis_mean"]
            <= gates["innovation_nis_interval"][1]
        ),
        "innovation_coverage": (
            gates["innovation_coverage_95_interval"][0]
            <= summary["median_coverage_95"]
            <= gates["innovation_coverage_95_interval"][1]
        ),
        "innovation_autocorrelation": (
            abs(summary["median_innovation_lag1"])
            <= gates["absolute_innovation_lag1_maximum"]
        ),
    }
    passed = all(criteria.values())
    output = Path(args.output_dir)
    analyze.write_csv(output / "validation_runs.csv", rows)
    report = {
        "status": "validated" if passed else "not_validated",
        "model_file": str(model_path),
        "variant": args.variant,
        "parameters_refit": False,
        "thresholds_refit": False,
        "data_integrity": integrity,
        "summary": summary,
        "criteria": criteria,
        "next_gate": "implementation_equivalence" if passed else "stop_predictor",
    }
    analyze.write_json(output / "validation.json", report)
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
    result.add_argument("--episode-quiet-ms", type=float, default=1000.0)
    return result


if __name__ == "__main__":
    print(json.dumps(validate(parser().parse_args()), indent=2, sort_keys=True))
