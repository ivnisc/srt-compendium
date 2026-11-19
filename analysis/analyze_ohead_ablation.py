#!/usr/bin/env python3
"""Audita y analiza la ablación congelada de SRTO_OHEADBW."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Sequence

import analyze


NON_NUMERIC_STATS = {"role", "variant", "decision"}
TOTAL_COUNTERS = (
    "pktSentTotal", "pktRecvTotal", "pktSentUniqueTotal",
    "pktRecvUniqueTotal", "pktSndLossTotal", "pktRcvLossTotal",
    "pktRetransTotal", "pktSndDropTotal", "pktRcvDropTotal",
    "byteSentTotal", "byteRecvTotal", "byteSentUniqueTotal",
    "byteRecvUniqueTotal", "byteRetransTotal", "byteSndDropTotal",
    "byteRcvDropTotal",
)
PHASES = ("high", "low", "recovery")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def write_json_atomic(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        analyze.write_json(temporary, value)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def finite_number(value: str | None) -> bool:
    try:
        return value not in (None, "") and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def stats_are_finite(rows: list[dict[str, str]]) -> bool:
    if not rows:
        return False
    numeric_fields = set(rows[0]) - NON_NUMERIC_STATS
    return all(finite_number(row.get(field))
               for row in rows for field in numeric_fields)


def counters_are_monotonic(rows: list[dict[str, str]]) -> bool:
    for field in TOTAL_COUNTERS:
        values = [float(row[field]) for row in rows]
        if any(right < left for left, right in zip(values, values[1:])):
            return False
    return True


def schedule_audit(rows: list[dict[str, str]], sample_ms: float,
                   duration_s: float) -> dict[str, object]:
    if len(rows) < 2 or sample_ms <= 0.0:
        return {"valid": False, "reason": "insufficient_rows"}
    scheduled_rows = rows[:-1]
    period_us = sample_ms * 1000.0
    scheduled_us = [
        float(row["elapsed_ms"]) * 1000.0 -
        float(row["sample_lateness_us"])
        for row in scheduled_rows
    ]
    origin = scheduled_us[0]
    errors = [abs(value - (origin + index * period_us))
              for index, value in enumerate(scheduled_us)]
    positions = [round((value - origin) / period_us)
                 for value in scheduled_us]
    minimum_rows = math.floor(duration_s * 1000.0 / sample_ms)
    return {
        "valid": (
            len(scheduled_rows) >= minimum_rows
            and positions == list(range(len(scheduled_rows)))
            and max(errors, default=math.inf) <= 1000.0
        ),
        "rows": len(rows),
        "scheduled_rows": len(scheduled_rows),
        "minimum_scheduled_rows": minimum_rows,
        "maximum_grid_error_us": max(errors, default=math.inf),
        "final_row_excluded_from_grid": True,
    }


def transition_rows(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    previous: int | None = None
    for row in rows:
        current = int(float(row["ohead_pct"]))
        if previous is not None and current != previous:
            output.append({
                "elapsed_ms": float(row["elapsed_ms"]),
                "previous": previous,
                "current": current,
                "decision": row["decision"],
                "v2_confirmed": int(float(row.get("v2_confirmed", "0"))),
            })
        previous = current
    return output


def active_maxbw_diagnostic(rows: list[dict[str, str]],
                            input_rate_bps: float) -> dict[str, object]:
    usable = []
    previous: int | None = None
    active_samples = 0
    for row in rows:
        current = int(float(row["ohead_pct"]))
        if current == 10:
            active_samples += 1
            if previous == 10 and float(row["mbpsMaxBW"]) > 0.0:
                usable.append(float(row["mbpsMaxBW"]))
        previous = current
    if active_samples == 0:
        return {"status": "not_activated", "active_samples": 0}
    if not usable:
        return {
            "status": "unverified_short_actuation",
            "active_samples": active_samples,
            "usable_samples": 0,
        }
    expected = input_rate_bps / 1e6 * 1.10
    observed = analyze.median(usable)
    error = abs(observed - expected) / expected
    return {
        "status": "matches" if error <= 0.03 else "mismatch",
        "active_samples": active_samples,
        "usable_samples": len(usable),
        "expected_mbps": expected,
        "observed_median_mbps": observed,
        "relative_error": error,
    }


def validated_disposition(path: Path | None, allowed: str,
                          expected: dict[str, object]) -> dict | None:
    if path is None or not path.is_file():
        return None
    disposition = json.loads(path.read_text())
    if disposition.get("decision") != allowed:
        return None
    if not str(disposition.get("justification", "")).strip():
        return None
    if any(str(disposition.get(key)) != str(value)
           for key, value in expected.items()):
        return None
    return disposition


def resolve_controller_crosscheck(
        arm: str, crosscheck_valid: bool, disposition_path: Path | None,
        protocol: dict, run_id: str) -> tuple[str, dict | None]:
    if arm != "assistant":
        return "not_required", None
    if crosscheck_valid:
        return "ready", None
    allowed = protocol["controller_crosscheck_policy"]["allowed_disposition"]
    disposition = validated_disposition(
        disposition_path, allowed, {"run_id": run_id})
    return (
        "readiness_failure_retained" if disposition else "discrepancy",
        disposition,
    )


def has_actuator_error(rows: list[dict[str, str]]) -> bool:
    return any(row.get("decision") == "set_ohead_error" for row in rows)


def phase_bounds(run_dir: Path, duration_s: float) -> dict[str, tuple[float, float]]:
    events = read_csv(run_dir / "network_events.csv")
    by_phase = {row["phase"]: float(row["sender_elapsed_ms"])
                for row in events}
    required = {"high", "low", "recovery"}
    if set(by_phase) != required:
        raise ValueError("network_events no contiene las tres fases")
    low = by_phase["low"]
    recovery = by_phase["recovery"]
    end = duration_s * 1000.0
    if not (0.0 <= by_phase["high"] < low < recovery < end + 1000.0):
        raise ValueError("límites temporales de fase inválidos")
    return {
        "high": (0.0, low),
        "low": (low, recovery),
        "recovery": (recovery, end),
    }


def rows_in_phase(rows: list[dict[str, str]], begin: float,
                  end: float) -> list[dict[str, str]]:
    return [row for row in rows
            if begin <= float(row["elapsed_ms"]) < end]


def cumulative_before(rows: list[dict[str, str]], field: str,
                      boundary: float) -> float:
    values = [float(row[field]) for row in rows
              if float(row["elapsed_ms"]) < boundary]
    return values[-1] if values else 0.0


def phase_metrics(tx_rows: list[dict[str, str]],
                  rx_rows: list[dict[str, str]],
                  bounds: dict[str, tuple[float, float]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for phase, (begin, end) in bounds.items():
        tx = rows_in_phase(tx_rows, begin, end)
        rx = rows_in_phase(rx_rows, begin, end)
        result[f"{phase}_pktRcvBelated"] = sum(
            float(row["pktRcvBelated"]) for row in rx)
        for field, cumulative_rows in (
            ("pktRcvDropTotal", rx_rows),
            ("pktRetransTotal", tx_rows),
            ("pktSndDropTotal", tx_rows),
        ):
            result[f"{phase}_{field}"] = max(
                0.0,
                cumulative_before(cumulative_rows, field, end) -
                cumulative_before(cumulative_rows, field, begin),
            )
        rtt = [float(row["msRTT"]) for row in tx
               if float(row["msRTT"]) > 0.0]
        sndbuf = [float(row["msSndBuf"]) for row in tx]
        send_rate = [float(row["mbpsSendRate"]) for row in tx]
        maxbw = [float(row["mbpsMaxBW"]) for row in tx]
        result[f"{phase}_rtt_p95_ms"] = analyze.percentile(rtt, 0.95)
        result[f"{phase}_sndbuf_p95_ms"] = analyze.percentile(sndbuf, 0.95)
        result[f"{phase}_sndbuf_peak_ms"] = max(sndbuf, default=0.0)
        result[f"{phase}_send_rate_mean_mbps"] = analyze.mean(send_rate)
        result[f"{phase}_maxbw_median_mbps"] = analyze.median(maxbw)
    return result


def expected_arm(protocol: dict, arm: str) -> dict:
    matches = [item for item in protocol["arms"] if item["id"] == arm]
    if len(matches) != 1:
        raise ValueError(f"brazo desconocido: {arm}")
    return matches[0]


def metadata_matches(run: dict[str, str], tx: dict[str, str], protocol: dict,
                     arm: str, variant: str, run_id: str,
                     protocol_hash: str, plan_hash: str) -> dict[str, bool]:
    expected = expected_arm(protocol, arm)
    numeric_run = {
        "input_rate_bps": protocol["input_rate_bps"],
        "duration_s": protocol["duration_s"],
        "phase_high_s": protocol["phase_high_s"],
        "phase_low_s": protocol["phase_low_s"],
        "latency_ms": protocol["latency_ms"],
        "delay_ms": protocol["delay_ms"],
        "loss_percent": protocol["loss_percent"],
        "reorder_percent_effective": protocol["reorder_percent"],
        "sample_ms": protocol["sample_ms"],
        "queue_limit_packets": protocol["queue_limit_packets"],
        "queue_formula_capacity_percent": protocol["queue_formula_capacity_percent"],
        "high_capacity_percent": protocol["high_capacity_percent"],
        "low_capacity_percent": protocol["low_capacity_percent"],
    }
    numeric_ok = all(
        finite_number(run.get(field)) and
        abs(float(run[field]) - float(value)) <= 1e-9
        for field, value in numeric_run.items()
    )
    return {
        "identity": (
            run.get("protocol_version") == protocol["protocol_id"]
            and run.get("scenario") == "dynamic"
            and run.get("variant") == variant == expected["variant"]
            and run.get("run_id") == run_id
        ),
        "profile": numeric_ok,
        "hashes": (
            run.get("source_sha256") == protocol["expected_source_sha256"]
            and run.get("srt_git_commit") == protocol["expected_srt_git_commit"]
            and run.get("experiment_binary_sha256") ==
                protocol["expected_binary_sha256"]
            and run.get("experiment_matrix_sha256") == protocol_hash
            and run.get("experiment_plan_sha256") == plan_hash
        ),
        "tree_clean": run.get("experiment_git_dirty") == "no",
        "cpu_affinity": (
            run.get("sender_cpu") == protocol["sender_cpu"]
            and run.get("receiver_cpu") == protocol["receiver_cpu"]
        ),
        "mode_and_ohead": (
            tx.get("variant") == variant
            and tx.get("mode") == expected["mode"]
            and int(float(tx.get("nominal_ohead", "nan"))) ==
                int(expected["nominal_ohead"])
            and int(float(tx.get("active_ohead", "nan"))) ==
                int(expected["active_ohead"])
        ),
        "controller_hashes": (
            arm != "assistant" or (
                run.get("controller_selection_sha256") ==
                    protocol["expected_controller_selection_sha256"]
                and run.get("controller_config_sha256") ==
                    protocol["expected_controller_config_sha256"]
            )
        ),
    }


def baseline_calibration_audit(rows: list[dict[str, str]],
                               protocol: dict) -> dict[str, object]:
    config = protocol["baseline_integrity"]
    begin = float(config["begin_ms"])
    end = float(config["end_ms"])
    baseline = rows_in_phase(rows, begin, end)
    positive = [row for row in baseline
                if finite_number(row.get("msRTT"))
                and float(row["msRTT"]) > 0.0]
    all_positive = len(positive) == len(baseline)
    minimum = int(config["minimum_samples"])
    return {
        "valid": len(baseline) >= minimum and all_positive,
        "samples": len(baseline),
        "positive_rtt_samples": len(positive),
        "minimum_samples": minimum,
        "begin_ms": begin,
        "end_ms": end,
    }


def network_timing_audit(bounds: dict[str, tuple[float, float]],
                         protocol: dict) -> dict[str, object]:
    expected_low = float(protocol["phase_high_s"]) * 1000.0
    expected_recovery = (
        float(protocol["phase_high_s"]) + float(protocol["phase_low_s"])
    ) * 1000.0
    actual_low = bounds["low"][0]
    actual_recovery = bounds["recovery"][0]
    errors = {
        "low_ms": actual_low - expected_low,
        "recovery_ms": actual_recovery - expected_recovery,
    }
    tolerance = float(protocol["network_timing_tolerance_ms"])
    return {
        "valid": all(abs(value) <= tolerance for value in errors.values()),
        "tolerance_ms": tolerance,
        "errors_ms": errors,
    }


def arm_assignment_and_behavior(
        arm: str, values: list[int], transitions: list[dict[str, object]],
        final_ohead: int, changes_meta: int,
        bounds: dict[str, tuple[float, float]], sample_ms: float,
        v2_ready: int) -> tuple[bool, dict[str, object], bool]:
    pairs = [(item["previous"], item["current"]) for item in transitions]
    decisions = [item["decision"] for item in transitions]
    behavior: dict[str, object] = {
        "transition_count_series": len(transitions),
        "controller_changes_metadata": changes_meta,
        "final_ohead_metadata": final_ohead,
    }
    crosscheck_valid = True
    if not values:
        return False, behavior, crosscheck_valid
    if arm == "vanilla":
        valid = (set(values) == {25} and not transitions
                 and final_ohead == 25 and changes_meta == 0)
        behavior["protocol_conforming"] = valid
        return valid, behavior, crosscheck_valid
    if arm == "const10":
        valid = (set(values) == {10} and not transitions
                 and final_ohead == 10 and changes_meta == 0)
        behavior["protocol_conforming"] = valid
        return valid, behavior, crosscheck_valid
    if arm == "oracle10":
        expected_times = [bounds["low"][0], bounds["recovery"][0]]
        transition_times = [float(item["elapsed_ms"]) for item in transitions]
        tolerance = max(250.0, 2.0 * sample_ms)
        timing_errors = [actual - target for actual, target in
                         zip(transition_times, expected_times)]
        timing_ok = (
            len(transition_times) == 2
            and len(timing_errors) == 2
            and all(abs(value) <= tolerance for value in timing_errors)
        )
        valid = (
            set(values) == {10, 25}
            and pairs == [(25, 10), (10, 25)]
            and decisions == ["fixed_activate", "fixed_restore"]
            and timing_ok and final_ohead == 25 and changes_meta == 2
        )
        behavior.update({
            "protocol_conforming": valid,
            "transition_timing_error_ms": timing_errors,
        })
        return valid, behavior, crosscheck_valid

    activation = [item for item in transitions if item["current"] == 10]
    restoration = [item for item in transitions if item["current"] == 25]
    early = any(float(item["elapsed_ms"]) < bounds["low"][0]
                for item in activation)
    missing_restore = bool(activation) and not restoration
    no_activation = not activation
    standard_cycle = (
        pairs == [(25, 10), (10, 25)]
        and decisions == ["v2_activate", "v2_restore"]
        and not early
        and int(activation[0]["v2_confirmed"]) == 1
    )
    protocol_conforming = no_activation or standard_cycle
    behavior.update({
        "no_activation": no_activation,
        "early_activation": early,
        "missing_restore": missing_restore,
        "unexpected_transition_count": len(transitions) not in (0, 2),
        "standard_cycle": standard_cycle,
        "protocol_conforming": protocol_conforming,
    })
    assignment_integrity = values[0] == 25 and set(values).issubset({10, 25})
    crosscheck_valid = v2_ready == 1
    return assignment_integrity, behavior, crosscheck_valid


def audit_run(run_dir: Path, protocol: dict, arm: str, variant: str,
              run_id: str, protocol_hash: str, plan_hash: str,
              equivalence_path: Path | None = None,
              disposition_path: Path | None = None,
              controller_disposition_path: Path | None = None,
              integrity_only: bool = False) -> dict[str, object]:
    required = (
        "run_metadata.csv", "tx_metadata.csv", "rx_metadata.csv",
        "tx_stats.csv", "rx_stats.csv", "network_events.csv",
    )
    missing = [name for name in required if not (run_dir / name).is_file()]
    if missing:
        return {
            "status": "invalid_integrity", "run_dir": str(run_dir),
            "arm": arm, "integrity_criteria": {"required_files": False},
            "missing": missing,
        }

    run = analyze.read_key_value(run_dir / "run_metadata.csv")
    tx_meta = analyze.read_key_value(run_dir / "tx_metadata.csv")
    tx_rows = read_csv(run_dir / "tx_stats.csv")
    rx_rows = read_csv(run_dir / "rx_stats.csv")
    duration = float(protocol["duration_s"])
    sample_ms = float(protocol["sample_ms"])
    metadata_criteria = metadata_matches(
        run, tx_meta, protocol, arm, variant, run_id,
        protocol_hash, plan_hash)
    tx_schedule = schedule_audit(tx_rows, sample_ms, duration)
    rx_schedule = schedule_audit(rx_rows, sample_ms, duration)
    transitions = transition_rows(tx_rows) if tx_rows else []
    values = [int(float(row["ohead_pct"])) for row in tx_rows]
    maxbw_errors = []
    for row in tx_rows:
        observed = float(row["mbpsMaxBW"])
        expected_maxbw = (float(protocol["input_rate_bps"]) / 1e6 *
                          (1.0 + float(row["ohead_pct"]) / 100.0))
        if observed > 0.0:
            maxbw_errors.append(abs(observed - expected_maxbw) / expected_maxbw)
    maxbw_error_p99 = analyze.percentile(maxbw_errors, 0.99)
    final_ohead = int(float(tx_meta.get("final_ohead", "-1")))
    changes_meta = int(float(tx_meta.get("controller_changes", "-1")))

    payload_read = float(tx_meta.get("payload_bytes_read", "nan"))
    payload_sent = float(tx_meta.get("payload_bytes_sent", "nan"))
    payload_budget = float(tx_meta.get("payload_budget_bytes", "nan"))
    payload_duration = float(tx_meta.get("payload_send_duration_s", "nan"))
    payload_complete = (
        all(math.isfinite(value) for value in
            (payload_read, payload_sent, payload_budget, payload_duration))
        and payload_read == payload_budget
        and payload_sent == payload_read
        and payload_read == float(protocol["expected_payload_bytes"])
        and abs(payload_duration - duration) < 0.010
    )

    network_timing: dict[str, object] = {"valid": False}
    try:
        bounds = phase_bounds(run_dir, duration)
        bounds_valid = True
        network_timing = network_timing_audit(bounds, protocol)
    except (OSError, KeyError, ValueError):
        bounds = {}
        bounds_valid = False

    baseline = baseline_calibration_audit(tx_rows, protocol)
    arm_integrity = False
    behavior: dict[str, object] = {}
    crosscheck_valid = True
    if bounds_valid:
        arm_integrity, behavior, crosscheck_valid = arm_assignment_and_behavior(
            arm, values, transitions, final_ohead, changes_meta, bounds,
            sample_ms, int(float(tx_meta.get("v2_ready", "0"))))

    actuator_error = has_actuator_error(tx_rows)
    active_maxbw = (
        active_maxbw_diagnostic(tx_rows, float(protocol["input_rate_bps"]))
        if arm == "assistant" else {"status": "not_applicable"}
    )

    integrity_criteria = {
        "required_files": True,
        **metadata_criteria,
        "payload_complete": payload_complete,
        "finite_tx_series": stats_are_finite(tx_rows),
        "finite_rx_series": stats_are_finite(rx_rows),
        "monotonic_tx_counters": counters_are_monotonic(tx_rows),
        "monotonic_rx_counters": counters_are_monotonic(rx_rows),
        "tx_schedule_continuous": bool(tx_schedule.get("valid")),
        "rx_schedule_continuous": bool(rx_schedule.get("valid")),
        "network_phase_bounds": bounds_valid,
        "network_timing": bool(network_timing.get("valid")),
        "baseline_calibrable": bool(baseline["valid"]),
        "maxbw_matches_ohead": bool(maxbw_errors) and maxbw_error_p99 <= 0.03,
        "assigned_arm_applied": arm_integrity,
    }
    integrity_valid = all(integrity_criteria.values())

    controller_status, controller_disposition = resolve_controller_crosscheck(
        arm, crosscheck_valid, controller_disposition_path, protocol, run_id)

    equivalence_status = "not_required"
    disposition: dict[str, object] | None = None
    if arm == "assistant" and not integrity_only and \
            controller_status == "readiness_failure_retained":
        equivalence_status = "not_applicable_controller_readiness_failure"
    elif arm == "assistant" and not integrity_only:
        if equivalence_path is None or not equivalence_path.is_file():
            equivalence_status = "missing"
        else:
            equivalence = json.loads(equivalence_path.read_text())
            equivalence_status = str(equivalence.get("status", "missing"))
        if equivalence_status != "equivalent" and disposition_path is not None \
                and disposition_path.is_file():
            allowed = protocol["equivalence_policy"][
                "allowed_reference_disposition"]
            disposition = validated_disposition(
                disposition_path, allowed, {"run_id": run_id})
            if disposition:
                equivalence_status = "reference_failure_retained"
            else:
                equivalence_status = "invalid_disposition"

    if not integrity_valid:
        status = "invalid_integrity"
    elif actuator_error:
        status = "actuator_application_error"
    elif controller_status == "discrepancy":
        status = "controller_crosscheck_discrepancy"
    elif integrity_only:
        status = "valid_integrity"
    elif arm == "assistant" and equivalence_status == "missing":
        status = "audit_error"
    elif arm == "assistant" and equivalence_status == "invalid_disposition":
        status = "audit_error"
    elif arm == "assistant" and equivalence_status not in (
            "equivalent", "reference_failure_retained",
            "not_applicable_controller_readiness_failure"):
        status = "equivalence_discrepancy"
    else:
        status = "valid"

    record: dict[str, object] = {
        "status": status,
        "run_dir": str(run_dir),
        "arm": arm,
        "variant": variant,
        "run_id": run_id,
        "integrity_criteria": integrity_criteria,
        "integrity_valid": integrity_valid,
        "baseline": baseline,
        "network_timing": network_timing,
        "controller_crosscheck_valid": crosscheck_valid,
        "controller_crosscheck_status": controller_status,
        "controller_disposition": controller_disposition,
        "behavior": behavior,
        "actuator_error": actuator_error,
        "active_maxbw_diagnostic": active_maxbw,
        "equivalence_status": equivalence_status,
        "equivalence_disposition": disposition,
        "tx_schedule": tx_schedule,
        "rx_schedule": rx_schedule,
        "transitions": transitions,
        "maxbw_relative_error_p99": maxbw_error_p99,
    }
    if integrity_valid:
        tx_summary = analyze.summarize_stats(run_dir / "tx_stats.csv")
        rx_summary = analyze.summarize_stats(run_dir / "rx_stats.csv")
        metrics = phase_metrics(tx_rows, rx_rows, bounds)
        metrics.update({
            "byteRecvUniqueTotal": float(rx_summary["byteRecvUniqueTotal"]),
            "U": float(rx_summary["byteRecvUniqueTotal"]) /
                float(protocol["baseline_B_ref"]),
            "pktRcvBelated": float(rx_summary["pktRcvBelated_sum"]),
            "pktRcvDropTotal": float(rx_summary["pktRcvDropTotal"]),
            "pktRetransTotal": float(tx_summary["pktRetransTotal"]),
            "pktSndDropTotal": float(tx_summary["pktSndDropTotal"]),
            "rtt_p95_ms": float(tx_summary["rtt_p95_ms"]),
            "sndbuf_peak_ms": float(tx_summary["snd_buffer_peak_ms"]),
            "v2_eval_p99_us": float(tx_summary["v2_eval_p99_us"]),
            "activation_count": 1.0 if any(
                item["decision"] == "v2_activate" for item in transitions) else 0.0,
        })
        record["metrics"] = metrics
        record["experiment_git_commit"] = run.get("experiment_git_commit", "")
    return record


def safe_audit_run(*args, **kwargs) -> dict[str, object]:
    try:
        return audit_run(*args, **kwargs)
    except Exception as error:  # el informe debe sobrevivir a un fallo del auditor
        run_dir = kwargs.get("run_dir", args[0] if args else "")
        return {
            "status": "audit_error",
            "run_dir": str(run_dir),
            "error_type": type(error).__name__,
            "error": str(error),
        }


def beta_continued_fraction(a: float, b: float, x: float) -> float:
    maximum_iterations = 200
    epsilon = 3e-14
    tiny = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = tiny if abs(d) < tiny else d
    d = 1.0 / d
    result = d
    for iteration in range(1, maximum_iterations + 1):
        even = 2 * iteration
        coefficient = iteration * (b - iteration) * x / (
            (qam + even) * (a + even))
        d = 1.0 + coefficient * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + coefficient / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        result *= d * c
        coefficient = -(a + iteration) * (qab + iteration) * x / (
            (a + even) * (qap + even))
        d = 1.0 + coefficient * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + coefficient / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < epsilon:
            return result
    raise ArithmeticError("la fracción continua beta no convergió")


def regularized_beta(x: float, a: float, b: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * beta_continued_fraction(a, b, x) / a
    return 1.0 - front * beta_continued_fraction(b, a, 1.0 - x) / b


def student_t_cdf(value: float, degrees: float) -> float:
    if degrees <= 0.0:
        raise ValueError("los grados de libertad deben ser positivos")
    if value == 0.0:
        return 0.5
    x = degrees / (degrees + value * value)
    tail = 0.5 * regularized_beta(x, degrees / 2.0, 0.5)
    return 1.0 - tail if value > 0.0 else tail


def student_t_ppf(probability: float, degrees: float) -> float:
    if not 0.0 < probability < 1.0:
        raise ValueError("la probabilidad debe estar entre cero y uno")
    if probability < 0.5:
        return -student_t_ppf(1.0 - probability, degrees)
    low, high = 0.0, 1.0
    while student_t_cdf(high, degrees) < probability:
        high *= 2.0
    for _ in range(100):
        middle = (low + high) / 2.0
        if student_t_cdf(middle, degrees) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def welch_tost(a: Sequence[float], b: Sequence[float], margin: float,
               alpha: float) -> dict[str, object]:
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    difference = mean_a - mean_b
    variance_a = statistics.variance(a)
    variance_b = statistics.variance(b)
    term_a = variance_a / len(a)
    term_b = variance_b / len(b)
    standard_error = math.sqrt(term_a + term_b)
    degrees = (term_a + term_b) ** 2 / (
        term_a ** 2 / (len(a) - 1) + term_b ** 2 / (len(b) - 1))
    critical = student_t_ppf(1.0 - alpha, degrees)
    interval = (difference - critical * standard_error,
                difference + critical * standard_error)
    lower_t = (difference + margin) / standard_error
    upper_t = (difference - margin) / standard_error
    lower_p = 1.0 - student_t_cdf(lower_t, degrees)
    upper_p = student_t_cdf(upper_t, degrees)
    return {
        "difference": difference,
        "standard_error": standard_error,
        "degrees_of_freedom": degrees,
        "ci90_low": interval[0],
        "ci90_high": interval[1],
        "lower_test_p": lower_p,
        "upper_test_p": upper_p,
        "equivalent": lower_p < alpha and upper_p < alpha,
    }


def bootstrap_difference(a: Sequence[float], b: Sequence[float],
                         iterations: int, seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        estimates.append(
            statistics.fmean(rng.choice(a) for _ in a) -
            statistics.fmean(rng.choice(b) for _ in b)
        )
    return (analyze.percentile(estimates, 0.025),
            analyze.percentile(estimates, 0.975))


def permutation_test(a: Sequence[float], b: Sequence[float], iterations: int,
                     seed: int) -> dict[str, float]:
    observed = statistics.fmean(a) - statistics.fmean(b)
    combined = list(a) + list(b)
    count = 0
    rng = random.Random(seed)
    for _ in range(iterations):
        shuffled = rng.sample(combined, len(combined))
        difference = (statistics.fmean(shuffled[:len(a)]) -
                      statistics.fmean(shuffled[len(a):]))
        if abs(difference) >= abs(observed):
            count += 1
    return {
        "observed_difference": observed,
        "two_sided_p_monte_carlo": (count + 1.0) / (iterations + 1.0),
        "iterations": float(iterations),
    }


def summarize(values: Sequence[float]) -> dict[str, float]:
    return {
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "sample_sd": statistics.stdev(values),
        "minimum": min(values),
        "maximum": max(values),
    }


def slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    x_mean, y_mean = statistics.fmean(xs), statistics.fmean(ys)
    denominator = sum((value - x_mean) ** 2 for value in xs)
    return (sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) /
            denominator if denominator else 0.0)


def comparison(a: list[dict[str, object]], b: list[dict[str, object]],
               iterations: int, seed: int) -> dict[str, object]:
    a_values = [float(row["metrics"]["U"]) for row in a]  # type: ignore[index]
    b_values = [float(row["metrics"]["U"]) for row in b]  # type: ignore[index]
    ci = bootstrap_difference(a_values, b_values, iterations, seed)
    superiority = sum(left > right for left in a_values for right in b_values)
    return {
        "mean_difference": statistics.fmean(a_values) - statistics.fmean(b_values),
        "median_difference": statistics.median(a_values) - statistics.median(b_values),
        "ci95_low": ci[0],
        "ci95_high": ci[1],
        "probability_of_superiority": superiority / (len(a_values) * len(b_values)),
    }


def audit_clean_controls(results: Path, protocol: dict,
                         protocol_hash: str, plan_hash: str) -> dict[str, object]:
    expected_ids = ([f"pre_{index}" for index in range(1, 6)] +
                    [f"mid_{index}" for index in (100, 200, 300, 400)] +
                    [f"post_{index}" for index in range(1, 6)])
    records = []
    for run_id in expected_ids:
        run_dir = results / "clean" / "ablation_guard" / f"run_{run_id}"
        required = all((run_dir / name).is_file() for name in (
            "run_metadata.csv", "tx_metadata.csv", "rx_metadata.csv",
            "tx_stats.csv", "rx_stats.csv", "network_events.csv"))
        if not required:
            records.append({"run_id": run_id, "valid": False,
                            "reason": "missing_files"})
            continue
        run = analyze.read_key_value(run_dir / "run_metadata.csv")
        tx_meta = analyze.read_key_value(run_dir / "tx_metadata.csv")
        tx_rows = read_csv(run_dir / "tx_stats.csv")
        rx_rows = read_csv(run_dir / "rx_stats.csv")
        tx_summary = analyze.summarize_stats(run_dir / "tx_stats.csv")
        rx_summary = analyze.summarize_stats(run_dir / "rx_stats.csv")
        events = read_csv(run_dir / "network_events.csv")
        governors = run.get("cpu_governors", "unknown")
        cpu_ok = (
            governors == "performance" or (
                governors == "unavailable"
                and run.get("host_virtualization") not in ("", "none", "unknown")
                and run.get("cpu_frequency_control") == "unavailable"
            )
        )
        payload = float(tx_meta.get("payload_bytes_read", "nan"))
        budget = float(tx_meta.get("payload_budget_bytes", "nan"))
        sent = float(tx_meta.get("payload_bytes_sent", "nan"))
        zero_events = all(float(value) == 0.0 for value in (
            tx_summary["pktSndLossTotal"], tx_summary["pktRetransTotal"],
            tx_summary["pktSndDropTotal"], rx_summary["pktRcvLossTotal"],
            rx_summary["pktRcvDropTotal"], rx_summary["pktRcvBelated_sum"],
            rx_summary["pktRcvRetrans_sum"],
        ))
        valid = all((
            run.get("protocol_version") == protocol["protocol_id"],
            run.get("scenario") == "clean",
            run.get("variant") == "ablation_guard",
            run.get("run_id") == run_id,
            run.get("source_sha256") == protocol["expected_source_sha256"],
            run.get("srt_git_commit") == protocol["expected_srt_git_commit"],
            run.get("experiment_binary_sha256") == protocol["expected_binary_sha256"],
            run.get("experiment_matrix_sha256") == protocol_hash,
            run.get("experiment_plan_sha256") == plan_hash,
            run.get("experiment_git_dirty") == "no",
            cpu_ok,
            run.get("sender_cpu") == protocol["sender_cpu"],
            run.get("receiver_cpu") == protocol["receiver_cpu"],
            payload == budget == sent,
            payload == float(protocol["expected_payload_bytes"]),
            abs(float(tx_meta["payload_send_duration_s"]) -
                float(protocol["duration_s"])) < 0.010,
            not events,
            stats_are_finite(tx_rows), stats_are_finite(rx_rows),
            counters_are_monotonic(tx_rows), counters_are_monotonic(rx_rows),
            bool(schedule_audit(tx_rows, protocol["sample_ms"],
                                protocol["duration_s"])["valid"]),
            bool(schedule_audit(rx_rows, protocol["sample_ms"],
                                protocol["duration_s"])["valid"]),
            zero_events,
            float(tx_summary["rtt_p99_ms"]) < 1.0,
        ))
        records.append({
            "run_id": run_id, "valid": valid,
            "byteSentUniqueTotal": float(tx_summary["byteSentUniqueTotal"]),
            "payload_bytes_read": payload,
        })
    valid_rows = [row for row in records if row["valid"]]
    sent = [float(row["byteSentUniqueTotal"]) for row in valid_rows]
    payload = [float(row["payload_bytes_read"]) for row in valid_rows]
    stable = (
        len(valid_rows) == len(expected_ids)
        and analyze.coefficient_of_variation(sent) < 0.001
        and analyze.coefficient_of_variation(payload) < 0.001
    )
    return {
        "status": "accepted" if stable else "rejected",
        "expected_runs": len(expected_ids),
        "valid_runs": len(valid_rows),
        "byteSentUniqueTotal_cv": analyze.coefficient_of_variation(sent),
        "payload_bytes_read_cv": analyze.coefficient_of_variation(payload),
        "runs": records,
    }


def final_analysis(args: argparse.Namespace, protocol: dict,
                   protocol_hash: str, plan_hash: str) -> dict[str, object]:
    plan = read_csv(Path(args.plan))
    progress = read_csv(Path(args.progress))
    valid_progress = [row for row in progress
                      if row["kind"] == "experimental"
                      and row["outcome"] == "valid"]
    by_order: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in valid_progress:
        by_order[row["plan_execution_order"]].append(row)
    plan_orders = {row["execution_order"] for row in plan}
    progress_complete = (
        set(by_order) == plan_orders
        and all(len(rows) == 1 for rows in by_order.values())
    )
    if not progress_complete:
        raise SystemExit("El progreso no contiene una corrida válida por trabajo")

    plan_by_order = {row["execution_order"]: row for row in plan}
    records: list[dict[str, object]] = []
    for order in sorted(plan_orders, key=int):
        progress_row = by_order[order][0]
        planned = plan_by_order[order]
        if progress_row["arm"] != planned["arm"] or \
                progress_row["variant"] != planned["variant"]:
            raise SystemExit(f"El progreso no coincide con el plan en {order}")
        run_dir = Path(progress_row["run_dir"])
        equivalence = None
        disposition = None
        if planned["arm"] == "assistant":
            equivalence = (Path(args.results) / "equivalence" /
                           f"run_{progress_row['run_id']}.json")
            disposition = (Path(args.results) / "equivalence_dispositions" /
                           f"run_{progress_row['run_id']}.json")
            controller_disposition = (
                Path(args.results) / "controller_dispositions" /
                f"run_{progress_row['run_id']}.json")
        else:
            controller_disposition = None
        record = safe_audit_run(
            run_dir, protocol, planned["arm"], planned["variant"],
            progress_row["run_id"], protocol_hash, plan_hash, equivalence,
            disposition, controller_disposition)
        if record["status"] != "valid":
            raise SystemExit(f"La auditoría final rechazó {run_dir}")
        record.update({
            "execution_order": int(order),
            "block": int(planned["block"]),
            "position": int(planned["position"]),
        })
        records.append(record)

    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in records:
        groups[str(row["arm"])].append(row)
    expected_runs = int(protocol["runs_per_arm"])
    if any(len(groups[arm]) != expected_runs
           for arm in ("vanilla", "const10", "oracle10", "assistant")):
        raise SystemExit("La campaña no contiene cien corridas válidas por brazo")

    iterations = int(protocol["bootstrap_iterations"])
    seed = int(protocol["bootstrap_seed"])
    primary = comparison(groups["assistant"], groups["const10"],
                         iterations, seed)
    assistant_u = [float(row["metrics"]["U"])  # type: ignore[index]
                   for row in groups["assistant"]]
    const10_u = [float(row["metrics"]["U"])  # type: ignore[index]
                 for row in groups["const10"]]
    tost = welch_tost(assistant_u, const10_u,
                      float(protocol["equivalence_margin_U"]),
                      float(protocol["alpha"]))
    low, high = float(primary["ci95_low"]), float(primary["ci95_high"])
    margin = float(protocol["equivalence_margin_U"])
    if low > margin:
        decision = "assistant_practically_superior"
    elif high < -margin:
        decision = "const10_practically_superior"
    elif bool(tost["equivalent"]):
        decision = "practically_equivalent"
    elif low > 0.0:
        decision = "assistant_positive_magnitude_undetermined"
    elif high < 0.0:
        decision = "const10_positive_magnitude_undetermined"
    else:
        decision = "inconclusive"
    primary.update({
        "contrast": "assistant_minus_const10",
        "equivalence_margin_U": margin,
        "welch_tost": tost,
        "decision": decision,
        "permutation": permutation_test(
            assistant_u, const10_u, iterations, seed + 1),
    })

    conforming_assistant = [
        row for row in groups["assistant"]
        if bool(row["behavior"].get("protocol_conforming"))  # type: ignore[union-attr]
    ]
    behavior_sensitivity = {
        "scope": "secondary",
        "included_assistant_runs": len(conforming_assistant),
        "excluded_assistant_runs": len(groups["assistant"]) -
            len(conforming_assistant),
        "comparison": (
            comparison(conforming_assistant, groups["const10"],
                       iterations, seed + 20)
            if conforming_assistant else None
        ),
    }

    secondary_pairs = (
        ("const10_minus_vanilla", "const10", "vanilla"),
        ("oracle10_minus_vanilla", "oracle10", "vanilla"),
        ("assistant_minus_oracle10", "assistant", "oracle10"),
        ("assistant_minus_vanilla", "assistant", "vanilla"),
    )
    secondary = {
        name: comparison(groups[left], groups[right], iterations, seed + index)
        for index, (name, left, right) in enumerate(secondary_pairs, start=2)
    }

    arm_summary: dict[str, object] = {}
    for arm, rows in groups.items():
        metrics = [row["metrics"] for row in rows]
        fields = ["U", "pktRcvBelated", "pktRcvDropTotal",
                  "pktRetransTotal", "pktSndDropTotal", "rtt_p95_ms",
                  "sndbuf_peak_ms", "activation_count"]
        arm_summary[arm] = {
            "runs": len(rows),
            **{field: summarize([float(item[field]) for item in metrics])
               for field in fields},
            "phases": {
                phase: {
                    field: summarize([
                        float(item[f"{phase}_{field}"]) for item in metrics
                    ])
                    for field in (
                        "pktRcvBelated", "pktRcvDropTotal",
                        "pktRetransTotal", "pktSndDropTotal",
                        "rtt_p95_ms", "sndbuf_p95_ms",
                        "send_rate_mean_mbps", "maxbw_median_mbps",
                    )
                }
                for phase in PHASES
            },
        }

    assistant_behavior_fields = (
        "no_activation", "early_activation", "missing_restore",
        "unexpected_transition_count", "standard_cycle",
        "protocol_conforming",
    )
    assistant_behavior = {
        field: sum(bool(row["behavior"].get(field))  # type: ignore[union-attr]
                   for row in groups["assistant"])
        for field in assistant_behavior_fields
    }
    equivalence_statuses: dict[str, int] = defaultdict(int)
    for row in groups["assistant"]:
        equivalence_statuses[str(row["equivalence_status"])] += 1

    order_diagnostics = {}
    for arm, rows in groups.items():
        ordered = sorted(rows, key=lambda row: int(row["execution_order"]))
        xs = [float(row["execution_order"]) for row in ordered]
        ys = [float(row["metrics"]["U"]) for row in ordered]  # type: ignore[index]
        midpoint = len(ys) // 2
        order_diagnostics[arm] = {
            "slope_U_per_execution": slope(xs, ys),
            "first_half_mean_U": statistics.fmean(ys[:midpoint]),
            "second_half_mean_U": statistics.fmean(ys[midpoint:]),
        }

    paired_differences = []
    for block in range(1, expected_runs + 1):
        assistant = next(row for row in groups["assistant"]
                         if int(row["block"]) == block)
        const10 = next(row for row in groups["const10"]
                       if int(row["block"]) == block)
        paired_differences.append(
            float(assistant["metrics"]["U"]) -  # type: ignore[index]
            float(const10["metrics"]["U"]))  # type: ignore[index]
    order_diagnostics["block_paired_assistant_minus_const10"] = {
        "mean": statistics.fmean(paired_differences),
        "median": statistics.median(paired_differences),
        "positive_fraction": sum(value > 0.0 for value in paired_differences) /
            len(paired_differences),
    }

    clean = audit_clean_controls(
        Path(args.results), protocol, protocol_hash, plan_hash)
    commits = sorted({str(row["experiment_git_commit"]) for row in records})
    criteria = {
        "protocol_frozen": protocol.get("status") == "frozen_before_execution",
        "plan_complete": len(plan) == int(protocol["valid_experimental_runs"]),
        "one_valid_run_per_plan_job": progress_complete,
        "runs_per_arm": all(len(groups[arm]) == expected_runs for arm in groups),
        "single_clean_experiment_commit": len(commits) == 1 and bool(commits[0]),
        "clean_controls_accepted": clean["status"] == "accepted",
        "assistant_traceability_resolved": all(
            row["equivalence_status"] in (
                "equivalent", "reference_failure_retained",
                "not_applicable_controller_readiness_failure")
            for row in groups["assistant"]),
    }
    report: dict[str, object] = {
        "status": "accepted" if all(criteria.values()) else "rejected",
        "protocol_id": protocol["protocol_id"],
        "criteria": criteria,
        "protocol_sha256": protocol_hash,
        "plan_sha256": plan_hash,
        "experiment_git_commits": commits,
        "primary": primary,
        "secondary_descriptive": secondary,
        "behavior_sensitivity": behavior_sensitivity,
        "assistant_behavior": assistant_behavior,
        "assistant_equivalence_statuses": dict(equivalence_statuses),
        "arms": arm_summary,
        "order_sensitivity": order_diagnostics,
        "clean_controls": clean,
        "invalid_integrity_attempts": sum(
            row["kind"] == "experimental"
            and row["outcome"] == "invalid_integrity"
            for row in progress),
    }
    analyze.write_json(Path(args.output), report)

    flat_rows = []
    for row in records:
        base = {key: row[key] for key in (
            "execution_order", "block", "position", "arm", "variant",
            "run_id", "run_dir")}
        base.update(row["metrics"])  # type: ignore[arg-type]
        base.update({
            f"behavior_{key}": value
            for key, value in row["behavior"].items()  # type: ignore[union-attr]
        })
        base["equivalence_status"] = row["equivalence_status"]
        flat_rows.append(base)
    analyze.write_csv(Path(args.runs_csv), flat_rows)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", default="experiments/ohead_ablation_protocol.json")
    parser.add_argument("--plan", default="results/v2/ohead_ablation/execution_plan.csv")
    parser.add_argument("--results", default="results/v2/ohead_ablation")
    parser.add_argument("--progress", default="results/v2/ohead_ablation/progress.csv")
    parser.add_argument("--output", default="results/v2/ohead_ablation/comparison.json")
    parser.add_argument("--runs-csv", default="results/v2/ohead_ablation/runs.csv")
    parser.add_argument("--audit-run")
    parser.add_argument("--arm")
    parser.add_argument("--variant")
    parser.add_argument("--run-id")
    parser.add_argument("--equivalence")
    parser.add_argument("--equivalence-disposition")
    parser.add_argument("--controller-disposition")
    parser.add_argument("--validate-failure-disposition")
    parser.add_argument("--plan-execution-order")
    parser.add_argument("--attempt")
    parser.add_argument("--integrity-only", action="store_true")
    parser.add_argument("--audit-output")
    args = parser.parse_args()

    protocol_path = Path(args.protocol)
    protocol = json.loads(protocol_path.read_text())
    if args.validate_failure_disposition:
        if not all((args.plan_execution_order, args.attempt, args.run_id)):
            parser.error("la disposición requiere orden, intento y run-id")
        allowed = protocol["failure_disposition_policy"]["allowed_decision"]
        disposition = validated_disposition(
            Path(args.validate_failure_disposition), allowed, {
                "plan_execution_order": args.plan_execution_order,
                "attempt": args.attempt,
                "run_id": args.run_id,
            })
        report = {
            "status": "valid_disposition" if disposition
                else "invalid_disposition",
            "disposition": disposition,
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        if disposition:
            return
        raise SystemExit(2)
    plan_path = Path(args.plan)
    protocol_hash = file_hash(protocol_path)
    plan_hash = file_hash(plan_path)
    if args.audit_run:
        if not all((args.arm, args.variant, args.run_id, args.audit_output)):
            parser.error("--audit-run requiere brazo, variante, run-id y audit-output")
        report = safe_audit_run(
            Path(args.audit_run), protocol, args.arm, args.variant, args.run_id,
            protocol_hash, plan_hash,
            Path(args.equivalence) if args.equivalence else None,
            Path(args.equivalence_disposition)
                if args.equivalence_disposition else None,
            Path(args.controller_disposition)
                if args.controller_disposition else None,
            args.integrity_only)
        write_json_atomic(Path(args.audit_output), report)
    else:
        report = final_analysis(args, protocol, protocol_hash, plan_hash)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["status"] in ("valid", "valid_integrity", "accepted"):
        return
    raise SystemExit(1 if report["status"] == "invalid_integrity" else 2)


if __name__ == "__main__":
    main()
