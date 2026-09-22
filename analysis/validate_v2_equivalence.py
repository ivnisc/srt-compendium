#!/usr/bin/env python3
"""Comprueba equivalencia entre el replay Python y el controlador C++ V2."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import analyze
import select_v2_detector as detector


def decision_time(rows: list[dict[str, str]], decision: str) -> float:
    values = [analyze.numeric(row.get("elapsed_ms")) for row in rows
              if row.get("decision") == decision]
    return values[0] if values else math.nan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument(
        "--selection", default="results/v2/detector_selection/selection.json")
    parser.add_argument("--output", default="results/v2/equivalence.json")
    parser.add_argument("--score-tolerance", type=float, default=1e-8)
    parser.add_argument("--time-tolerance-ms", type=float, default=1.0)
    args = parser.parse_args()

    selection = json.loads(Path(args.selection).read_text())
    selected = selection.get("selected")
    if not selected or selected.get("method") != "kalman":
        raise SystemExit("La selección no contiene un Kalman congelado")
    run_dir = Path(args.run_dir)
    begin, end = selection["baseline_window_ms"]
    run = detector.load_run(run_dir, float(begin), float(end))
    expected_scores = detector.kalman_score(run, selected["parameters"])
    replay_iterations = 100
    replay_started = time.perf_counter_ns()
    for _ in range(replay_iterations):
        detector.kalman_score(run, selected["parameters"])
    replay_elapsed_ns = time.perf_counter_ns() - replay_started
    python_replay_us_per_sample = (
        replay_elapsed_ns / 1000.0 / replay_iterations / len(run.elapsed_ms)
    )
    rows = analyze.read_rows(run_dir / "tx_stats.csv")
    observed_scores = [analyze.numeric(row.get("v2_score")) for row in rows]
    cpp_eval = [analyze.numeric(row.get("v2_eval_us")) for row in rows
                if analyze.numeric(row.get("elapsed_ms")) >= float(end)]
    indexes = [index for index, elapsed in enumerate(run.elapsed_ms)
               if elapsed >= float(end)]
    errors = [abs(expected_scores[index] - observed_scores[index])
              for index in indexes]

    expected = detector.state_machine(
        run, expected_scores, float(selected["threshold"]), float(end),
        int(selection["trigger_samples"]), int(selection["release_samples"]),
        float(selection["release_ratio"]),
        float(selection["confirmation_ratio"]),
        float(selection["confirmation_timeout_ms"]),
        float(selection["retry_cooldown_ms"]),
    )
    if selection.get("actuate_on_confirmation", False):
        event_pairs = {
            "candidate": (
                expected["candidate_elapsed_ms"],
                decision_time(rows, "v2_candidate")),
            "activation": (
                expected["activation_elapsed_ms"],
                decision_time(rows, "v2_activate")),
            "restore": (
                expected["restore_elapsed_ms"],
                decision_time(rows, "v2_restore")),
        }
    else:
        event_pairs = {
            "activation": (
                expected["activation_elapsed_ms"],
                decision_time(rows, "v2_activate")),
            "confirmation": (
                expected["confirmation_elapsed_ms"],
                decision_time(rows, "v2_confirm")),
            "restore": (
                expected["restore_elapsed_ms"],
                decision_time(rows, "v2_restore")),
        }

    def same_time(pair: tuple[float, float]) -> bool:
        left, right = pair
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        return abs(left - right) <= args.time_tolerance_ms

    tx_meta = analyze.read_key_value(run_dir / "tx_metadata.csv")
    baseline_errors = {
        "rtt_baseline": abs(
            analyze.numeric(tx_meta.get("v2_rtt_baseline")) - run.rtt_baseline),
        "rtt_sigma": abs(
            analyze.numeric(tx_meta.get("v2_rtt_sigma")) - run.rtt_sigma),
        "sndbuf_baseline": abs(
            analyze.numeric(tx_meta.get("v2_sndbuf_baseline")) -
            run.sndbuf_baseline),
        "sndbuf_sigma": abs(
            analyze.numeric(tx_meta.get("v2_sndbuf_sigma")) - run.sndbuf_sigma),
    }
    criteria = {
        "score_equivalent": max(errors, default=math.inf) <= args.score_tolerance,
        "baseline_equivalent": max(baseline_errors.values()) <= args.score_tolerance,
        "decision_times_equivalent": all(same_time(pair)
                                         for pair in event_pairs.values()),
        "final_ohead_nominal": tx_meta.get("final_ohead") == "25",
        "cpp_runtime_budget": analyze.percentile(cpp_eval, 0.99) < 1000.0,
    }
    report = {
        "status": "equivalent" if all(criteria.values()) else "not_equivalent",
        "run_dir": str(run_dir),
        "criteria": criteria,
        "maximum_absolute_score_error": max(errors, default=math.inf),
        "baseline_absolute_errors": baseline_errors,
        "decision_times_ms": {
            name: {"python": pair[0], "cpp": pair[1]}
            for name, pair in event_pairs.items()
        },
        "score_tolerance": args.score_tolerance,
        "time_tolerance_ms": args.time_tolerance_ms,
        "runtime": {
            "cpp_eval_p99_us": analyze.percentile(cpp_eval, 0.99),
            "cpp_eval_max_us": max(cpp_eval, default=math.inf),
            "python_offline_replay_us_per_sample": python_replay_us_per_sample,
            "sample_period_us": 100000.0,
            "warning": (
                "La medición Python es un replay offline y no una comparación "
                "de scheduling en el camino de datos."
            ),
        },
    }
    analyze.write_json(Path(args.output), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["status"] != "equivalent":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
