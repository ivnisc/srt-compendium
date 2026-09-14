#!/usr/bin/env python3
"""Reproduce el piloto K1 previo al desarrollo formal del modelo de estado."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import analyze


@dataclass
class Series:
    run_id: str
    elapsed_ms: list[float]
    rtt_ms: list[float]
    belated: list[float]
    dropped: list[float]


class RttTrendKalman:
    """Equivalente numérico del filtro usado por el emisor C++."""

    def __init__(self, process_noise: float, measurement_noise: float) -> None:
        self.q = process_noise
        self.r = measurement_noise
        self.initialized = False
        self.x0 = 0.0
        self.x1 = 0.0
        self.p00 = 100.0
        self.p01 = 0.0
        self.p10 = 0.0
        self.p11 = 100.0

    def update(self, measurement: float, dt_s: float, horizon_s: float) -> float:
        if not self.initialized:
            self.x0 = measurement
            self.initialized = True
            return self.x0
        dt_s = max(dt_s, 1e-6)
        x0_pred = self.x0 + dt_s * self.x1
        x1_pred = self.x1
        q00 = self.q * dt_s ** 3 / 3.0
        q01 = self.q * dt_s ** 2 / 2.0
        q11 = self.q * dt_s
        pp00 = self.p00 + dt_s * (self.p10 + self.p01) + dt_s ** 2 * self.p11 + q00
        pp01 = self.p01 + dt_s * self.p11 + q01
        pp10 = self.p10 + dt_s * self.p11 + q01
        pp11 = self.p11 + q11
        innovation = measurement - x0_pred
        innovation_variance = pp00 + self.r
        k0 = pp00 / innovation_variance
        k1 = pp10 / innovation_variance
        self.x0 = x0_pred + k0 * innovation
        self.x1 = x1_pred + k1 * innovation
        self.p00 = (1.0 - k0) * pp00
        self.p01 = (1.0 - k0) * pp01
        self.p10 = pp10 - k1 * pp00
        self.p11 = pp11 - k1 * pp01
        return self.x0 + max(horizon_s, 0.0) * self.x1


def run_sort_key(path: Path) -> tuple[int, str]:
    run_id = path.name.removeprefix("run_")
    return (int(run_id), run_id) if run_id.isdigit() else (10**9, run_id)


def find_runs(results: Path, variant: str) -> list[Path]:
    paths = list((results / "dynamic" / variant).glob("run_*"))
    return sorted((path for path in paths if (path / "run_metadata.csv").exists()),
                  key=run_sort_key)


def load_series(run_dir: Path, warmup_ms: float = 3000.0) -> Series:
    tx = analyze.read_rows(run_dir / "tx_stats.csv")
    rx = analyze.read_rows(run_dir / "rx_stats.csv")
    aligned = analyze.nearest_align(tx, rx)
    aligned = [(left, right) for left, right in aligned
               if analyze.numeric(left.get("msRTT")) > 0.0
               and analyze.numeric(left.get("elapsed_ms")) >= warmup_ms]
    if len(aligned) < 5:
        raise ValueError(f"Muestras insuficientes en {run_dir}")
    return Series(
        run_id=run_dir.name.removeprefix("run_"),
        elapsed_ms=[analyze.numeric(left.get("elapsed_ms")) for left, _ in aligned],
        rtt_ms=[analyze.numeric(left.get("msRTT")) for left, _ in aligned],
        belated=[analyze.numeric(right.get("pktRcvBelated")) for _, right in aligned],
        dropped=[analyze.numeric(right.get("pktRcvDrop")) for _, right in aligned],
    )


def sample_interval_ms(series: Sequence[Series]) -> float:
    intervals = []
    for item in series:
        intervals.extend(item.elapsed_ms[index] - item.elapsed_ms[index - 1]
                         for index in range(1, len(item.elapsed_ms)))
    return max(1.0, analyze.median(intervals))


def estimate_measurement_noise(series: Sequence[Series], phase_high_s: float) -> float:
    stable = []
    for item in series:
        stable.extend(value for elapsed, value in zip(item.elapsed_ms, item.rtt_ms)
                      if 3000.0 <= elapsed < max(4000.0, phase_high_s * 1000.0 - 1000.0))
    return max(statistics.variance(stable) if len(stable) > 1 else 1.0, 0.01)


def kalman_scores(item: Series, q: float, r: float, horizon_ms: float) -> list[float]:
    model = RttTrendKalman(q, r)
    scores = []
    previous = item.elapsed_ms[0]
    for elapsed, measurement in zip(item.elapsed_ms, item.rtt_ms):
        dt_s = max((elapsed - previous) / 1000.0, 1e-6)
        scores.append(model.update(measurement, dt_s, horizon_ms / 1000.0))
        previous = elapsed
    return scores


def ewma_scores(item: Series, alpha: float = 0.2) -> list[float]:
    estimate = item.rtt_ms[0]
    output = []
    for measurement in item.rtt_ms:
        estimate = alpha * measurement + (1.0 - alpha) * estimate
        output.append(estimate)
    return output


def future_labels(events: Sequence[float], action_steps: int,
                  maximum_steps: int) -> list[bool]:
    labels = []
    for index in range(len(events)):
        begin = min(len(events), index + action_steps)
        end = min(len(events), index + maximum_steps + 1)
        labels.append(any(value > 0.0 for value in events[begin:end]))
    return labels


def classification(scores: Sequence[float], labels: Sequence[bool],
                   threshold: float) -> dict[str, float]:
    predicted = [score >= threshold for score in scores]
    tp = sum(prediction and label for prediction, label in zip(predicted, labels))
    fp = sum(prediction and not label for prediction, label in zip(predicted, labels))
    fn = sum(not prediction and label for prediction, label in zip(predicted, labels))
    tn = sum(not prediction and not label for prediction, label in zip(predicted, labels))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positive_rate": fp / (fp + tn) if fp + tn else 0.0,
        "positive_samples": float(sum(labels)),
    }


def candidate_thresholds(scores: Sequence[float]) -> list[float]:
    probabilities = [0.50 + index * 0.01 for index in range(50)]
    return sorted(set(analyze.percentile(scores, value) for value in probabilities))


def tune_threshold(scores: Sequence[float], labels: Sequence[bool]) -> tuple[float, dict[str, float]]:
    if not any(labels):
        raise ValueError("No hay eventos futuros en el conjunto de calibración")
    choices = []
    for threshold in candidate_thresholds(scores):
        metrics = classification(scores, labels, threshold)
        choices.append((metrics["f1"], metrics["recall"], -metrics["false_positive_rate"],
                        threshold, metrics))
    _, _, _, threshold, metrics = max(choices)
    return threshold, metrics


def concatenate(values: Sequence[Sequence[float]]) -> list[float]:
    return [value for sequence in values for value in sequence]


def concatenate_bool(values: Sequence[Sequence[bool]]) -> list[bool]:
    return [value for sequence in values for value in sequence]


def event_leads(series: Sequence[Series], scores: Sequence[Sequence[float]],
                threshold: float, endpoint: str, trigger_samples: int,
                max_lead_ms: float) -> dict[str, float]:
    leads = []
    event_count = 0
    for item, run_scores in zip(series, scores):
        events = item.belated if endpoint == "belated" else item.dropped
        alerts = []
        consecutive = 0
        above = False
        for index, score in enumerate(run_scores):
            consecutive = consecutive + 1 if score >= threshold else 0
            new_above = consecutive >= trigger_samples
            if new_above and not above:
                alerts.append(index)
            above = new_above
        onsets = [index for index, value in enumerate(events)
                  if value > 0.0 and (index == 0 or events[index - 1] <= 0.0)]
        event_count += len(onsets)
        for onset in onsets:
            eligible = [alert for alert in alerts if alert < onset and
                        item.elapsed_ms[onset] - item.elapsed_ms[alert] <= max_lead_ms]
            if eligible:
                leads.append(item.elapsed_ms[onset] - item.elapsed_ms[max(eligible)])
    return {
        "event_onsets": float(event_count),
        "matched_event_onsets": float(len(leads)),
        "onset_recall": len(leads) / event_count if event_count else 0.0,
        "median_lead_ms": analyze.median(leads),
    }


def measure_action_latency(results: Path, tolerance: float = 0.03) -> dict:
    observations = []
    for variant in ("fixed10", "fixed40"):
        for run_dir in find_runs(results, variant):
            metadata = analyze.read_key_value(run_dir / "run_metadata.csv")
            tx_metadata = analyze.read_key_value(run_dir / "tx_metadata.csv")
            input_mbps = analyze.numeric(metadata.get("input_rate_bps")) / 1_000_000.0
            nominal = int(analyze.numeric(tx_metadata.get("nominal_ohead"), 25.0))
            active = int(analyze.numeric(tx_metadata.get("active_ohead"), 25.0))
            rows = analyze.read_rows(run_dir / "tx_stats.csv")
            for index, row in enumerate(rows):
                decision = row.get("decision")
                if decision not in ("fixed_activate", "fixed_restore"):
                    continue
                target = active if decision == "fixed_activate" else nominal
                expected = input_mbps * (1.0 + target / 100.0)
                started = analyze.numeric(row.get("elapsed_ms"))
                for candidate in rows[index + 1:]:
                    elapsed = analyze.numeric(candidate.get("elapsed_ms"))
                    if elapsed - started > 3000.0:
                        break
                    actual = analyze.numeric(candidate.get("mbpsMaxBW"))
                    if expected > 0.0 and abs(actual - expected) / expected <= tolerance:
                        observations.append({
                            "run": str(run_dir), "decision": decision,
                            "target_ohead": target, "expected_mbps": expected,
                            "observed_mbps": actual, "latency_ms": elapsed - started,
                        })
                        break
    latencies = [item["latency_ms"] for item in observations]
    return {
        "status": "measured" if latencies else "not_observed",
        "median_ms": analyze.median(latencies),
        "p95_ms": analyze.percentile(latencies, 0.95),
        "observations": observations,
    }


def choose_endpoint(calibration: Sequence[Series], validation: Sequence[Series]) -> str:
    for endpoint in ("belated", "dropped"):
        calibration_events = sum(sum(value > 0.0 for value in getattr(item, endpoint))
                                 for item in calibration)
        validation_events = sum(sum(value > 0.0 for value in getattr(item, endpoint))
                                for item in validation)
        if calibration_events and validation_events:
            return endpoint
    raise ValueError("Belated y drop carecen de eventos en calibración o validación")


def build_labels(series: Sequence[Series], endpoint: str, action_steps: int,
                 max_steps: int) -> list[list[bool]]:
    return [future_labels(item.belated if endpoint == "belated" else item.dropped,
                          action_steps, max_steps) for item in series]


def parse_ids(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def write_controller_env(path: Path, values: dict) -> None:
    content = (
        "# generado exclusivamente desde datos de calibración\n"
        f"ACTIVE_OHEAD={values['active_ohead']}\n"
        f"TRIGGER_RTT_MS={values['trigger_rtt_ms']:.6f}\n"
        f"RELEASE_RTT_MS={values['release_rtt_ms']:.6f}\n"
        f"HORIZON_MS={values['horizon_ms']:.0f}\n"
        f"KALMAN_Q={values['kalman_q']:.6f}\n"
        f"KALMAN_R={values['kalman_r']:.6f}\n"
        f"TRIGGER_SAMPLES={values['trigger_samples']}\n"
        f"RELEASE_SAMPLES={values['release_samples']}\n"
        f"COOLDOWN_MS={values['cooldown_ms']:.0f}\n"
        f"WARMUP_MS={values['warmup_ms']}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def command_calibrate(args: argparse.Namespace) -> None:
    results = Path(args.results)
    with Path(args.actuator).open() as stream:
        actuator = json.load(stream)
    report = {
        "role": "pilot_k1",
        "warning": (
            "Las corridas usadas ya fueron inspeccionadas. Este comando no "
            "valida el modelo ni genera una política ejecutable."
        ),
        "actuator": actuator,
    }
    if actuator.get("status") != "viable":
        report["status"] = "actuator_not_viable"
        analyze.write_json(Path(args.output), report)
        raise SystemExit("El actuador no superó la compuerta de viabilidad")

    paths = find_runs(results, "fixed25")
    if len(paths) < 3:
        raise SystemExit("Se requieren al menos tres corridas dynamic/fixed25")
    if args.calibration_ids or args.validation_ids:
        calibration_ids = parse_ids(args.calibration_ids)
        validation_ids = parse_ids(args.validation_ids)
        if not calibration_ids or not validation_ids or calibration_ids & validation_ids:
            raise SystemExit("Los IDs de calibración y validación deben ser disjuntos y no vacíos")
        calibration_paths = [path for path in paths
                             if path.name.removeprefix("run_") in calibration_ids]
        validation_paths = [path for path in paths
                            if path.name.removeprefix("run_") in validation_ids]
    else:
        split = max(1, math.ceil(len(paths) * 0.6))
        calibration_paths, validation_paths = paths[:split], paths[split:]
    if not calibration_paths or not validation_paths:
        raise SystemExit("No fue posible construir conjuntos separados")

    calibration = [load_series(path) for path in calibration_paths]
    validation = [load_series(path) for path in validation_paths]
    endpoint = choose_endpoint(calibration, validation)
    interval_ms = sample_interval_ms(calibration + validation)
    action = measure_action_latency(results)
    report["action_latency"] = action
    if action["status"] != "measured":
        report["status"] = "action_latency_not_measured"
        analyze.write_json(Path(args.output), report)
        raise SystemExit("No se observó la latencia acción-efecto en mbpsMaxBW")

    horizon_ms = max(interval_ms, math.ceil((action["p95_ms"] + interval_ms) / interval_ms) * interval_ms)
    action_steps = max(1, math.ceil(action["p95_ms"] / interval_ms))
    max_steps = max(action_steps + 1, math.ceil(args.max_lead_ms / interval_ms))
    calibration_labels_by_run = build_labels(calibration, endpoint, action_steps, max_steps)
    validation_labels_by_run = build_labels(validation, endpoint, action_steps, max_steps)
    calibration_labels = concatenate_bool(calibration_labels_by_run)
    validation_labels = concatenate_bool(validation_labels_by_run)
    measurement_noise = estimate_measurement_noise(calibration, args.phase_high_s)

    q_values = [float(value) for value in args.q_grid.split(",")]
    kalman_candidates = []
    for q_value in q_values:
        by_run = [kalman_scores(item, q_value, measurement_noise, horizon_ms)
                  for item in calibration]
        flat = concatenate(by_run)
        threshold, metrics = tune_threshold(flat, calibration_labels)
        kalman_candidates.append((metrics["f1"], metrics["recall"], q_value,
                                  threshold, metrics))
    _, _, selected_q, trigger, calibration_metrics = max(kalman_candidates)

    calibration_kalman = [kalman_scores(item, selected_q, measurement_noise, horizon_ms)
                          for item in calibration]
    validation_kalman = [kalman_scores(item, selected_q, measurement_noise, horizon_ms)
                         for item in validation]
    baseline_scores = {
        "persistence": ([item.rtt_ms for item in calibration],
                        [item.rtt_ms for item in validation]),
        "ewma": ([ewma_scores(item) for item in calibration],
                 [ewma_scores(item) for item in validation]),
    }
    validation_metrics = classification(concatenate(validation_kalman),
                                        validation_labels, trigger)
    baseline_report = {}
    for name, (train_by_run, test_by_run) in baseline_scores.items():
        threshold, train_metrics = tune_threshold(concatenate(train_by_run),
                                                  calibration_labels)
        baseline_report[name] = {
            "threshold": threshold,
            "calibration": train_metrics,
            "validation": classification(concatenate(test_by_run),
                                         validation_labels, threshold),
        }

    leads = event_leads(validation, validation_kalman, trigger, endpoint,
                        args.trigger_samples, args.max_lead_ms)
    normal_scores = [score for score, label in zip(concatenate(calibration_kalman),
                                                   calibration_labels) if not label]
    release = analyze.percentile(normal_scores, 0.60) if normal_scores else trigger * 0.95
    release = min(release, trigger - max(0.5, abs(trigger) * 0.01))
    best_baseline_f1 = max(value["validation"]["f1"] for value in baseline_report.values())
    validated = (
        validation_metrics["positive_samples"] > 0.0
        and validation_metrics["f1"] > best_baseline_f1
        and leads["matched_event_onsets"] > 0.0
        and leads["median_lead_ms"] > action["p95_ms"]
    )
    parameters = {
        "active_ohead": int(actuator["recommended_ohead"]),
        "trigger_rtt_ms": trigger,
        "release_rtt_ms": release,
        "horizon_ms": horizon_ms,
        "kalman_q": selected_q,
        "kalman_r": measurement_noise,
        "trigger_samples": args.trigger_samples,
        "release_samples": args.release_samples,
        "cooldown_ms": max(1000.0, 2.0 * action["p95_ms"]),
        "warmup_ms": 3000,
    }
    report.update({
        "status": "model_development_required",
        "pilot_outcome": (
            "candidate_passed_initial_check" if validated
            else "candidate_did_not_beat_baselines"
        ),
        "endpoint": endpoint,
        "calibration_run_ids": [item.run_id for item in calibration],
        "validation_run_ids": [item.run_id for item in validation],
        "sample_interval_ms": interval_ms,
        "maximum_lead_ms": args.max_lead_ms,
        "kalman": {
            "parameters": parameters,
            "calibration": calibration_metrics,
            "validation": validation_metrics,
            "event_leads_validation": leads,
        },
        "baselines": baseline_report,
        "criteria": {
            "kalman_f1_exceeds_both_baselines": validation_metrics["f1"] > best_baseline_f1,
            "median_lead_exceeds_action_p95": leads["median_lead_ms"] > action["p95_ms"],
        },
    })
    analyze.write_json(Path(args.output), report)
    report["controller_env_created"] = False
    print(json.dumps(report, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results")
    parser.add_argument("--actuator", default="results/actuator.json")
    parser.add_argument("--output", default="results/controller_calibration.json")
    parser.add_argument("--controller-env", default="experiments/controller.env")
    parser.add_argument("--calibration-ids", default="")
    parser.add_argument("--validation-ids", default="")
    parser.add_argument("--phase-high-s", type=float, default=15.0)
    parser.add_argument("--max-lead-ms", type=float, default=5000.0)
    parser.add_argument("--q-grid", default="0.01,0.1,1,10")
    parser.add_argument("--trigger-samples", type=int, default=3)
    parser.add_argument("--release-samples", type=int, default=5)
    return parser


if __name__ == "__main__":
    command_calibrate(build_parser().parse_args())
