#!/usr/bin/env python3
"""Auditoría descriptiva y reproducible de trazas de la matriz de red V2."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

import analyze


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def truth(value: str | None) -> bool:
    return value == "True"


def run_number(row: dict[str, str]) -> int:
    return int(row["run_id"])


def closest_to_median(rows: list[dict[str, str]]) -> dict[str, str]:
    target = statistics.median(float(row["U"]) for row in rows)
    return min(rows, key=lambda row: (abs(float(row["U"]) - target), run_number(row)))


def select_runs(rows: list[dict[str, str]], center: str,
                boundary: str) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["condition"], row["arm"])].append(row)
    for group in grouped.values():
        group.sort(key=run_number)

    selected: dict[tuple[str, str, str], dict] = {}

    def add(reason: str, row: dict[str, str]) -> None:
        key = (row["condition"], row["arm"], row["run_id"])
        if key not in selected:
            selected[key] = {"row": row, "reasons": []}
        selected[key]["reasons"].append(reason)

    for arm in ("vanilla", "assistant"):
        add("center_nearest_median_U", closest_to_median(grouped[(center, arm)]))
        boundary_rows = grouped[(boundary, arm)]
        add("boundary_nearest_median_U", closest_to_median(boundary_rows))
        add("boundary_minimum_U", min(
            boundary_rows, key=lambda row: (float(row["U"]), run_number(row))))
        add("boundary_maximum_U", max(
            boundary_rows, key=lambda row: (float(row["U"]), -run_number(row))))

    vanilla_boundary = grouped[(boundary, "vanilla")]
    recovered = [row for row in vanilla_boundary if truth(row["recovered"])]
    not_recovered = [row for row in vanilla_boundary if not truth(row["recovered"])]
    add("boundary_vanilla_recovered_nearest_median_U",
        closest_to_median(recovered))
    add("boundary_vanilla_not_recovered_nearest_median_U",
        closest_to_median(not_recovered))

    return list(selected.values())


def first_decision(rows: list[dict[str, str]], decision: str) -> float | None:
    for row in rows:
        if row.get("decision") == decision:
            return analyze.numeric(row.get("elapsed_ms"))
    return None


def last_decision_before(rows: list[dict[str, str]], decision: str,
                         limit_ms: float | None) -> float | None:
    values = [analyze.numeric(row.get("elapsed_ms")) for row in rows
              if row.get("decision") == decision
              and (limit_ms is None
                   or analyze.numeric(row.get("elapsed_ms")) <= limit_ms)]
    return values[-1] if values else None


def first_positive(rows: list[dict[str, str]], field: str, begin_ms: float,
                   end_ms: float) -> float | None:
    for row in rows:
        elapsed = analyze.numeric(row.get("elapsed_ms"))
        if begin_ms <= elapsed < end_ms and analyze.numeric(row.get(field)) > 0.0:
            return elapsed
    return None


def phase_name(elapsed_ms: float, low_begin_ms: float,
               recovery_begin_ms: float) -> str:
    if elapsed_ms < low_begin_ms:
        return "high"
    if elapsed_ms < recovery_begin_ms:
        return "low"
    return "recovery"


def window(rows: list[dict[str, str]], begin_ms: float,
           end_ms: float) -> list[dict[str, str]]:
    return [row for row in rows
            if begin_ms <= analyze.numeric(row.get("elapsed_ms")) < end_ms]


def sum_field(rows: list[dict[str, str]], field: str) -> float:
    return sum(analyze.numeric(row.get(field)) for row in rows)


def event_window(rows: list[dict[str, str]], begin_ms: float,
                 end_ms: float) -> dict:
    subset = window(rows, begin_ms, end_ms)
    unique = sum_field(subset, "pktRecvUnique")
    belated = sum_field(subset, "pktRcvBelated")
    dropped = sum_field(subset, "pktRcvDrop")
    return {
        "begin_ms": begin_ms,
        "end_ms": end_ms,
        "samples": len(subset),
        "pktRecvUnique": unique,
        "pktRcvBelated": belated,
        "pktRcvDrop": dropped,
        "belated_fraction": belated / unique if unique else 0.0,
        "drop_fraction": dropped / unique if unique else 0.0,
    }


def peak(rows: list[dict[str, str]], field: str, low_begin_ms: float,
         recovery_begin_ms: float) -> dict:
    eligible = [row for row in rows if row.get(field) not in (None, "")]
    if not eligible:
        return {"value": None, "elapsed_ms": None, "phase": None}
    row = max(eligible, key=lambda item: analyze.numeric(item.get(field)))
    elapsed = analyze.numeric(row.get("elapsed_ms"))
    return {
        "value": analyze.numeric(row.get(field)),
        "elapsed_ms": elapsed,
        "phase": phase_name(elapsed, low_begin_ms, recovery_begin_ms),
    }


def trace_bins(identifier: str, tx: list[dict[str, str]],
               rx: list[dict[str, str]], low_begin_ms: float,
               recovery_begin_ms: float, duration_ms: float) -> list[dict]:
    result: list[dict] = []
    for second in range(math.ceil(duration_ms / 1000.0)):
        begin = second * 1000.0
        end = min((second + 1) * 1000.0, duration_ms)
        tx_bin = window(tx, begin, end)
        rx_bin = window(rx, begin, end)
        unique = sum_field(rx_bin, "pktRecvUnique")
        belated = sum_field(rx_bin, "pktRcvBelated")
        dropped = sum_field(rx_bin, "pktRcvDrop")

        def values(rows: list[dict[str, str]], field: str) -> list[float]:
            return [analyze.numeric(row.get(field)) for row in rows
                    if row.get(field) not in (None, "")]

        rtt = values(tx_bin, "msRTT")
        sndbuf = values(tx_bin, "msSndBuf")
        score = values(tx_bin, "v2_score")
        ohead = values(tx_bin, "ohead_pct")
        result.append({
            "trace": identifier,
            "second": second,
            "phase": phase_name(begin + 500.0, low_begin_ms, recovery_begin_ms),
            "ohead_median_pct": analyze.median(ohead),
            "rtt_mean_ms": analyze.mean(rtt),
            "rtt_p95_ms": analyze.percentile(rtt, 0.95),
            "sndbuf_mean_ms": analyze.mean(sndbuf),
            "sndbuf_p95_ms": analyze.percentile(sndbuf, 0.95),
            "score_max": max(score) if score else 0.0,
            "pktRetrans": sum_field(tx_bin, "pktRetrans"),
            "pktRecvUnique": unique,
            "pktRcvBelated": belated,
            "pktRcvDrop": dropped,
            "belated_fraction": belated / unique if unique else 0.0,
            "drop_fraction": dropped / unique if unique else 0.0,
        })
    return result


def audit_trace(results: Path, item: dict) -> tuple[dict, list[dict]]:
    row = item["row"]
    condition, arm, run_id = row["condition"], row["arm"], row["run_id"]
    run_dir = results / "dynamic" / f"network_{condition}_{arm}" / f"run_{run_id}"
    tx_path, rx_path = run_dir / "tx_stats.csv", run_dir / "rx_stats.csv"
    metadata_path = run_dir / "run_metadata.csv"
    events_path = run_dir / "network_events.csv"
    tx, rx = analyze.read_rows(tx_path), analyze.read_rows(rx_path)
    metadata = analyze.read_key_value(metadata_path)
    low_begin, recovery_begin = analyze.phase_bounds_ms(run_dir, metadata)
    duration_ms = analyze.numeric(metadata.get("duration_s"), 60.0) * 1000.0
    activation = first_decision(tx, "v2_activate")
    candidate = last_decision_before(tx, "v2_candidate", activation)
    restore = first_decision(tx, "v2_restore")
    identifier = f"{condition}_{arm}_run_{run_id}"

    periods = {
        "high": event_window(rx, 3000.0, low_begin),
        "low": event_window(rx, low_begin, recovery_begin),
        "recovery": event_window(rx, recovery_begin, duration_ms),
    }
    if activation is not None:
        periods["low_before_activation"] = event_window(
            rx, low_begin, min(activation, recovery_begin))
        periods["low_after_activation"] = event_window(
            rx, max(activation, low_begin), recovery_begin)

    decisions = [{
        "elapsed_ms": analyze.numeric(sample.get("elapsed_ms")),
        "decision": sample.get("decision"),
        "ohead_pct": int(analyze.numeric(sample.get("ohead_pct"))),
        "score": analyze.numeric(sample.get("v2_score")),
        "msRTT": analyze.numeric(sample.get("msRTT")),
        "msSndBuf": analyze.numeric(sample.get("msSndBuf")),
    } for sample in tx if sample.get("decision") in {
        "v2_candidate", "v2_activate", "v2_restore", "v2_candidate_timeout"
    }]

    bins = trace_bins(identifier, tx, rx, low_begin, recovery_begin, duration_ms)
    most_belated = max(bins, key=lambda value: value["pktRcvBelated"])
    most_dropped = max(bins, key=lambda value: value["pktRcvDrop"])
    most_retrans = max(bins, key=lambda value: value["pktRetrans"])
    report = {
        "trace": identifier,
        "selection_reasons": item["reasons"],
        "condition": condition,
        "arm": arm,
        "run_id": run_id,
        "U": float(row["U"]),
        "high_stable": truth(row["high_stable"]),
        "low_degraded": truth(row["low_degraded"]),
        "recovered": truth(row["recovered"]),
        "low_begin_ms": low_begin,
        "recovery_begin_ms": recovery_begin,
        "candidate_ms": candidate,
        "activation_ms": activation,
        "activation_relative_low_ms": (
            activation - low_begin if activation is not None else None),
        "restore_ms": restore,
        "restore_relative_recovery_ms": (
            restore - recovery_begin if restore is not None else None),
        "decision_events": decisions,
        "periods": periods,
        "peaks": {
            "rtt": peak(tx, "msRTT", low_begin, recovery_begin),
            "sender_buffer": peak(tx, "msSndBuf", low_begin, recovery_begin),
            "score": peak(tx, "v2_score", low_begin, recovery_begin),
            "one_second_belated": most_belated,
            "one_second_drop": most_dropped,
            "one_second_retransmission": most_retrans,
        },
        "source_files_sha256": {
            "tx_stats.csv": file_hash(tx_path),
            "rx_stats.csv": file_hash(rx_path),
            "run_metadata.csv": file_hash(metadata_path),
            "network_events.csv": file_hash(events_path),
        },
    }
    return report, bins


def aggregate_assistant_timing(results: Path,
                               rows: list[dict[str, str]]) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["arm"] != "assistant":
            continue
        condition, run_id = row["condition"], row["run_id"]
        run_dir = (results / "dynamic" / f"network_{condition}_assistant"
                   / f"run_{run_id}")
        tx = analyze.read_rows(run_dir / "tx_stats.csv")
        rx = analyze.read_rows(run_dir / "rx_stats.csv")
        metadata = analyze.read_key_value(run_dir / "run_metadata.csv")
        low_begin, recovery_begin = analyze.phase_bounds_ms(run_dir, metadata)
        activation = first_decision(tx, "v2_activate")
        candidate = last_decision_before(tx, "v2_candidate", activation)
        restore = first_decision(tx, "v2_restore")
        first_belated = first_positive(
            rx, "pktRcvBelated", low_begin, recovery_begin)
        first_drop = first_positive(rx, "pktRcvDrop", low_begin, recovery_begin)
        low = event_window(rx, low_begin, recovery_begin)
        before = event_window(rx, low_begin, activation or recovery_begin)
        after = event_window(rx, activation or recovery_begin, recovery_begin)
        grouped[condition].append({
            "candidate_delay_ms": (
                candidate - low_begin if candidate is not None else None),
            "activation_delay_ms": (
                activation - low_begin if activation is not None else None),
            "confirmation_delay_ms": (
                activation - candidate
                if activation is not None and candidate is not None else None),
            "restore_delay_ms": (
                restore - recovery_begin if restore is not None else None),
            "first_belated_delay_ms": (
                first_belated - low_begin if first_belated is not None else None),
            "first_drop_delay_ms": (
                first_drop - low_begin if first_drop is not None else None),
            "activation_lead_first_belated_ms": (
                first_belated - activation
                if activation is not None and first_belated is not None else None),
            "activation_lead_first_drop_ms": (
                first_drop - activation
                if activation is not None and first_drop is not None else None),
            "belated_after_activation_fraction": (
                after["pktRcvBelated"] / low["pktRcvBelated"]
                if low["pktRcvBelated"] else 0.0),
            "drop_after_activation_fraction": (
                after["pktRcvDrop"] / low["pktRcvDrop"]
                if low["pktRcvDrop"] else 0.0),
            "belated_before_activation": before["pktRcvBelated"],
            "drop_before_activation": before["pktRcvDrop"],
        })

    output = {}
    for condition, records in sorted(grouped.items()):
        summary = {
            "runs": len(records),
            "activation_before_or_at_first_belated_runs": sum(
                record["activation_lead_first_belated_ms"] is not None
                and record["activation_lead_first_belated_ms"] >= 0.0
                for record in records),
            "activation_before_or_at_first_drop_runs": sum(
                record["activation_lead_first_drop_ms"] is not None
                and record["activation_lead_first_drop_ms"] >= 0.0
                for record in records),
        }
        for field in records[0]:
            values = [float(record[field]) for record in records
                      if record[field] is not None]
            summary[field] = {
                "median": analyze.median(values),
                "minimum": min(values),
                "maximum": max(values),
            }
        output[condition] = summary
    return output


def aggregate_boundary_recovery(results: Path, rows: list[dict[str, str]],
                                boundary: str) -> dict:
    output = {}
    for arm in ("vanilla", "assistant"):
        records = []
        for row in rows:
            if row["condition"] != boundary or row["arm"] != arm:
                continue
            run_dir = (results / "dynamic" / f"network_{boundary}_{arm}"
                       / f"run_{row['run_id']}")
            tx = analyze.read_rows(run_dir / "tx_stats.csv")
            rx = analyze.read_rows(run_dir / "rx_stats.csv")
            metadata = analyze.read_key_value(run_dir / "run_metadata.csv")
            low_begin, recovery_begin = analyze.phase_bounds_ms(run_dir, metadata)
            duration_ms = analyze.numeric(metadata.get("duration_s"), 60.0) * 1000.0
            guard_ms = max(500.0, 2.0 * analyze.numeric(
                metadata.get("sample_ms"), 100.0))
            high = window(tx, 3000.0, low_begin - guard_ms)
            recovery = window(tx, recovery_begin + guard_ms,
                              duration_ms - guard_ms)
            tail_begin = max(recovery_begin + guard_ms, duration_ms - 5000.0)
            tail = window(tx, tail_begin, duration_ms - guard_ms)
            tail_rx = window(rx, tail_begin, duration_ms - guard_ms)

            def p95(samples: list[dict[str, str]], field: str,
                    positive: bool = False) -> float:
                values = [analyze.numeric(sample.get(field)) for sample in samples
                          if sample.get(field) not in (None, "")
                          and (not positive
                               or analyze.numeric(sample.get(field)) > 0.0)]
                return analyze.percentile(values, 0.95)

            high_rtt, recovery_rtt = p95(high, "msRTT", True), p95(
                recovery, "msRTT", True)
            tail_rtt = p95(tail, "msRTT", True)
            high_buffer, recovery_buffer = p95(high, "msSndBuf"), p95(
                recovery, "msSndBuf")
            tail_buffer = p95(tail, "msSndBuf")
            records.append({
                "run_id": row["run_id"],
                "gate_recovered": truth(row["recovered"]),
                "high_rtt_p95_ms": high_rtt,
                "recovery_rtt_p95_ms": recovery_rtt,
                "tail5_rtt_p95_ms": tail_rtt,
                "recovery_minus_high_rtt_p95_ms": recovery_rtt - high_rtt,
                "tail5_minus_high_rtt_p95_ms": tail_rtt - high_rtt,
                "high_sndbuf_p95_ms": high_buffer,
                "recovery_sndbuf_p95_ms": recovery_buffer,
                "tail5_sndbuf_p95_ms": tail_buffer,
                "recovery_minus_high_sndbuf_p95_ms": (
                    recovery_buffer - high_buffer),
                "tail5_minus_high_sndbuf_p95_ms": tail_buffer - high_buffer,
                "tail5_pktRcvBelated": sum_field(tail_rx, "pktRcvBelated"),
                "tail5_pktRcvDrop": sum_field(tail_rx, "pktRcvDrop"),
            })
        summary = {
            "runs": len(records),
            "gate_recovered_runs": sum(
                record["gate_recovered"] for record in records),
            "tail5_rtt_within_5ms_of_high_runs": sum(
                record["tail5_minus_high_rtt_p95_ms"] <= 5.0
                for record in records),
            "tail5_buffer_within_20ms_of_high_runs": sum(
                record["tail5_minus_high_sndbuf_p95_ms"] <= 20.0
                for record in records),
            "tail5_both_within_gate_margins_runs": sum(
                record["tail5_minus_high_rtt_p95_ms"] <= 5.0
                and record["tail5_minus_high_sndbuf_p95_ms"] <= 20.0
                for record in records),
        }
        for field in (
                "recovery_minus_high_rtt_p95_ms",
                "tail5_minus_high_rtt_p95_ms",
                "recovery_minus_high_sndbuf_p95_ms",
                "tail5_minus_high_sndbuf_p95_ms",
                "tail5_pktRcvBelated", "tail5_pktRcvDrop"):
            values = [float(record[field]) for record in records]
            summary[field] = {
                "median": analyze.median(values),
                "minimum": min(values),
                "maximum": max(values),
            }
        summary["runs_detail"] = records
        output[arm] = summary
    return output


def compare_replications(previous_rows: list[dict[str, str]],
                         current_rows: list[dict[str, str]],
                         previous_condition: str, current_condition: str,
                         iterations: int, seed: int) -> dict:
    def values(rows: list[dict[str, str]], condition: str,
               arm: str) -> list[float]:
        return [float(row["U"]) for row in rows
                if row["condition"] == condition and row["arm"] == arm]

    old_a = values(previous_rows, previous_condition, "assistant")
    old_v = values(previous_rows, previous_condition, "vanilla")
    new_a = values(current_rows, current_condition, "assistant")
    new_v = values(current_rows, current_condition, "vanilla")
    if not all((old_a, old_v, new_a, new_v)):
        raise SystemExit("Faltan brazos para comparar las réplicas centrales")

    old_delta = analyze.mean(old_a) - analyze.mean(old_v)
    new_delta = analyze.mean(new_a) - analyze.mean(new_v)
    observed = new_delta - old_delta
    rng = random.Random(seed)
    samples = []
    for _ in range(iterations):
        sampled_old = analyze.mean([rng.choice(old_a) for _ in old_a]) - analyze.mean(
            [rng.choice(old_v) for _ in old_v])
        sampled_new = analyze.mean([rng.choice(new_a) for _ in new_a]) - analyze.mean(
            [rng.choice(new_v) for _ in new_v])
        samples.append(sampled_new - sampled_old)
    low = analyze.percentile(samples, 0.025)
    high = analyze.percentile(samples, 0.975)
    return {
        "design": "independent_difference_of_differences_bootstrap",
        "iterations": iterations,
        "seed": seed,
        "previous_condition": previous_condition,
        "current_condition": current_condition,
        "runs_per_arm_previous": len(old_a),
        "runs_per_arm_current": len(new_a),
        "previous_delta_U": old_delta,
        "current_delta_U": new_delta,
        "difference_of_delta_U": observed,
        "ci95_low": low,
        "ci95_high": high,
        "interpretation": (
            "increase_supported" if low > 0.0 else
            "decrease_supported" if high < 0.0 else
            "no_detectable_change"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results/v2/network_matrix")
    parser.add_argument(
        "--summary-csv", default="results/v2/network_matrix/condition_runs.csv")
    parser.add_argument(
        "--previous-summary-csv",
        default="results/v2/robustness/condition_runs.csv")
    parser.add_argument("--center-condition", default="d45_l1")
    parser.add_argument("--boundary-condition", default="d60_l2")
    parser.add_argument(
        "--output", default="results/v2/network_matrix/trace_audit.json")
    parser.add_argument(
        "--bins-csv", default="results/v2/network_matrix/trace_audit_bins.csv")
    parser.add_argument("--replication-bootstrap", type=int, default=10000)
    parser.add_argument("--replication-seed", type=int, default=20260916)
    args = parser.parse_args()

    results, summary_path = Path(args.results), Path(args.summary_csv)
    previous_summary_path = Path(args.previous_summary_csv)
    rows = read_csv(summary_path)
    previous_rows = read_csv(previous_summary_path)
    selected = select_runs(rows, args.center_condition, args.boundary_condition)
    traces, bins = [], []
    for item in selected:
        report, trace_bins_rows = audit_trace(results, item)
        traces.append(report)
        bins.extend(trace_bins_rows)

    output = {
        "status": "post_hoc_descriptive_audit",
        "primary_inference_unchanged": True,
        "selection_protocol": {
            "fixed_before_trace_inspection": True,
            "center_condition": args.center_condition,
            "boundary_condition": args.boundary_condition,
            "criteria": [
                "nearest median U by arm in center and boundary",
                "minimum and maximum U by arm in boundary",
                "nearest median U among recovered and non-recovered boundary vanilla runs",
            ],
            "tie_break": "lowest numeric run_id",
            "selected_unique_runs": len(traces),
        },
        "inputs_sha256": {
            "condition_runs.csv": file_hash(summary_path),
            "previous_condition_runs.csv": file_hash(previous_summary_path),
            "audit_script": file_hash(Path(__file__)),
        },
        "center_replication_comparison": compare_replications(
            previous_rows, rows, "b85_c104", args.center_condition,
            args.replication_bootstrap, args.replication_seed),
        "all_assistant_timing": aggregate_assistant_timing(results, rows),
        "boundary_recovery_audit": aggregate_boundary_recovery(
            results, rows, args.boundary_condition),
        "selected_traces": traces,
    }
    analyze.write_json(Path(args.output), output)
    analyze.write_csv(Path(args.bins_csv), bins)
    print(json.dumps({
        "status": output["status"],
        "selected_unique_runs": len(traces),
        "assistant_runs_scanned": sum(
            value["runs"] for value in output["all_assistant_timing"].values()),
        "output": args.output,
        "bins_csv": args.bins_csv,
    }, indent=2))


if __name__ == "__main__":
    main()
