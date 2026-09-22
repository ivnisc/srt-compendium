#!/usr/bin/env python3
"""Audita operación, costos y reproducibilidad de un screening causal V2."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path

import analyze


def natural_run_key(path: Path) -> tuple[int, str]:
    suffix = path.name.removeprefix("run_")
    return (int(suffix), suffix) if suffix.isdigit() else (10**9, suffix)


def first_time(rows: list[dict[str, str]], decision: str) -> float:
    values = [analyze.numeric(row.get("elapsed_ms"), math.nan) for row in rows
              if row.get("decision") == decision]
    return values[0] if values else math.nan


def finite_median(values: list[float]) -> float | None:
    clean = [value for value in values if math.isfinite(value)]
    return statistics.median(clean) if clean else None


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def phase_rows(rows: list[dict[str, str]], begin: float,
               end: float) -> list[dict[str, str]]:
    return [row for row in rows
            if begin <= analyze.numeric(row.get("elapsed_ms")) < end]


def active_fraction(rows: list[dict[str, str]], active_ohead: int) -> float:
    if not rows:
        return 0.0
    active = sum(int(analyze.numeric(row.get("ohead_pct"))) == active_ohead
                 for row in rows)
    return active / len(rows)


def transition_times(rows: list[dict[str, str]]) -> list[float]:
    values: list[float] = []
    previous: int | None = None
    for row in rows:
        current = int(analyze.numeric(row.get("ohead_pct")))
        if previous is not None and current != previous:
            values.append(analyze.numeric(row.get("elapsed_ms")))
        previous = current
    return values


def audit_run(run_dir: Path, b_ref: float, payload_ref: float,
              source_hash: str | None, treatment: bool) -> dict:
    run = analyze.read_key_value(run_dir / "run_metadata.csv")
    tx_meta = analyze.read_key_value(run_dir / "tx_metadata.csv")
    tx_rows = analyze.read_rows(run_dir / "tx_stats.csv")
    rx_rows = analyze.read_rows(run_dir / "rx_stats.csv")
    tx = analyze.summarize_stats(run_dir / "tx_stats.csv")
    rx = analyze.summarize_stats(run_dir / "rx_stats.csv")
    low_begin, recovery_begin = analyze.phase_bounds_ms(run_dir, run)
    run_end = analyze.numeric(run.get("duration_s"), 60.0) * 1000.0
    active_ohead = int(analyze.numeric(tx_meta.get("active_ohead"), 10.0))
    candidate_times = [analyze.numeric(row.get("elapsed_ms")) for row in tx_rows
                       if row.get("decision") == "v2_candidate"]
    activation = first_time(tx_rows, "v2_activate")
    restore = first_time(tx_rows, "v2_restore")
    changes = transition_times(tx_rows)
    before_low = phase_rows(tx_rows, 0.0, low_begin)
    low = phase_rows(tx_rows, low_begin, recovery_begin)
    recovery_after_guard = phase_rows(
        tx_rows, recovery_begin + 1000.0, run_end)
    candidate_rows = [row for row in tx_rows
                      if row.get("decision") == "v2_candidate"]
    activation_rows = [row for row in tx_rows
                       if row.get("decision") == "v2_activate"]
    active_rows = [row for row in tx_rows
                   if int(analyze.numeric(row.get("ohead_pct"))) == active_ohead]
    expected_maxbw = analyze.numeric(tx_meta.get("input_rate_bps")) / 1e6 * (
        1.0 + active_ohead / 100.0)
    observed_maxbw = analyze.median([
        analyze.numeric(row.get("mbpsMaxBW")) for row in active_rows
        if analyze.numeric(row.get("mbpsMaxBW")) > 0.0
    ])
    maxbw_error = (
        abs(observed_maxbw - expected_maxbw) / expected_maxbw
        if expected_maxbw > 0.0 and active_rows else math.inf
    )
    payload = analyze.numeric(tx_meta.get("payload_bytes_read"))
    unique_received = analyze.numeric(rx.get("byteRecvUniqueTotal"))
    record = {
        "variant": run.get("variant", run_dir.parent.name),
        "run_id": run.get("run_id", run_dir.name.removeprefix("run_")),
        "run_dir": str(run_dir),
        "protocol_version": run.get("protocol_version", ""),
        "input_rate_bps": run.get("input_rate_bps", ""),
        "duration_s": run.get("duration_s", ""),
        "phase_high_s": run.get("phase_high_s", ""),
        "phase_low_s": run.get("phase_low_s", ""),
        "latency_ms": run.get("latency_ms", ""),
        "delay_ms": run.get("delay_ms", ""),
        "loss_percent": run.get("loss_percent", ""),
        "reorder_percent_effective": run.get("reorder_percent_effective", ""),
        "queue_limit_packets": run.get("queue_limit_packets", ""),
        "high_capacity_percent": run.get("high_capacity_percent", ""),
        "low_capacity_percent": run.get("low_capacity_percent", ""),
        "sender_cpu": run.get("sender_cpu", ""),
        "receiver_cpu": run.get("receiver_cpu", ""),
        "experiment_binary_sha256": run.get("experiment_binary_sha256", ""),
        "source_sha256": run.get("source_sha256", ""),
        "srt_git_commit": run.get("srt_git_commit", ""),
        "controller_selection_sha256": run.get(
            "controller_selection_sha256", ""),
        "controller_config_sha256": run.get("controller_config_sha256", ""),
        "U": unique_received / b_ref if b_ref else 0.0,
        "payload_bytes_read": int(payload),
        "source_valid": (
            abs(payload - payload_ref) < 0.5
            and bool(source_hash)
            and run.get("source_sha256") == source_hash
        ),
        "low_begin_ms": low_begin,
        "recovery_begin_ms": recovery_begin,
        "candidate_count": len(candidate_times),
        "pretransition_candidate_count": sum(
            value < low_begin for value in candidate_times),
        "first_candidate_relative_low_ms": (
            candidate_times[0] - low_begin if candidate_times else None),
        "activation_relative_low_ms": (
            activation - low_begin if math.isfinite(activation) else None),
        "restore_relative_recovery_ms": (
            restore - recovery_begin if math.isfinite(restore) else None),
        "actual_ohead_transition_count": len(changes),
        "controller_changes_metadata": int(analyze.numeric(
            tx_meta.get("controller_changes"))),
        "pretransition_active_fraction": active_fraction(
            before_low, active_ohead),
        "low_active_fraction": active_fraction(low, active_ohead),
        "recovery_active_fraction_after_1s": active_fraction(
            recovery_after_guard, active_ohead),
        "candidate_kept_nominal": all(
            int(analyze.numeric(row.get("ohead_pct"))) == 25
            for row in candidate_rows),
        "activation_was_confirmed": bool(activation_rows) and all(
            int(analyze.numeric(row.get("v2_confirmed"))) == 1
            for row in activation_rows),
        "final_ohead": int(analyze.numeric(tx_meta.get("final_ohead"))),
        "v2_confirmed_metadata": int(analyze.numeric(
            tx_meta.get("v2_confirmed"))),
        "active_mbpsMaxBW_expected": expected_maxbw if treatment else None,
        "active_mbpsMaxBW_median": observed_maxbw if treatment else None,
        "active_mbpsMaxBW_relative_error": maxbw_error if treatment else None,
        "pktSndDropTotal": int(analyze.numeric(tx.get("pktSndDropTotal"))),
        "pktRcvDropTotal": int(analyze.numeric(rx.get("pktRcvDropTotal"))),
        "pktRcvBelated_sum": int(analyze.numeric(rx.get("pktRcvBelated_sum"))),
        "pktRetransTotal": int(analyze.numeric(tx.get("pktRetransTotal"))),
        "snd_buffer_peak_ms": analyze.numeric(tx.get("snd_buffer_peak_ms")),
        "snd_buffer_tail_median_ms": analyze.numeric(
            tx.get("snd_buffer_tail_median_ms")),
        "snd_buffer_tail_slope_ms_s": analyze.numeric(
            tx.get("snd_buffer_tail_slope_ms_s")),
        "rtt_p95_ms": analyze.numeric(tx.get("rtt_p95_ms")),
        "v2_eval_p99_us": analyze.numeric(tx.get("v2_eval_p99_us")),
        "v2_eval_max_us": analyze.numeric(tx.get("v2_eval_max_us")),
    }
    if not treatment:
        for field in (
            "candidate_kept_nominal", "activation_was_confirmed",
            "v2_confirmed_metadata", "pretransition_active_fraction",
            "low_active_fraction", "recovery_active_fraction_after_1s",
        ):
            record[field] = None
    return record


def summarize_group(rows: list[dict]) -> dict:
    numeric_fields = (
        "U", "pktSndDropTotal", "pktRcvDropTotal", "pktRcvBelated_sum",
        "pktRetransTotal", "snd_buffer_peak_ms", "snd_buffer_tail_median_ms",
        "snd_buffer_tail_slope_ms_s", "rtt_p95_ms",
    )
    result: dict[str, object] = {"runs": len(rows)}
    for field in numeric_fields:
        values = [float(row[field]) for row in rows]
        result[f"mean_{field}"] = analyze.mean(values)
        result[f"median_{field}"] = analyze.median(values)
    return result


def load_equivalence(results: Path, variant: str,
                     run_dirs: list[Path]) -> tuple[list[dict], list[str]]:
    reports = []
    missing = []
    for run_dir in run_dirs:
        path = results / "equivalence" / f"{variant}_{run_dir.name}.json"
        if path.exists():
            reports.append(json.loads(path.read_text()))
        else:
            missing.append(str(path))
    return reports, missing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results/v2")
    parser.add_argument("--label", required=True)
    parser.add_argument("--reference-variant", required=True)
    parser.add_argument("--treatment-variant", required=True)
    parser.add_argument("--minimum-runs", type=int, default=10)
    parser.add_argument("--maximum-recovery-active-fraction", type=float,
                        default=0.10)
    parser.add_argument("--maximum-runtime-p99-us", type=float, default=1000.0)
    parser.add_argument("--maximum-maxbw-relative-error", type=float,
                        default=0.03)
    parser.add_argument("--controller-config", default="")
    args = parser.parse_args()

    results = Path(args.results)
    baseline = json.loads((results / "baseline.json").read_text())
    comparison = json.loads(
        (results / f"{args.label}_comparison.json").read_text())
    host = json.loads((results / "host_validation.json").read_text())
    reference_dirs = sorted(
        (results / "dynamic" / args.reference_variant).glob("run_*"),
        key=natural_run_key)
    treatment_dirs = sorted(
        (results / "dynamic" / args.treatment_variant).glob("run_*"),
        key=natural_run_key)
    reference = [audit_run(path, float(baseline["B_ref"]),
                           float(baseline["payload_ref"]),
                           baseline.get("source_sha256"), False)
                 for path in reference_dirs if (path / "run_metadata.csv").exists()]
    treatment = [audit_run(path, float(baseline["B_ref"]),
                           float(baseline["payload_ref"]),
                           baseline.get("source_sha256"), True)
                 for path in treatment_dirs if (path / "run_metadata.csv").exists()]
    equivalence, missing_equivalence = load_equivalence(
        results, args.treatment_variant, treatment_dirs)
    clean_names = {Path(row.get("run", "")).name
                   for row in host.get("per_run", [])}
    signature_fields = (
        "protocol_version", "input_rate_bps", "duration_s", "phase_high_s",
        "phase_low_s", "latency_ms", "delay_ms", "loss_percent",
        "reorder_percent_effective", "queue_limit_packets",
        "high_capacity_percent", "low_capacity_percent", "sender_cpu",
        "receiver_cpu", "experiment_binary_sha256", "source_sha256",
        "srt_git_commit",
    )
    signature_values = {
        field: sorted({str(row[field]) for row in reference + treatment})
        for field in signature_fields
    }

    criteria = {
        "minimum_runs": (
            len(reference) >= args.minimum_runs
            and len(treatment) >= args.minimum_runs),
        "primary_effect_positive": (
            comparison.get("status") == "supported"
            and float(comparison.get("ci95_low", -math.inf)) > 0.0),
        "source_and_payload_constant": all(
            row["source_valid"] for row in reference + treatment),
        "experimental_signature_constant": all(
            len(values) == 1 and values[0] for values in signature_values.values()),
        "host_controls_accepted": (
            host.get("status") == "accepted"
            and f"run_{args.label}_pre" in clean_names
            and f"run_{args.label}_post" in clean_names),
        "python_cpp_equivalent_all_runs": (
            len(equivalence) == len(treatment)
            and not missing_equivalence
            and all(report.get("status") == "equivalent"
                    for report in equivalence)),
        "candidate_never_actuated": all(
            row["candidate_kept_nominal"] for row in treatment),
        "no_pretransition_actuation": all(
            row["pretransition_active_fraction"] == 0.0 for row in treatment),
        "activated_after_capacity_reduction": all(
            row["activation_relative_low_ms"] is not None
            and row["activation_relative_low_ms"] >= 0.0
            for row in treatment),
        "activation_confirmed": all(
            row["activation_was_confirmed"]
            and row["v2_confirmed_metadata"] == 1 for row in treatment),
        "exactly_two_ohead_changes": all(
            row["actual_ohead_transition_count"] == 2
            and row["controller_changes_metadata"] == 2
            for row in treatment),
        "returned_to_nominal": all(
            row["final_ohead"] == 25 for row in treatment),
        "recovery_activity_bounded": all(
            row["recovery_active_fraction_after_1s"] <=
            args.maximum_recovery_active_fraction for row in treatment),
        "zero_sender_drops": all(
            row["pktSndDropTotal"] == 0 for row in treatment),
        "maxbw_matches_actuator": all(
            row["active_mbpsMaxBW_relative_error"] <=
            args.maximum_maxbw_relative_error for row in treatment),
        "controller_runtime_bounded": all(
            row["v2_eval_p99_us"] <= args.maximum_runtime_p99_us
            for row in treatment),
    }
    controller_integrity: dict[str, object] = {}
    if args.controller_config:
        controller_path = Path(args.controller_config)
        frozen = read_env(controller_path)
        expected_config_hash = hashlib.sha256(
            controller_path.read_bytes()).hexdigest()
        expected_selection_hash = frozen.get("V2_SELECTION_SHA256", "")
        observed_config_hashes = sorted({
            str(row["controller_config_sha256"]) for row in treatment})
        observed_selection_hashes = sorted({
            str(row["controller_selection_sha256"]) for row in treatment})
        criteria["frozen_controller_hash_matches"] = (
            observed_config_hashes == [expected_config_hash])
        criteria["frozen_selection_hash_matches"] = (
            bool(expected_selection_hash)
            and observed_selection_hashes == [expected_selection_hash])
        controller_integrity = {
            "controller_path": str(controller_path),
            "expected_config_sha256": expected_config_hash,
            "observed_config_sha256": observed_config_hashes,
            "expected_selection_sha256": expected_selection_hash,
            "observed_selection_sha256": observed_selection_hashes,
        }
    passed = all(criteria.values())
    accepted_status = (
        "confirmed" if args.label == "final_ab" else "ready_to_freeze")
    report = {
        "status": accepted_status if passed else "not_ready",
        "label": args.label,
        "reference_variant": args.reference_variant,
        "treatment_variant": args.treatment_variant,
        "criteria": criteria,
        "limits": {
            "minimum_runs_per_variant": args.minimum_runs,
            "maximum_recovery_active_fraction_after_1s":
                args.maximum_recovery_active_fraction,
            "maximum_runtime_p99_us": args.maximum_runtime_p99_us,
            "maximum_maxbw_relative_error":
                args.maximum_maxbw_relative_error,
        },
        "primary": {
            "delta_U": comparison.get("delta_U"),
            "ci95_low": comparison.get("ci95_low"),
            "ci95_high": comparison.get("ci95_high"),
        },
        "reference": summarize_group(reference),
        "treatment": summarize_group(treatment),
        "controller": {
            "pretransition_candidates_total": sum(
                row["pretransition_candidate_count"] for row in treatment),
            "activation_delay_from_low_median_ms": finite_median([
                float(row["activation_relative_low_ms"]) for row in treatment
                if row["activation_relative_low_ms"] is not None]),
            "activation_delay_from_low_max_ms": max(
                (float(row["activation_relative_low_ms"]) for row in treatment
                 if row["activation_relative_low_ms"] is not None),
                default=None),
            "restore_delay_from_recovery_median_ms": finite_median([
                float(row["restore_relative_recovery_ms"]) for row in treatment
                if row["restore_relative_recovery_ms"] is not None]),
            "recovery_active_fraction_after_1s_median": analyze.median([
                float(row["recovery_active_fraction_after_1s"])
                for row in treatment]),
            "cpp_eval_p99_us_max": max(
                (float(row["v2_eval_p99_us"]) for row in treatment),
                default=None),
            "cpp_eval_max_us_max": max(
                (float(row["v2_eval_max_us"]) for row in treatment),
                default=None),
            "maxbw_relative_error_max": max(
                (float(row["active_mbpsMaxBW_relative_error"])
                 for row in treatment), default=None),
        },
        "integrity": {
            "signature_values": signature_values,
            "controller": controller_integrity,
        },
        "equivalence_reports": len(equivalence),
        "missing_equivalence_reports": missing_equivalence,
        "runs": reference + treatment,
    }
    output = results / f"{args.label}_audit.json"
    csv_output = results / f"{args.label}_audit_runs.csv"
    analyze.write_json(output, report)
    analyze.write_csv(csv_output, reference + treatment)
    print(json.dumps(report, indent=2, sort_keys=True))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
