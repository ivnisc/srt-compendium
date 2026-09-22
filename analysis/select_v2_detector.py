#!/usr/bin/env python3
"""Selecciona un detector causal V2 con partición por corrida."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import analyze
from calibrate_controller import RttTrendKalman


@dataclass
class Run:
    run_id: str
    elapsed_ms: list[float]
    rtt_ms: list[float]
    sndbuf_ms: list[float]
    transition_ms: float
    recovery_ms: float
    rtt_baseline: float
    rtt_sigma: float
    sndbuf_baseline: float
    sndbuf_sigma: float
    baseline_begin_ms: float
    baseline_end_ms: float


def load_run(path: Path, baseline_begin_ms: float,
             baseline_end_ms: float) -> Run:
    rows = analyze.read_rows(path / "tx_stats.csv")
    elapsed = [analyze.numeric(row.get("elapsed_ms")) for row in rows]
    rtt = [analyze.numeric(row.get("msRTT")) for row in rows]
    sndbuf = [analyze.numeric(row.get("msSndBuf")) for row in rows]
    indexes = [index for index, value in enumerate(elapsed)
               if baseline_begin_ms <= value < baseline_end_ms and rtt[index] > 0.0]
    if len(indexes) < 20:
        raise ValueError(f"Baseline insuficiente en {path}")
    baseline_rtt = [rtt[index] for index in indexes]
    baseline_buffer = [sndbuf[index] for index in indexes]
    transition, recovery = analyze.phase_bounds_ms(path)
    return Run(
        path.name.removeprefix("run_"), elapsed, rtt, sndbuf,
        transition, recovery,
        statistics.median(baseline_rtt),
        max(statistics.stdev(baseline_rtt), 0.01),
        statistics.median(baseline_buffer),
        max(statistics.stdev(baseline_buffer), 1.0),
        baseline_begin_ms, baseline_end_ms,
    )


def standardized(run: Run) -> tuple[list[float], list[float]]:
    rtt = [(value - run.rtt_baseline) / run.rtt_sigma for value in run.rtt_ms]
    sndbuf = [
        (value - run.sndbuf_baseline) / run.sndbuf_sigma
        for value in run.sndbuf_ms
    ]
    return rtt, sndbuf


def weighted(rtt: Sequence[float], sndbuf: Sequence[float],
             rtt_weight: float) -> list[float]:
    return [rtt_weight * left + (1.0 - rtt_weight) * right
            for left, right in zip(rtt, sndbuf)]


def ewma(values: Sequence[float], elapsed_ms: Sequence[float], alpha: float,
         begin_ms: float) -> list[float]:
    estimate = 0.0
    initialized = False
    result = [0.0] * len(values)
    for index, (elapsed, value) in enumerate(zip(elapsed_ms, values)):
        if elapsed < begin_ms:
            continue
        if not initialized:
            estimate = value
            initialized = True
        estimate = alpha * value + (1.0 - alpha) * estimate
        result[index] = estimate
    return result


def ewma_score(run: Run, parameters: dict[str, float]) -> list[float]:
    rtt, sndbuf = standardized(run)
    return weighted(
        ewma(rtt, run.elapsed_ms, parameters["alpha"], run.baseline_begin_ms),
        ewma(sndbuf, run.elapsed_ms, parameters["alpha"], run.baseline_begin_ms),
        parameters["rtt_weight"],
    )


def cusum_score(run: Run, parameters: dict[str, float]) -> list[float]:
    rtt, sndbuf = standardized(run)
    observations = weighted(rtt, sndbuf, parameters["rtt_weight"])
    result = []
    state = 0.0
    for elapsed, value in zip(run.elapsed_ms, observations):
        if elapsed < run.baseline_end_ms:
            result.append(0.0)
            continue
        state = max(0.0, state + value - parameters["drift"])
        result.append(state)
    return result


def kalman_trace(values: Sequence[float], elapsed_ms: Sequence[float],
                 q: float, r: float, horizon_ms: float,
                 begin_ms: float) -> list[float]:
    model = RttTrendKalman(q, r)
    result = [0.0] * len(values)
    previous = begin_ms
    for index, (elapsed, value) in enumerate(zip(elapsed_ms, values)):
        if elapsed < begin_ms:
            continue
        dt_s = max((elapsed - previous) / 1000.0, 1e-6)
        result[index] = model.update(value, dt_s, horizon_ms / 1000.0)
        previous = elapsed
    return result


def kalman_score(run: Run, parameters: dict[str, float]) -> list[float]:
    rtt, sndbuf = standardized(run)
    horizon = parameters["horizon_ms"]
    return weighted(
        kalman_trace(rtt, run.elapsed_ms, parameters["q_rtt"], 1.0, horizon,
                     run.baseline_begin_ms),
        kalman_trace(sndbuf, run.elapsed_ms, parameters["q_sndbuf"], 1.0, horizon,
                     run.baseline_begin_ms),
        parameters["rtt_weight"],
    )


METHODS: dict[str, Callable[[Run, dict[str, float]], list[float]]] = {
    "ewma": ewma_score,
    "cusum": cusum_score,
    "kalman": kalman_score,
}


def rolling_minimum(values: Sequence[float], count: int) -> list[float]:
    if count <= 1:
        return list(values)
    return [min(values[index - count + 1:index + 1])
            for index in range(count - 1, len(values))]


def zero_false_threshold(runs: Sequence[Run], score_by_run: Sequence[list[float]],
                         calibration_end_ms: float,
                         trigger_samples: int,
                         safety_sigmas: float) -> float:
    per_run_bounds = []
    for run, scores in zip(runs, score_by_run):
        stable = [score for elapsed, score in zip(run.elapsed_ms, scores)
                  if calibration_end_ms <= elapsed < run.transition_ms]
        bounds = rolling_minimum(stable, trigger_samples)
        per_run_bounds.append(max(bounds, default=0.0))
    maximum = max(per_run_bounds, default=0.0)
    dispersion = (statistics.stdev(per_run_bounds)
                  if len(per_run_bounds) > 1 else 0.0)
    return math.nextafter(maximum + safety_sigmas * dispersion, math.inf)


def state_machine(run: Run, scores: Sequence[float], threshold: float,
                  calibration_end_ms: float, trigger_samples: int,
                  release_samples: int, release_ratio: float,
                  confirmation_ratio: float, confirmation_timeout_ms: float,
                  retry_cooldown_ms: float) -> dict:
    active = False
    candidate = False
    confirmed = False
    completed = False
    trigger_count = 0
    release_count = 0
    activations = []
    candidates = []
    states = []
    active_since = -math.inf
    candidate_since = -math.inf
    retry_after = -math.inf
    confirmation_elapsed_ms = math.nan
    restore_elapsed_ms = math.nan
    release_threshold = threshold * release_ratio
    for elapsed, score in zip(run.elapsed_ms, scores):
        if elapsed < calibration_end_ms:
            states.append(False)
            continue
        if not active and not candidate and not completed:
            trigger_count = trigger_count + 1 if (
                elapsed >= retry_after and score >= threshold) else 0
            if trigger_count >= trigger_samples:
                candidate = True
                candidate_since = elapsed
                candidates.append(elapsed)
                trigger_count = 0
        elif candidate:
            if score >= threshold * confirmation_ratio:
                candidate = False
                active = True
                confirmed = True
                active_since = elapsed
                activations.append(elapsed)
                if not math.isfinite(confirmation_elapsed_ms):
                    confirmation_elapsed_ms = elapsed
            elif elapsed - candidate_since >= confirmation_timeout_ms:
                candidate = False
                retry_after = elapsed + retry_cooldown_ms
        else:
            release_count = release_count + 1 if (
                active and confirmed and score <= release_threshold) else 0
            if active and release_count >= release_samples:
                active = False
                completed = True
                restore_elapsed_ms = elapsed
                release_count = 0
        states.append(active)
    pre = [value for value in activations if value < run.transition_ms]
    pre_candidates = [value for value in candidates if value < run.transition_ms]
    during = [value for value in activations
              if run.transition_ms <= value < run.recovery_ms]
    recovery_indexes = [index for index, value in enumerate(run.elapsed_ms)
                        if value >= run.recovery_ms + 1000.0]
    recovery_active = analyze.mean([
        float(states[index]) for index in recovery_indexes
    ])
    low_indexes = [index for index, value in enumerate(run.elapsed_ms)
                   if run.transition_ms <= value < run.recovery_ms]
    return {
        "run": run.run_id,
        "false_activations": len(pre),
        "false_candidates": len(pre_candidates),
        "detected": bool(during) or (
            bool(pre) and any(states[index] for index in low_indexes)
        ),
        "detection_delay_ms": (
            during[0] - run.transition_ms if during else math.inf
        ),
        "low_active_fraction": analyze.mean([
            float(states[index]) for index in low_indexes
        ]),
        "recovery_active_fraction": recovery_active,
        "activations": len(activations),
        "candidates": len(candidates),
        "candidate_elapsed_ms": candidates[0] if candidates else math.nan,
        "activation_elapsed_ms": activations[0] if activations else math.nan,
        "confirmation_elapsed_ms": confirmation_elapsed_ms,
        "restore_elapsed_ms": restore_elapsed_ms,
    }


def aggregate(rows: Sequence[dict]) -> dict:
    delays = [row["detection_delay_ms"] for row in rows
              if math.isfinite(row["detection_delay_ms"])]
    return {
        "runs": len(rows),
        "detections": sum(row["detected"] for row in rows),
        "runs_with_false_activation": sum(
            row["false_activations"] > 0 for row in rows),
        "runs_with_false_candidate": sum(
            row["false_candidates"] > 0 for row in rows),
        "median_detection_delay_ms": analyze.median(delays),
        "p90_detection_delay_ms": analyze.percentile(delays, 0.90),
        "maximum_detection_delay_ms": max(delays, default=math.inf),
        "median_low_active_fraction": analyze.median([
            row["low_active_fraction"] for row in rows
        ]),
        "median_recovery_active_fraction": analyze.median([
            row["recovery_active_fraction"] for row in rows
        ]),
        "median_activations": analyze.median([
            row["activations"] for row in rows
        ]),
        "median_candidates": analyze.median([
            row["candidates"] for row in rows
        ]),
    }


def parameter_grid(method: str) -> list[dict[str, float]]:
    weights = (0.5, 0.75, 1.0)
    if method == "ewma":
        return [dict(alpha=alpha, rtt_weight=weight)
                for alpha, weight in itertools.product(
                    (0.05, 0.1, 0.2, 0.35, 0.5), weights)]
    if method == "cusum":
        return [dict(drift=drift, rtt_weight=weight)
                for drift, weight in itertools.product(
                    (0.25, 0.5, 1.0, 2.0), weights)]
    return [dict(q_rtt=q_rtt, q_sndbuf=q_sndbuf, horizon_ms=horizon,
                 rtt_weight=weight)
            for q_rtt, q_sndbuf, horizon, weight in itertools.product(
                (0.01, 0.1, 1.0, 10.0), (0.01, 0.1, 1.0, 10.0),
                (0.0, 250.0, 500.0), weights)]


def evaluate(runs: Sequence[Run], method: str, parameters: dict[str, float],
             threshold: float, args: argparse.Namespace) -> tuple[dict, list[dict]]:
    rows = [state_machine(
        run, METHODS[method](run, parameters), threshold,
        args.baseline_end_ms, args.trigger_samples,
        args.release_samples, args.release_ratio,
        args.confirmation_ratio, args.confirmation_timeout_ms,
        args.retry_cooldown_ms,
    ) for run in runs]
    return aggregate(rows), rows


def select_family(training: Sequence[Run], method: str,
                  args: argparse.Namespace) -> dict:
    candidates = []
    for parameters in parameter_grid(method):
        scores = [METHODS[method](run, parameters) for run in training]
        threshold = zero_false_threshold(
            training, scores, args.baseline_end_ms, args.trigger_samples,
            args.threshold_safety_sigmas)
        summary, _ = evaluate(training, method, parameters, threshold, args)
        eligible = (
            summary["detections"] == len(training)
            and summary["runs_with_false_activation"] == 0
        )
        candidates.append({
            "method": method,
            "parameters": parameters,
            "threshold": threshold,
            "summary": summary,
            "eligible": eligible,
        })
    eligible = [item for item in candidates if item["eligible"]]
    if not eligible:
        raise RuntimeError(f"Ningún candidato {method} es elegible")
    return min(eligible, key=lambda item: (
        item["summary"]["median_detection_delay_ms"],
        item["summary"]["p90_detection_delay_ms"],
        item["summary"]["median_recovery_active_fraction"],
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results/v2")
    parser.add_argument("--variant", default="vanilla")
    parser.add_argument("--output-dir", default="results/v2/detector_selection")
    parser.add_argument(
        "--controller-env", default="experiments/controller_v2_pilot.env")
    parser.add_argument("--training-ids", default="1-12")
    parser.add_argument("--validation-ids", default="13-20")
    parser.add_argument("--baseline-begin-ms", type=float, default=3000.0)
    parser.add_argument("--baseline-end-ms", type=float, default=10000.0)
    parser.add_argument("--trigger-samples", type=int, default=3)
    parser.add_argument("--release-samples", type=int, default=10)
    parser.add_argument("--release-ratio", type=float, default=0.5)
    parser.add_argument("--threshold-safety-sigmas", type=float, default=1.0)
    parser.add_argument("--confirmation-ratio", type=float, default=5.0)
    parser.add_argument("--confirmation-timeout-ms", type=float, default=5000.0)
    parser.add_argument("--retry-cooldown-ms", type=float, default=1000.0)
    parser.add_argument(
        "--maximum-recovery-active-fraction", type=float, default=0.10)
    args = parser.parse_args()

    def parse_ids(specification: str) -> set[str]:
        result = set()
        for part in specification.split(","):
            if "-" in part:
                first, last = (int(value) for value in part.split("-", 1))
                result.update(str(value) for value in range(first, last + 1))
            else:
                result.add(part.strip())
        return result

    root = Path(args.results) / "dynamic" / args.variant
    paths = sorted(root.glob("run_*"), key=lambda path: int(
        path.name.removeprefix("run_")))
    runs = [load_run(path, args.baseline_begin_ms, args.baseline_end_ms)
            for path in paths if (path / "run_metadata.csv").exists()]
    training_ids = parse_ids(args.training_ids)
    validation_ids = parse_ids(args.validation_ids)
    training = [run for run in runs if run.run_id in training_ids]
    validation = [run for run in runs if run.run_id in validation_ids]
    if len(training) != len(training_ids) or len(validation) != len(validation_ids):
        raise SystemExit("La partición solicita corridas ausentes")

    families = []
    run_rows = []
    for method in METHODS:
        candidate = select_family(training, method, args)
        validation_summary, validation_rows = evaluate(
            validation, method, candidate["parameters"],
            candidate["threshold"], args)
        candidate["validation"] = validation_summary
        families.append(candidate)
        for row in validation_rows:
            run_rows.append({"method": method, **row})

    cross_validation = []
    for method in METHODS:
        fold_rows = []
        fold_models = []
        for fold in range(5):
            test = [run for index, run in enumerate(runs) if index % 5 == fold]
            train = [run for index, run in enumerate(runs) if index % 5 != fold]
            candidate = select_family(train, method, args)
            _, test_rows = evaluate(
                test, method, candidate["parameters"],
                candidate["threshold"], args)
            fold_rows.extend(test_rows)
            fold_models.append({
                "fold": fold + 1,
                "parameters": candidate["parameters"],
                "threshold": candidate["threshold"],
                "test_ids": [run.run_id for run in test],
            })
        cross_validation.append({
            "method": method,
            "summary": aggregate(fold_rows),
            "folds": fold_models,
        })

    cv_eligible = [item for item in cross_validation if (
        item["summary"]["detections"] == len(runs)
        and item["summary"]["runs_with_false_activation"] == 0
        and item["summary"]["median_recovery_active_fraction"]
            <= args.maximum_recovery_active_fraction
    )]
    selected_family = min(cv_eligible, key=lambda item: (
        item["summary"]["median_detection_delay_ms"],
        item["summary"]["p90_detection_delay_ms"],
        item["summary"]["median_recovery_active_fraction"],
    )) if cv_eligible else None
    selected = None
    if selected_family:
        selected = select_family(runs, selected_family["method"], args)
        selected["cross_validation"] = selected_family["summary"]
    report = {
        "status": "selected_for_pilot" if selected else "no_detector_validated",
        "role": "development_only",
        "training_ids": sorted(training_ids, key=int),
        "validation_ids": sorted(validation_ids, key=int),
        "baseline_window_ms": [args.baseline_begin_ms, args.baseline_end_ms],
        "trigger_samples": args.trigger_samples,
        "release_samples": args.release_samples,
        "release_ratio": args.release_ratio,
        "threshold_safety_sigmas": args.threshold_safety_sigmas,
        "confirmation_ratio": args.confirmation_ratio,
        "confirmation_timeout_ms": args.confirmation_timeout_ms,
        "retry_cooldown_ms": args.retry_cooldown_ms,
        "actuate_on_confirmation": True,
        "maximum_recovery_active_fraction": (
            args.maximum_recovery_active_fraction),
        "families": families,
        "five_fold_cross_validation": cross_validation,
        "selected": selected,
        "kalman_formulation": {
            "state": ["rtt", "rtt_trend", "sndbuf", "sndbuf_trend"],
            "observation": ["msRTT", "msSndBuf"],
            "F": "blockdiag([[1,dt],[0,1]], [[1,dt],[0,1]])",
            "H": "[[1,0,0,0],[0,0,1,0]]",
            "Q": "blockdiag(q_rtt*G(dt), q_sndbuf*G(dt))",
            "G": "[[dt^3/3,dt^2/2],[dt^2/2,dt]]",
            "R": "diag(sigma_rtt^2, sigma_sndbuf^2); standardized to identity",
            "note": "Los dos bloques son independientes; la política combina sus predicciones normalizadas.",
        },
    }
    output = Path(args.output_dir)
    analyze.write_json(output / "selection.json", report)
    analyze.write_csv(output / "validation_runs.csv", run_rows)
    if selected:
        if selected["method"] != "kalman":
            raise SystemExit("El candidato seleccionado no es Kalman")
        selection_sha256 = hashlib.sha256(
            (output / "selection.json").read_bytes()).hexdigest()
        parameters = selected["parameters"]
        controller = Path(args.controller_env)
        controller.parent.mkdir(parents=True, exist_ok=True)
        controller.write_text(
            "# generado por select_v2_detector.py; sólo para piloto de desarrollo\n"
            f"V2_SELECTION_SHA256={selection_sha256}\n"
            "ACTIVE_OHEAD=10\n"
            "V2_ACTUATE_ON_CONFIRMATION=1\n"
            f"V2_BASELINE_BEGIN_MS={args.baseline_begin_ms:g}\n"
            f"V2_BASELINE_END_MS={args.baseline_end_ms:g}\n"
            f"V2_KALMAN_Q_RTT={parameters['q_rtt']:.17g}\n"
            f"V2_KALMAN_Q_SNDBUF={parameters['q_sndbuf']:.17g}\n"
            f"V2_HORIZON_MS={parameters['horizon_ms']:.17g}\n"
            f"V2_RTT_WEIGHT={parameters['rtt_weight']:.17g}\n"
            f"V2_TRIGGER_SCORE={selected['threshold']:.17g}\n"
            f"V2_TRIGGER_SAMPLES={args.trigger_samples}\n"
            f"V2_RELEASE_SCORE_RATIO={args.release_ratio:.17g}\n"
            f"V2_RELEASE_SAMPLES={args.release_samples}\n"
            f"V2_CONFIRMATION_RATIO={args.confirmation_ratio:.17g}\n"
            f"V2_CONFIRMATION_TIMEOUT_MS={args.confirmation_timeout_ms:g}\n"
            f"V2_RETRY_COOLDOWN_MS={args.retry_cooldown_ms:g}\n"
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
