#!/usr/bin/env python3
"""Identifica y compara K0, K1 y K2 usando sólo corridas de desarrollo."""

from __future__ import annotations

import argparse
import bisect
import itertools
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import analyze
import calibrate_controller as pilot
import describe_model_data as description


MODELS = ("K0", "K1", "K2", "K3")


@dataclass
class Trace:
    level: list[float]
    trend: list[float]
    innovation: list[float]
    innovation_variance: list[float]


def model_parameters(model: str) -> tuple[str, ...]:
    if model == "K0":
        return ("q_level", "r")
    if model == "K1":
        return ("q_trend", "r")
    if model == "K2":
        return ("q_level", "q_trend", "r")
    if model == "K3":
        return ("q_trend", "r", "lambda")
    raise ValueError(f"Modelo desconocido: {model}")


def run_filter(series: pilot.Series, model: str, parameters: dict[str, float],
               p0_scale: float = 1_000_000.0) -> Trace:
    q_level = parameters.get("q_level", 0.0)
    q_trend = parameters.get("q_trend", 0.0)
    measurement_noise = parameters["r"]
    level = series.rtt_ms[0]
    trend = 0.0
    p00 = p0_scale
    p01 = 0.0
    p10 = 0.0
    p11 = p0_scale if model != "K0" else 0.0
    levels = [level]
    trends = [trend]
    innovations = [0.0]
    innovation_variances = [p00 + measurement_noise]

    for index in range(1, len(series.rtt_ms)):
        dt = max(
            (series.elapsed_ms[index] - series.elapsed_ms[index - 1]) / 1000.0,
            1e-6,
        )
        if model == "K0":
            predicted_level = level
            predicted_p00 = p00 + q_level * dt
            innovation = series.rtt_ms[index] - predicted_level
            variance = max(predicted_p00 + measurement_noise, 1e-12)
            gain = predicted_p00 / variance
            level = predicted_level + gain * innovation
            p00 = max((1.0 - gain) * predicted_p00, 1e-12)
        else:
            if model == "K3":
                damping = parameters["lambda"]
                phi = math.exp(-damping * dt)
                integrated = (1.0 - phi) / damping
                predicted_level = level + integrated * trend
                predicted_trend = phi * trend
                q11 = q_trend * (1.0 - phi ** 2) / (2.0 * damping)
                q01 = q_trend * (1.0 - phi) ** 2 / (2.0 * damping ** 2)
                q00 = q_level * dt + q_trend / damping ** 2 * (
                    dt - 2.0 * (1.0 - phi) / damping
                    + (1.0 - phi ** 2) / (2.0 * damping)
                )
                q00 = max(q00, 0.0)
                pp00 = (
                    p00 + integrated * (p01 + p10)
                    + integrated ** 2 * p11 + q00
                )
                pp01 = phi * (p01 + integrated * p11) + q01
                pp10 = phi * (p10 + integrated * p11) + q01
                pp11 = phi ** 2 * p11 + q11
            else:
                predicted_level = level + dt * trend
                predicted_trend = trend
                q00 = q_level * dt + q_trend * dt ** 3 / 3.0
                q01 = q_trend * dt ** 2 / 2.0
                q11 = q_trend * dt
                pp00 = p00 + dt * (p01 + p10) + dt ** 2 * p11 + q00
                pp01 = p01 + dt * p11 + q01
                pp10 = p10 + dt * p11 + q01
                pp11 = p11 + q11
            innovation = series.rtt_ms[index] - predicted_level
            variance = max(pp00 + measurement_noise, 1e-12)
            gain0 = pp00 / variance
            gain1 = pp10 / variance
            level = predicted_level + gain0 * innovation
            trend = predicted_trend + gain1 * innovation
            p00 = max((1.0 - gain0) * pp00, 1e-12)
            updated_p01 = (1.0 - gain0) * pp01
            updated_p10 = pp10 - gain1 * pp00
            p01 = p10 = 0.5 * (updated_p01 + updated_p10)
            p11 = max(pp11 - gain1 * pp01, 1e-12)
        levels.append(level)
        trends.append(trend)
        innovations.append(innovation)
        innovation_variances.append(variance)
    return Trace(levels, trends, innovations, innovation_variances)


def likelihood(series: Sequence[pilot.Series], model: str,
               parameters: dict[str, float], burn_in: int) -> float:
    total = 0.0
    observations = 0
    for item in series:
        trace = run_filter(item, model, parameters)
        for innovation, variance in zip(
                trace.innovation[burn_in:], trace.innovation_variance[burn_in:]):
            total += 0.5 * (
                math.log(2.0 * math.pi * variance)
                + innovation * innovation / variance
            )
            observations += 1
    return total / observations if observations else math.inf


def parameter_dict(names: Sequence[str], exponents: Sequence[float]) -> dict[str, float]:
    return {name: 10.0 ** exponent for name, exponent in zip(names, exponents)}


def fit_parameters(series: Sequence[pilot.Series], model: str,
                   burn_in: int) -> tuple[dict[str, float], float]:
    names = model_parameters(model)
    coarse = {
        "q_level": (-6, -4, -2, 0, 2, 4, 6),
        "q_trend": (-6, -4, -2, 0, 2, 4, 6),
        "r": (-6, -4, -2, 0, 2, 4),
        "lambda": (-1, -0.5, 0, 0.5, 1),
    }
    candidates = itertools.product(*(coarse[name] for name in names))
    best_exponents: tuple[float, ...] | None = None
    best_score = math.inf
    for exponents in candidates:
        parameters = parameter_dict(names, exponents)
        score = likelihood(series, model, parameters, burn_in)
        if score < best_score:
            best_score = score
            best_exponents = tuple(exponents)
    assert best_exponents is not None

    refined = [
        tuple(sorted(set(center + offset for offset in (-1.0, -0.5, 0.0, 0.5, 1.0))))
        for center in best_exponents
    ]
    for exponents in itertools.product(*refined):
        parameters = parameter_dict(names, exponents)
        score = likelihood(series, model, parameters, burn_in)
        if score < best_score:
            best_score = score
            best_exponents = tuple(exponents)
    for step in (0.25, 0.125):
        for position in range(len(names)):
            for offset in (-2, -1, 0, 1, 2):
                exponents = list(best_exponents)
                exponents[position] += offset * step
                parameters = parameter_dict(names, exponents)
                score = likelihood(series, model, parameters, burn_in)
                if score < best_score:
                    best_score = score
                    best_exponents = tuple(exponents)
    return parameter_dict(names, best_exponents), best_score


def forecast_scores(trace: Trace, horizon_ms: float, model: str,
                    parameters: dict[str, float]) -> list[float]:
    horizon_s = horizon_ms / 1000.0
    integrated = horizon_s
    if model == "K3":
        damping = parameters["lambda"]
        integrated = (1.0 - math.exp(-damping * horizon_s)) / damping
    return [level + integrated * trend
            for level, trend in zip(trace.level, trace.trend)]


def forecast_metrics(item: pilot.Series, scores: Sequence[float],
                     horizon_ms: float, burn_in: int) -> dict[str, float]:
    errors = []
    for index in range(burn_in, len(scores)):
        target = bisect.bisect_left(
            item.elapsed_ms, item.elapsed_ms[index] + horizon_ms, index + 1
        )
        if target >= len(scores):
            break
        errors.append(scores[index] - item.rtt_ms[target])
    if not errors:
        return {"rmse": 0.0, "mae": 0.0, "bias": 0.0, "samples": 0.0}
    return {
        "rmse": math.sqrt(statistics.mean(value * value for value in errors)),
        "mae": statistics.mean(abs(value) for value in errors),
        "bias": statistics.mean(errors),
        "samples": float(len(errors)),
    }


def standardized_innovation_metrics(trace: Trace, burn_in: int) -> dict[str, float]:
    standardized = [
        innovation / math.sqrt(variance)
        for innovation, variance in zip(
            trace.innovation[burn_in:], trace.innovation_variance[burn_in:]
        )
    ]
    return {
        "nis_mean": statistics.mean(value * value for value in standardized),
        "coverage_95": statistics.mean(abs(value) <= 1.96 for value in standardized),
        "innovation_lag1": analyze.autocorrelation(standardized, 1),
    }


def ewma_scores(item: pilot.Series, alpha: float) -> list[float]:
    estimate = item.rtt_ms[0]
    result = []
    for measurement in item.rtt_ms:
        estimate = alpha * measurement + (1.0 - alpha) * estimate
        result.append(estimate)
    return result


def fit_ewma_alpha(series: Sequence[pilot.Series], horizon_ms: float,
                   burn_in: int) -> float:
    candidates = (0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 0.8)
    scored = []
    for alpha in candidates:
        squared = []
        for item in series:
            metrics = forecast_metrics(
                item, ewma_scores(item, alpha), horizon_ms, burn_in
            )
            squared.append(metrics["rmse"] ** 2 * metrics["samples"])
        samples = sum(
            forecast_metrics(item, ewma_scores(item, alpha), horizon_ms, burn_in)[
                "samples"
            ]
            for item in series
        )
        scored.append((sum(squared) / max(samples, 1.0), alpha))
    return min(scored)[1]


def endpoint_values(item: pilot.Series, endpoint: str) -> list[float]:
    return item.belated if endpoint == "belated" else item.dropped


def future_episode_labels(item: pilot.Series, endpoint: str, quiet_ms: float,
                          minimum_lead_ms: float,
                          maximum_lead_ms: float) -> tuple[list[bool], list[bool]]:
    events = endpoint_values(item, endpoint)
    onsets = description.episode_onsets(events, item.elapsed_ms, quiet_ms)
    labels = []
    valid = []
    for index, elapsed in enumerate(item.elapsed_ms):
        valid.append(events[index] <= 0.0)
        labels.append(any(
            minimum_lead_ms <= item.elapsed_ms[onset] - elapsed <= maximum_lead_ms
            for onset in onsets
        ))
    return labels, valid


def candidate_thresholds(scores: Sequence[float]) -> list[float]:
    return sorted(set(
        analyze.percentile(scores, percentile / 100.0)
        for percentile in range(10, 100)
    ))


def classification(scores: Sequence[float], labels: Sequence[bool],
                   valid: Sequence[bool], threshold: float) -> dict[str, float]:
    pairs = [(score >= threshold, label) for score, label, use
             in zip(scores, labels, valid) if use]
    tp = sum(predicted and label for predicted, label in pairs)
    fp = sum(predicted and not label for predicted, label in pairs)
    fn = sum(not predicted and label for predicted, label in pairs)
    tn = sum(not predicted and not label for predicted, label in pairs)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "f1": (2.0 * precision * recall / (precision + recall)
               if precision + recall else 0.0),
        "precision": precision,
        "recall": recall,
        "false_positive_rate": fp / (fp + tn) if fp + tn else 0.0,
        "positive_samples": float(sum(label for _, label in pairs)),
    }


def tune_threshold(by_run_scores: Sequence[Sequence[float]],
                   series: Sequence[pilot.Series], endpoint: str,
                   quiet_ms: float, minimum_lead_ms: float,
                   maximum_lead_ms: float) -> tuple[float, dict[str, float]]:
    scores = []
    labels = []
    valid = []
    for item, run_scores in zip(series, by_run_scores):
        run_labels, run_valid = future_episode_labels(
            item, endpoint, quiet_ms, minimum_lead_ms, maximum_lead_ms
        )
        scores.extend(run_scores)
        labels.extend(run_labels)
        valid.extend(run_valid)
    usable_scores = [score for score, use in zip(scores, valid) if use]
    if not usable_scores or not any(label and use for label, use in zip(labels, valid)):
        raise ValueError(f"No hay episodios utilizables para {endpoint}")
    choices = []
    for threshold in candidate_thresholds(usable_scores):
        metrics = classification(scores, labels, valid, threshold)
        choices.append((
            metrics["f1"], metrics["recall"], -metrics["false_positive_rate"],
            threshold, metrics,
        ))
    _, _, _, threshold, metrics = max(choices)
    return threshold, metrics


def test_classification(item: pilot.Series, scores: Sequence[float], endpoint: str,
                        threshold: float, quiet_ms: float,
                        minimum_lead_ms: float,
                        maximum_lead_ms: float) -> dict[str, float]:
    labels, valid = future_episode_labels(
        item, endpoint, quiet_ms, minimum_lead_ms, maximum_lead_ms
    )
    return classification(scores, labels, valid, threshold)


def episode_coverage(item: pilot.Series, scores: Sequence[float], endpoint: str,
                     threshold: float, quiet_ms: float, minimum_lead_ms: float,
                     maximum_lead_ms: float,
                     trigger_samples: int) -> dict[str, float]:
    events = endpoint_values(item, endpoint)
    onsets = description.episode_onsets(events, item.elapsed_ms, quiet_ms)
    consecutive = 0
    active = []
    for score in scores:
        consecutive = consecutive + 1 if score >= threshold else 0
        active.append(consecutive >= trigger_samples)
    leads = []
    for onset in onsets:
        eligible = [
            index for index, state in enumerate(active[:onset])
            if state and minimum_lead_ms
            <= item.elapsed_ms[onset] - item.elapsed_ms[index]
            <= maximum_lead_ms
        ]
        if eligible:
            leads.append(item.elapsed_ms[onset] - item.elapsed_ms[max(eligible)])
    return {
        "episodes": float(len(onsets)),
        "covered": float(len(leads)),
        "coverage": len(leads) / len(onsets) if onsets else 0.0,
        "median_lead_ms": analyze.median(leads),
    }


def active_states(scores: Sequence[float], threshold: float,
                  trigger_samples: int) -> list[bool]:
    consecutive = 0
    result = []
    for score in scores:
        consecutive = consecutive + 1 if score >= threshold else 0
        result.append(consecutive >= trigger_samples)
    return result


def transition_metrics(item: pilot.Series, scores: Sequence[float], endpoint: str,
                       threshold: float, transition_ms: float,
                       recovery_ms: float, minimum_lead_ms: float,
                       trigger_samples: int,
                       transition_guard_ms: float) -> dict[str, float]:
    states = active_states(scores, threshold, trigger_samples)
    events = endpoint_values(item, endpoint)
    post_transition = [
        index for index, value in enumerate(events)
        if item.elapsed_ms[index] >= transition_ms and value > 0.0
    ]
    first_event = post_transition[0] if post_transition else None
    activations = [
        index for index, state in enumerate(states)
        if state and (index == 0 or not states[index - 1])
    ]
    eligible = []
    if first_event is not None:
        eligible = [
            index for index in activations
            if transition_ms <= item.elapsed_ms[index] < item.elapsed_ms[first_event]
            and item.elapsed_ms[first_event] - item.elapsed_ms[index]
            >= minimum_lead_ms
        ]
    detection = min(eligible) if eligible else None
    high_states = [
        state for elapsed, state in zip(item.elapsed_ms, states)
        if elapsed < transition_ms - transition_guard_ms
    ]
    recovery_states = [
        state for elapsed, state in zip(item.elapsed_ms, states)
        if elapsed >= recovery_ms + transition_guard_ms
    ]
    return {
        "first_event_observed": float(first_event is not None),
        "first_event_feasible": float(
            first_event is not None
            and item.elapsed_ms[first_event] - transition_ms >= minimum_lead_ms
        ),
        "detected": float(detection is not None),
        "lead_ms": (
            item.elapsed_ms[first_event] - item.elapsed_ms[detection]
            if detection is not None and first_event is not None else 0.0
        ),
        "activation_delay_ms": (
            item.elapsed_ms[detection] - transition_ms
            if detection is not None else 0.0
        ),
        "stable_high_active_fraction": (
            statistics.mean(high_states) if high_states else 0.0
        ),
        "recovery_active_fraction": (
            statistics.mean(recovery_states) if recovery_states else 0.0
        ),
    }


def tune_operational_threshold(
        by_run_scores: Sequence[Sequence[float]],
        series: Sequence[pilot.Series], endpoint: str,
        phases: dict[str, tuple[float, float]], minimum_lead_ms: float,
        trigger_samples: int, transition_guard_ms: float,
        maximum_false_active_fraction: float) -> tuple[float, dict[str, float]]:
    flat_scores = [score for run_scores in by_run_scores for score in run_scores]
    choices = []
    for threshold in candidate_thresholds(flat_scores):
        metrics = []
        for item, scores in zip(series, by_run_scores):
            transition_ms, recovery_ms = phases[item.run_id]
            metrics.append(transition_metrics(
                item, scores, endpoint, threshold, transition_ms, recovery_ms,
                minimum_lead_ms, trigger_samples, transition_guard_ms,
            ))
        feasible = sum(row["first_event_feasible"] for row in metrics)
        detected = sum(row["detected"] for row in metrics)
        leads = [row["lead_ms"] for row in metrics if row["detected"]]
        high_false = analyze.median([
            row["stable_high_active_fraction"] for row in metrics
        ])
        recovery_false = analyze.median([
            row["recovery_active_fraction"] for row in metrics
        ])
        respects_false_limit = (
            high_false <= maximum_false_active_fraction
            and recovery_false <= maximum_false_active_fraction
        )
        summary = {
            "feasible_runs": feasible,
            "detected_runs": detected,
            "detection_rate": detected / feasible if feasible else 0.0,
            "median_lead_ms": analyze.median(leads),
            "stable_high_active_fraction": high_false,
            "recovery_active_fraction": recovery_false,
            "respects_false_limit": respects_false_limit,
        }
        choices.append((
            respects_false_limit, summary["detection_rate"],
            summary["median_lead_ms"], -(high_false + recovery_false),
            threshold, summary,
        ))
    _, _, _, _, threshold, summary = max(choices)
    return threshold, summary


def aggregate_metric(rows: Sequence[dict], field: str) -> float:
    return analyze.median([analyze.numeric(row[field]) for row in rows])


def aggregate_transition_metrics(
        series: Sequence[pilot.Series], by_run_scores: Sequence[Sequence[float]],
        endpoint: str, threshold: float,
        phases: dict[str, tuple[float, float]], args: argparse.Namespace) -> dict:
    rows = []
    for item, scores in zip(series, by_run_scores):
        transition_ms, recovery_ms = phases[item.run_id]
        rows.append(transition_metrics(
            item, scores, endpoint, threshold, transition_ms, recovery_ms,
            args.minimum_lead_ms, args.trigger_samples,
            args.transition_guard_ms,
        ))
    leads = [row["lead_ms"] for row in rows if row["detected"]]
    return {
        "feasible_runs": sum(row["first_event_feasible"] for row in rows),
        "detected_runs": sum(row["detected"] for row in rows),
        "median_lead_ms": analyze.median(leads),
        "median_stable_high_active_fraction": analyze.median([
            row["stable_high_active_fraction"] for row in rows
        ]),
        "median_recovery_active_fraction": analyze.median([
            row["recovery_active_fraction"] for row in rows
        ]),
    }


def cross_validate(args: argparse.Namespace) -> dict:
    paths = pilot.find_runs(Path(args.results), args.variant)
    if len(paths) < args.minimum_runs:
        raise SystemExit(
            f"Se requieren {args.minimum_runs} corridas de desarrollo; hay {len(paths)}"
        )
    series = [pilot.load_series(path, args.warmup_ms) for path in paths]
    phases = {}
    for path, item in zip(paths, series):
        metadata = analyze.read_key_value(path / "run_metadata.csv")
        transition_ms, recovery_ms = analyze.phase_bounds_ms(path, metadata)
        phases[item.run_id] = (transition_ms, recovery_ms)
    horizons = [float(value) for value in args.horizons_ms.split(",")]
    fold_rows = []
    fitted_parameters = []

    for held_index, test in enumerate(series):
        training = [item for index, item in enumerate(series) if index != held_index]
        for model in MODELS:
            parameters, train_nll = fit_parameters(training, model, args.burn_in_samples)
            fitted_parameters.append({
                "fold": test.run_id,
                "model": model,
                "training_nll": train_nll,
                "q_level": "",
                "q_trend": "",
                "r": "",
                "lambda": "",
                **parameters,
            })
            training_traces = [run_filter(item, model, parameters) for item in training]
            test_trace = run_filter(test, model, parameters)
            innovation = standardized_innovation_metrics(
                test_trace, args.burn_in_samples
            )
            for horizon in horizons:
                train_kalman = [forecast_scores(trace, horizon, model, parameters)
                                for trace in training_traces]
                test_kalman = forecast_scores(
                    test_trace, horizon, model, parameters
                )
                alpha = fit_ewma_alpha(training, horizon, args.burn_in_samples)
                train_baselines = {
                    "persistence": [item.rtt_ms for item in training],
                    "ewma": [ewma_scores(item, alpha) for item in training],
                }
                test_baselines = {
                    "persistence": test.rtt_ms,
                    "ewma": ewma_scores(test, alpha),
                }
                kalman_forecast = forecast_metrics(
                    test, test_kalman, horizon, args.burn_in_samples
                )
                baseline_forecasts = {
                    name: forecast_metrics(
                        test, scores, horizon, args.burn_in_samples
                    ) for name, scores in test_baselines.items()
                }
                for endpoint in ("belated", "drop"):
                    method_scores = {"kalman": (train_kalman, test_kalman)}
                    method_scores.update({
                        name: (train_baselines[name], test_baselines[name])
                        for name in train_baselines
                    })
                    evaluated = {}
                    for method, (train_scores, test_scores) in method_scores.items():
                        threshold, operational_training = tune_operational_threshold(
                            train_scores, training, endpoint, phases,
                            args.minimum_lead_ms, args.trigger_samples,
                            args.transition_guard_ms,
                            args.maximum_false_active_fraction,
                        )
                        metrics = test_classification(
                            test, test_scores, endpoint, threshold,
                            args.episode_quiet_ms, args.minimum_lead_ms, horizon,
                        )
                        coverage = episode_coverage(
                            test, test_scores, endpoint, threshold,
                            args.episode_quiet_ms, args.minimum_lead_ms, horizon,
                            args.trigger_samples,
                        )
                        transition_ms, recovery_ms = phases[test.run_id]
                        transition = transition_metrics(
                            test, test_scores, endpoint, threshold,
                            transition_ms, recovery_ms, args.minimum_lead_ms,
                            args.trigger_samples, args.transition_guard_ms,
                        )
                        evaluated[method] = {
                            "threshold": threshold,
                            "training_detection_rate": operational_training[
                                "detection_rate"
                            ],
                            **metrics,
                            **{f"episode_{key}": value
                               for key, value in coverage.items()},
                            **{f"transition_{key}": value
                               for key, value in transition.items()},
                        }
                    row = {
                        "fold": test.run_id,
                        "model": model,
                        "horizon_ms": horizon,
                        "endpoint": endpoint,
                        "training_nll": train_nll,
                        "ewma_alpha": alpha,
                        "forecast_rmse": kalman_forecast["rmse"],
                        "forecast_mae": kalman_forecast["mae"],
                        "persistence_rmse": baseline_forecasts["persistence"]["rmse"],
                        "ewma_rmse": baseline_forecasts["ewma"]["rmse"],
                        **innovation,
                    }
                    for method, metrics in evaluated.items():
                        for key, value in metrics.items():
                            row[f"{method}_{key}"] = value
                    row["kalman_f1_delta_best_baseline"] = (
                        evaluated["kalman"]["f1"]
                        - max(evaluated["persistence"]["f1"],
                              evaluated["ewma"]["f1"])
                    )
                    baseline_detected = max(
                        evaluated["persistence"]["transition_detected"],
                        evaluated["ewma"]["transition_detected"],
                    )
                    baseline_lead = max(
                        evaluated["persistence"]["transition_lead_ms"],
                        evaluated["ewma"]["transition_lead_ms"],
                    )
                    row["kalman_transition_advantage_ms"] = (
                        evaluated["kalman"]["transition_lead_ms"] - baseline_lead
                        if evaluated["kalman"]["transition_detected"]
                        and baseline_detected else
                        evaluated["kalman"]["transition_lead_ms"]
                        if evaluated["kalman"]["transition_detected"] else
                        -baseline_lead if baseline_detected else 0.0
                    )
                    row["kalman_transition_win"] = float(
                        evaluated["kalman"]["transition_detected"]
                        and (
                            not baseline_detected
                            or evaluated["kalman"]["transition_lead_ms"] > baseline_lead
                        )
                    )
                    fold_rows.append(row)

    groups: dict[tuple[str, float, str], list[dict]] = {}
    for row in fold_rows:
        key = (row["model"], row["horizon_ms"], row["endpoint"])
        groups.setdefault(key, []).append(row)
    summary_rows = []
    for (model, horizon, endpoint), rows in sorted(groups.items()):
        summary_rows.append({
            "model": model,
            "horizon_ms": horizon,
            "endpoint": endpoint,
            "folds": len(rows),
            "median_forecast_rmse": aggregate_metric(rows, "forecast_rmse"),
            "median_persistence_rmse": aggregate_metric(rows, "persistence_rmse"),
            "median_ewma_rmse": aggregate_metric(rows, "ewma_rmse"),
            "median_nis_mean": aggregate_metric(rows, "nis_mean"),
            "median_coverage_95": aggregate_metric(rows, "coverage_95"),
            "median_innovation_lag1": aggregate_metric(rows, "innovation_lag1"),
            "median_kalman_f1": aggregate_metric(rows, "kalman_f1"),
            "median_persistence_f1": aggregate_metric(rows, "persistence_f1"),
            "median_ewma_f1": aggregate_metric(rows, "ewma_f1"),
            "median_f1_delta_best_baseline": aggregate_metric(
                rows, "kalman_f1_delta_best_baseline"
            ),
            "fold_wins": sum(
                analyze.numeric(row["kalman_f1_delta_best_baseline"]) > 0.0
                for row in rows
            ),
            "total_episodes": sum(
                analyze.numeric(row["kalman_episode_episodes"]) for row in rows
            ),
            "covered_episodes": sum(
                analyze.numeric(row["kalman_episode_covered"]) for row in rows
            ),
            "median_episode_lead_ms": aggregate_metric(
                rows, "kalman_episode_median_lead_ms"
            ),
            "feasible_transition_runs": sum(
                analyze.numeric(row["kalman_transition_first_event_feasible"])
                for row in rows
            ),
            "kalman_transition_detections": sum(
                analyze.numeric(row["kalman_transition_detected"]) for row in rows
            ),
            "persistence_transition_detections": sum(
                analyze.numeric(row["persistence_transition_detected"])
                for row in rows
            ),
            "ewma_transition_detections": sum(
                analyze.numeric(row["ewma_transition_detected"]) for row in rows
            ),
            "transition_wins": sum(
                analyze.numeric(row["kalman_transition_win"]) for row in rows
            ),
            "median_transition_advantage_ms": aggregate_metric(
                rows, "kalman_transition_advantage_ms"
            ),
            "median_transition_lead_ms": aggregate_metric(
                rows, "kalman_transition_lead_ms"
            ),
            "median_stable_high_active_fraction": aggregate_metric(
                rows, "kalman_transition_stable_high_active_fraction"
            ),
            "median_recovery_active_fraction": aggregate_metric(
                rows, "kalman_transition_recovery_active_fraction"
            ),
        })

    def event_key(row: dict) -> tuple:
        return (
            row["transition_wins"], row["kalman_transition_detections"],
            row["median_transition_advantage_ms"],
            row["median_f1_delta_best_baseline"],
        )

    def assess(row: dict) -> dict:
        result = dict(row)
        result["forecast_beats_both_baselines"] = (
            result["median_forecast_rmse"]
            < min(result["median_persistence_rmse"], result["median_ewma_rmse"])
        )
        result["innovation_diagnostics_acceptable"] = (
            0.5 <= result["median_nis_mean"] <= 2.0
            and 0.90 <= result["median_coverage_95"] <= 0.99
            and abs(result["median_innovation_lag1"]) <= 0.20
        )
        result["event_improvement_consistent"] = (
            result["feasible_transition_runs"] >= math.ceil(0.8 * len(series))
            and result["kalman_transition_detections"]
            >= math.ceil(0.8 * result["feasible_transition_runs"])
            and result["transition_wins"] >= math.ceil(len(series) / 2)
            and result["median_stable_high_active_fraction"]
            <= args.maximum_false_active_fraction
            and result["median_recovery_active_fraction"]
            <= args.maximum_false_active_fraction
        )
        return result

    best_event = assess(max(
        summary_rows,
        key=event_key,
    ))
    forecast_candidates = [
        assess(row) for row in summary_rows
        if row["median_forecast_rmse"]
        < min(row["median_persistence_rmse"], row["median_ewma_rmse"])
    ]
    best_forecast_qualified = (
        max(forecast_candidates, key=event_key) if forecast_candidates else None
    )
    eligible_candidates = [
        row for row in forecast_candidates
        if row["innovation_diagnostics_acceptable"]
        and row["event_improvement_consistent"]
    ]
    candidate = (
        max(eligible_candidates, key=event_key) if eligible_candidates
        else best_forecast_qualified or best_event
    )
    eligible = bool(eligible_candidates)

    final_parameters, final_nll = fit_parameters(
        series, candidate["model"], args.burn_in_samples
    )
    final_traces = [
        run_filter(item, candidate["model"], final_parameters) for item in series
    ]
    final_scores = [
        forecast_scores(
            trace, candidate["horizon_ms"], candidate["model"], final_parameters
        ) for trace in final_traces
    ]
    final_threshold, final_training = tune_operational_threshold(
        final_scores, series, candidate["endpoint"], phases,
        args.minimum_lead_ms, args.trigger_samples, args.transition_guard_ms,
        args.maximum_false_active_fraction,
    )
    final_transition = aggregate_transition_metrics(
        series, final_scores, candidate["endpoint"], final_threshold, phases, args
    )
    final_ewma_alpha = fit_ewma_alpha(
        series, candidate["horizon_ms"], args.burn_in_samples
    )
    baseline_scores = {
        "persistence": [item.rtt_ms for item in series],
        "ewma": [ewma_scores(item, final_ewma_alpha) for item in series],
    }
    frozen_baselines = {}
    for name, scores in baseline_scores.items():
        threshold, training_metrics = tune_operational_threshold(
            scores, series, candidate["endpoint"], phases,
            args.minimum_lead_ms, args.trigger_samples,
            args.transition_guard_ms, args.maximum_false_active_fraction,
        )
        frozen_baselines[name] = {
            "threshold_ms": threshold,
            "alpha": final_ewma_alpha if name == "ewma" else None,
            "development_operational_metrics": training_metrics,
        }
    selected_folds = [
        row for row in fitted_parameters if row["model"] == candidate["model"]
    ]
    parameter_stability = {}
    for name in model_parameters(candidate["model"]):
        values = [analyze.numeric(row[name]) for row in selected_folds]
        parameter_stability[name] = {
            "minimum": min(values),
            "median": analyze.median(values),
            "maximum": max(values),
            "max_to_min_ratio": max(values) / min(values),
        }
    p0_sensitivity = []
    for scale in (100.0, 10_000.0, 1_000_000.0, 100_000_000.0):
        traces = [
            run_filter(item, candidate["model"], final_parameters, scale)
            for item in series
        ]
        scores = [
            forecast_scores(
                trace, candidate["horizon_ms"], candidate["model"],
                final_parameters,
            ) for trace in traces
        ]
        forecast = [
            forecast_metrics(
                item, run_scores, candidate["horizon_ms"], args.burn_in_samples
            ) for item, run_scores in zip(series, scores)
        ]
        innovations = [
            standardized_innovation_metrics(trace, args.burn_in_samples)
            for trace in traces
        ]
        p0_sensitivity.append({
            "p0_scale": scale,
            "median_forecast_rmse": analyze.median([
                row["rmse"] for row in forecast
            ]),
            "median_nis_mean": analyze.median([
                row["nis_mean"] for row in innovations
            ]),
            "median_innovation_lag1": analyze.median([
                row["innovation_lag1"] for row in innovations
            ]),
        })

    provisional_model = {
        "status": "development_candidate_not_validated",
        "model": candidate["model"],
        "state": (
            ["rtt_level_ms"] if candidate["model"] == "K0"
            else ["rtt_level_ms", "rtt_trend_ms_s"]
        ),
        "observation": ["msRTT"],
        "endpoint": candidate["endpoint"],
        "horizon_ms": candidate["horizon_ms"],
        "parameters": final_parameters,
        "p0_diagonal": [1_000_000.0, 1_000_000.0],
        "burn_in_samples": args.burn_in_samples,
        "trigger_threshold_ms": final_threshold,
        "trigger_samples": args.trigger_samples,
        "minimum_useful_lead_ms": args.minimum_lead_ms,
        "maximum_false_active_fraction": args.maximum_false_active_fraction,
        "training_operational_metrics": final_training,
        "development_transition_metrics": final_transition,
        "development_nll_per_observation": final_nll,
        "baselines": frozen_baselines,
        "minimum_fresh_validation_runs": args.minimum_fresh_validation_runs,
        "validation_gates": {
            "feasible_transition_fraction_minimum": 0.8,
            "detection_rate_minimum": 0.8,
            "transition_win_fraction_minimum": 0.6,
            "median_lead_ms_strictly_greater_than": args.minimum_lead_ms,
            "median_stable_high_active_fraction_maximum": (
                args.maximum_false_active_fraction
            ),
            "median_recovery_active_fraction_maximum": (
                args.maximum_false_active_fraction
            ),
            "forecast_rmse_below_persistence_and_ewma": True,
            "innovation_nis_interval": [0.5, 2.0],
            "innovation_coverage_95_interval": [0.90, 0.99],
            "absolute_innovation_lag1_maximum": 0.20,
        },
    }

    output = Path(args.output_dir)
    analyze.write_csv(output / "cross_validation_folds.csv", fold_rows)
    analyze.write_csv(output / "cross_validation_summary.csv", summary_rows)
    analyze.write_csv(output / "fitted_parameters_by_fold.csv", fitted_parameters)
    analyze.write_json(output / "provisional_model.json", provisional_model)
    report = {
        "status": (
            "development_candidate_identified" if eligible
            else "no_eligible_development_candidate"
        ),
        "role": "development_only",
        "runs": [item.run_id for item in series],
        "models": list(MODELS),
        "horizons_ms": horizons,
        "minimum_useful_lead_ms": args.minimum_lead_ms,
        "episode_quiet_ms": args.episode_quiet_ms,
        "candidate": candidate,
        "best_event_score_without_model_gates": best_event,
        "best_forecast_qualified": best_forecast_qualified,
        "provisional_model": provisional_model,
        "parameter_stability_by_fold": parameter_stability,
        "p0_sensitivity": p0_sensitivity,
        "warning": (
            "La selección explora datos ya observados. Aun si resulta elegible, "
            "debe congelarse y validarse con corridas nuevas."
        ),
        "outputs": {
            "folds": str(output / "cross_validation_folds.csv"),
            "summary": str(output / "cross_validation_summary.csv"),
            "parameters": str(output / "fitted_parameters_by_fold.csv"),
            "provisional_model": str(output / "provisional_model.json"),
        },
    }
    analyze.write_json(output / "model_selection.json", report)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--results", default="results")
    result.add_argument("--variant", default="fixed25")
    result.add_argument("--output-dir", default="results/model_development")
    result.add_argument("--minimum-runs", type=int, default=5)
    result.add_argument("--warmup-ms", type=float, default=3000.0)
    result.add_argument("--burn-in-samples", type=int, default=20)
    result.add_argument("--horizons-ms", default="500,1000,2000")
    result.add_argument("--minimum-lead-ms", type=float, default=300.1)
    result.add_argument("--episode-quiet-ms", type=float, default=1000.0)
    result.add_argument("--trigger-samples", type=int, default=3)
    result.add_argument("--transition-guard-ms", type=float, default=500.0)
    result.add_argument("--maximum-false-active-fraction", type=float, default=0.05)
    result.add_argument("--minimum-fresh-validation-runs", type=int, default=10)
    return result


if __name__ == "__main__":
    print(json.dumps(cross_validate(parser().parse_args()), indent=2, sort_keys=True))
