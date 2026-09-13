#!/usr/bin/env python3
"""Análisis reproducible del experimento SRT sin dependencias externas."""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Sequence


def numeric(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else 0.0


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * max(0.0, min(1.0, probability))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def coefficient_of_variation(values: Sequence[float]) -> float:
    if len(values) < 2 or mean(values) == 0.0:
        return 0.0
    return statistics.stdev(values) / mean(values)


def linear_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return 0.0
    x_bar, y_bar = mean(xs), mean(ys)
    denominator = sum((x - x_bar) ** 2 for x in xs)
    if denominator == 0.0:
        return 0.0
    return sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)) / denominator


def read_key_value(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(newline="") as stream:
        return {row["key"]: row["value"] for row in csv.DictReader(stream)}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def phase_bounds_ms(run_dir: Path,
                    metadata: dict[str, str] | None = None) -> tuple[float, float]:
    """Devuelve inicio y fin efectivos de la fase reducida."""
    metadata = metadata or read_key_value(run_dir / "run_metadata.csv")
    nominal_start = numeric(metadata.get("phase_high_s"), 15.0) * 1000.0
    nominal_end = nominal_start + numeric(
        metadata.get("phase_low_s"), 30.0) * 1000.0
    events_path = run_dir / "network_events.csv"
    if not events_path.exists():
        return nominal_start, nominal_end
    events = read_rows(events_path)
    elapsed_by_phase = {
        row.get("phase", ""): numeric(row.get("sender_elapsed_ms"), math.nan)
        for row in events
    }
    effective_start = elapsed_by_phase.get("low", math.nan)
    effective_end = elapsed_by_phase.get("recovery", math.nan)
    if (math.isfinite(effective_start) and math.isfinite(effective_end)
            and effective_start < effective_end):
        return effective_start, effective_end
    return nominal_start, nominal_end


def write_csv(path: Path, rows: Sequence[dict], fields: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(rows[0]) if rows else []
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def summarize_stats(path: Path) -> dict[str, float]:
    rows = read_rows(path)
    if not rows:
        return {}

    def series(field: str, positive_only: bool = False) -> list[float]:
        values = [numeric(row.get(field)) for row in rows]
        return [value for value in values if value > 0.0] if positive_only else values

    elapsed = series("elapsed_ms")
    sample_interval = series("sample_interval_us", positive_only=True)
    sample_lateness = series("sample_lateness_us")
    v2_eval = series("v2_eval_us")
    rtt = series("msRTT", positive_only=True)
    send_rate = series("mbpsSendRate", positive_only=True)
    snd_buffer = series("msSndBuf")
    final_window_start = max(elapsed[-1] - 10000.0, 0.0) if elapsed else 0.0
    tail = [numeric(row.get("msSndBuf")) for row in rows
            if numeric(row.get("elapsed_ms")) >= final_window_start]
    tail_x = [numeric(row.get("elapsed_ms")) / 1000.0 for row in rows
              if numeric(row.get("elapsed_ms")) >= final_window_start]

    result = {
        "samples": len(rows),
        "sample_interval_p50_us": percentile(sample_interval, 0.50),
        "sample_interval_p99_us": percentile(sample_interval, 0.99),
        "sample_interval_max_us": max(sample_interval, default=0.0),
        "sample_lateness_p95_us": percentile(sample_lateness, 0.95),
        "sample_lateness_p99_us": percentile(sample_lateness, 0.99),
        "sample_lateness_max_us": max(sample_lateness, default=0.0),
        "v2_eval_p50_us": percentile(v2_eval, 0.50),
        "v2_eval_p95_us": percentile(v2_eval, 0.95),
        "v2_eval_p99_us": percentile(v2_eval, 0.99),
        "v2_eval_max_us": max(v2_eval, default=0.0),
        "rtt_p50_ms": percentile(rtt, 0.50),
        "rtt_p95_ms": percentile(rtt, 0.95),
        "rtt_p99_ms": percentile(rtt, 0.99),
        "send_rate_mean_mbps": mean(send_rate),
        "send_rate_cv": coefficient_of_variation(send_rate),
        "snd_buffer_peak_ms": max(snd_buffer, default=0.0),
        "snd_buffer_tail_median_ms": median(tail),
        "snd_buffer_tail_slope_ms_s": linear_slope(tail_x, tail),
        "pktRcvBelated_sum": sum(series("pktRcvBelated")),
        "pktRcvRetrans_sum": sum(series("pktRcvRetrans")),
    }
    for field in (
        "pktSentTotal", "pktRecvTotal", "pktSentUniqueTotal", "pktRecvUniqueTotal",
        "pktSndLossTotal", "pktRcvLossTotal", "pktRetransTotal",
        "pktSndDropTotal", "pktRcvDropTotal", "byteSentTotal", "byteRecvTotal",
        "byteSentUniqueTotal", "byteRecvUniqueTotal", "byteRetransTotal",
        "byteSndDropTotal", "byteRcvDropTotal",
    ):
        result[field] = max(series(field), default=0.0)
    return result


SUMMARY_FIELDS = [
    "scenario", "variant", "run_id", "seed", "seed_reproducible", "mode", "active_ohead",
    "final_ohead",
    "input_rate_bps", "high_capacity_percent", "low_capacity_percent",
    "high_rate_kbit", "low_rate_kbit", "payload_bytes_read", "payload_bytes_sent",
    "source_sha256",
    "payload_bytes_received", "tx_byteSentUniqueTotal", "rx_byteRecvUniqueTotal",
    "tx_pktSentUniqueTotal", "rx_pktRecvUniqueTotal", "tx_pktSndDropTotal",
    "rx_pktRcvDropTotal", "rx_pktRcvBelated_sum", "tx_pktRetransTotal",
    "rx_pktRcvRetrans_sum", "rtt_p50_ms", "rtt_p95_ms", "rtt_p99_ms",
    "send_rate_mean_mbps", "send_rate_cv", "snd_buffer_peak_ms",
    "snd_buffer_tail_median_ms", "snd_buffer_tail_slope_ms_s",
    "tx_sample_interval_p99_us", "tx_sample_lateness_p99_us",
    "rx_sample_interval_p99_us", "rx_sample_lateness_p99_us",
    "pacing_lateness_p99_us", "pacing_lateness_max_us",
    "send_call_p99_us", "send_call_max_us", "payload_send_duration_s",
    "delivery_interval_p99_us", "delivery_interval_max_us",
    "v2_eval_p99_us", "v2_eval_max_us",
    "controller_changes", "sample_ms", "latency_ms", "run_dir",
]


def discover_run_directories(root: Path) -> list[Path]:
    return sorted(path.parent for path in root.glob("**/run_metadata.csv"))


def collect_run(run_dir: Path) -> dict:
    run = read_key_value(run_dir / "run_metadata.csv")
    tx_meta = read_key_value(run_dir / "tx_metadata.csv")
    rx_meta = read_key_value(run_dir / "rx_metadata.csv")
    tx = summarize_stats(run_dir / "tx_stats.csv")
    rx = summarize_stats(run_dir / "rx_stats.csv")
    return {
        "scenario": run.get("scenario", run_dir.parents[1].name),
        "variant": run.get("variant", run_dir.parent.name),
        "run_id": run.get("run_id", run_dir.name.removeprefix("run_")),
        "seed": run.get("seed", ""),
        "seed_reproducible": run.get("seed_reproducible", "no"),
        "mode": tx_meta.get("mode", ""),
        "active_ohead": tx_meta.get("active_ohead", ""),
        "final_ohead": tx_meta.get("final_ohead", ""),
        "input_rate_bps": run.get("input_rate_bps", tx_meta.get("input_rate_bps", "")),
        "high_capacity_percent": run.get("high_capacity_percent", ""),
        "low_capacity_percent": run.get("low_capacity_percent", ""),
        "high_rate_kbit": run.get("high_rate_kbit", ""),
        "low_rate_kbit": run.get("low_rate_kbit", ""),
        "source_sha256": run.get("source_sha256", ""),
        "payload_bytes_read": tx_meta.get("payload_bytes_read", "0"),
        "payload_bytes_sent": tx_meta.get("payload_bytes_sent", "0"),
        "payload_bytes_received": rx_meta.get("payload_bytes_received", "0"),
        "tx_byteSentUniqueTotal": int(tx.get("byteSentUniqueTotal", 0)),
        "rx_byteRecvUniqueTotal": int(rx.get("byteRecvUniqueTotal", 0)),
        "tx_pktSentUniqueTotal": int(tx.get("pktSentUniqueTotal", 0)),
        "rx_pktRecvUniqueTotal": int(rx.get("pktRecvUniqueTotal", 0)),
        "tx_pktSndDropTotal": int(tx.get("pktSndDropTotal", 0)),
        "rx_pktRcvDropTotal": int(rx.get("pktRcvDropTotal", 0)),
        "rx_pktRcvBelated_sum": int(rx.get("pktRcvBelated_sum", 0)),
        "tx_pktRetransTotal": int(tx.get("pktRetransTotal", 0)),
        "rx_pktRcvRetrans_sum": int(rx.get("pktRcvRetrans_sum", 0)),
        "rtt_p50_ms": tx.get("rtt_p50_ms", 0.0),
        "rtt_p95_ms": tx.get("rtt_p95_ms", 0.0),
        "rtt_p99_ms": tx.get("rtt_p99_ms", 0.0),
        "send_rate_mean_mbps": tx.get("send_rate_mean_mbps", 0.0),
        "send_rate_cv": tx.get("send_rate_cv", 0.0),
        "snd_buffer_peak_ms": tx.get("snd_buffer_peak_ms", 0.0),
        "snd_buffer_tail_median_ms": tx.get("snd_buffer_tail_median_ms", 0.0),
        "snd_buffer_tail_slope_ms_s": tx.get("snd_buffer_tail_slope_ms_s", 0.0),
        "tx_sample_interval_p99_us": tx.get("sample_interval_p99_us", 0.0),
        "tx_sample_lateness_p99_us": tx.get("sample_lateness_p99_us", 0.0),
        "rx_sample_interval_p99_us": rx.get("sample_interval_p99_us", 0.0),
        "rx_sample_lateness_p99_us": rx.get("sample_lateness_p99_us", 0.0),
        "pacing_lateness_p99_us": tx_meta.get("pacing_lateness_p99_us", "0"),
        "pacing_lateness_max_us": tx_meta.get("pacing_lateness_max_us", "0"),
        "send_call_p99_us": tx_meta.get("send_call_p99_us", "0"),
        "send_call_max_us": tx_meta.get("send_call_max_us", "0"),
        "payload_send_duration_s": tx_meta.get("payload_send_duration_s", "0"),
        "delivery_interval_p99_us": rx_meta.get("delivery_interval_p99_us", "0"),
        "delivery_interval_max_us": rx_meta.get("delivery_interval_max_us", "0"),
        "v2_eval_p99_us": tx.get("v2_eval_p99_us", 0.0),
        "v2_eval_max_us": tx.get("v2_eval_max_us", 0.0),
        "controller_changes": tx_meta.get("controller_changes", "0"),
        "sample_ms": tx_meta.get("sample_ms", ""),
        "latency_ms": tx_meta.get("latency_ms", ""),
        "run_dir": str(run_dir),
    }


def command_collect(args: argparse.Namespace) -> None:
    rows = [collect_run(path) for path in discover_run_directories(Path(args.results))]
    if not rows:
        raise SystemExit("No se encontraron corridas con run_metadata.csv")
    write_csv(Path(args.output), rows, SUMMARY_FIELDS)
    print(f"corridas={len(rows)} output={args.output}")


def load_summary(path: Path) -> list[dict[str, str]]:
    return read_rows(path)


def command_baseline(args: argparse.Namespace) -> None:
    rows = [row for row in load_summary(Path(args.summary))
            if row["scenario"] == "clean" and row["variant"] == "vanilla"]
    if len(rows) < args.min_runs:
        raise SystemExit(f"Se requieren {args.min_runs} controles limpios; hay {len(rows)}")
    srt_bytes = [numeric(row["tx_byteSentUniqueTotal"]) for row in rows]
    payload = [numeric(row["payload_bytes_read"]) for row in rows]
    source_hashes = sorted(set(row["source_sha256"] for row in rows))
    srt_cv = coefficient_of_variation(srt_bytes)
    payload_cv = coefficient_of_variation(payload)
    result = {
        "status": (
            "ok" if srt_cv < 0.001 and payload_cv < 0.001
            and len(source_hashes) == 1 and source_hashes[0] else "unstable"
        ),
        "clean_runs": len(rows),
        "B_ref": median(srt_bytes),
        "payload_ref": median(payload),
        "source_sha256": source_hashes[0] if len(source_hashes) == 1 else None,
        "byteSentUniqueTotal_cv": srt_cv,
        "payload_bytes_read_cv": payload_cv,
        "acceptance_limit_cv": 0.001,
    }
    write_json(Path(args.output), result)
    print(json.dumps(result, indent=2))


def with_utility(rows: Iterable[dict[str, str]], baseline: dict) -> list[dict]:
    b_ref = float(baseline["B_ref"])
    payload_ref = float(baseline["payload_ref"])
    enriched = []
    for source in rows:
        row = dict(source)
        row["U"] = numeric(row["rx_byteRecvUniqueTotal"]) / b_ref if b_ref else 0.0
        row["payload_ratio"] = numeric(row["payload_bytes_read"]) / payload_ref if payload_ref else 0.0
        row["source_valid"] = (
            abs(row["payload_ratio"] - 1.0) < 0.001
            and row.get("source_sha256") == baseline.get("source_sha256")
        )
        enriched.append(row)
    return enriched


def command_actuator(args: argparse.Namespace) -> None:
    with Path(args.baseline).open() as stream:
        baseline = json.load(stream)
    if baseline.get("status") != "ok":
        raise SystemExit("El baseline limpio no satisface la variación máxima")
    rows = with_utility(
        (row for row in load_summary(Path(args.summary))
         if row["scenario"] == "dynamic" and row["variant"].startswith("fixed")),
        baseline,
    )
    groups: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        run_dir = Path(row["run_dir"])
        run_metadata = read_key_value(run_dir / "run_metadata.csv")
        tx_rows = read_rows(run_dir / "tx_stats.csv")
        phase_start, phase_end = phase_bounds_ms(run_dir, run_metadata)
        active_rows = [sample for sample in tx_rows
                       if phase_start + numeric(row.get("sample_ms"), 100.0) <=
                       numeric(sample.get("elapsed_ms")) < phase_end]
        expected_mbps = numeric(row["input_rate_bps"]) / 1_000_000.0 * (
            1.0 + numeric(row["active_ohead"]) / 100.0)
        observed_maxbw = median([numeric(sample.get("mbpsMaxBW"))
                                 for sample in active_rows
                                 if numeric(sample.get("mbpsMaxBW")) > 0.0])
        row["maxbw_expected_mbps"] = expected_mbps
        row["maxbw_observed_median_mbps"] = observed_maxbw
        row["maxbw_relative_error"] = (
            abs(observed_maxbw - expected_mbps) / expected_mbps
            if expected_mbps else math.inf
        )
        row["active_usPktSndPeriod_median"] = median([
            numeric(sample.get("usPktSndPeriod")) for sample in active_rows
            if numeric(sample.get("usPktSndPeriod")) > 0.0
        ])
        row["active_mbpsSendRate_median"] = median([
            numeric(sample.get("mbpsSendRate")) for sample in active_rows
            if numeric(sample.get("mbpsSendRate")) > 0.0
        ])
        groups[int(numeric(row["active_ohead"]))].append(row)
    if 25 not in groups or not {10, 40}.intersection(groups):
        raise SystemExit("Se requieren corridas fixed25 y al menos fixed10 o fixed40")

    reference_drop = median([numeric(row["tx_pktSndDropTotal"]) for row in groups[25]])
    reference_tail = median([numeric(row["snd_buffer_tail_median_ms"]) for row in groups[25]])
    report_groups = {}
    candidates = []
    for ohead, items in sorted(groups.items()):
        utility = median([row["U"] for row in items if row["source_valid"]])
        snd_drop = median([numeric(row["tx_pktSndDropTotal"]) for row in items])
        tail = median([numeric(row["snd_buffer_tail_median_ms"]) for row in items])
        slope = median([numeric(row["snd_buffer_tail_slope_ms_s"]) for row in items])
        maxbw_error = median([numeric(row["maxbw_relative_error"], math.inf)
                              for row in items])
        eligible = (
            ohead != 25
            and len(items) >= args.min_runs
            and all(row["source_valid"] for row in items)
            and all(numeric(row["maxbw_relative_error"], math.inf) <= 0.03
                    for row in items)
            and all(int(numeric(row["final_ohead"])) == 25 for row in items)
            and all(int(numeric(row["controller_changes"])) >= 2 for row in items)
            and snd_drop <= reference_drop
            and slope <= 1.0
            and tail <= max(reference_tail * 1.25, reference_tail + 100.0)
        )
        report_groups[str(ohead)] = {
            "runs": len(items), "median_U": utility,
            "median_pktSndDropTotal": snd_drop,
            "median_buffer_tail_ms": tail,
            "median_buffer_tail_slope_ms_s": slope,
            "expected_mbpsMaxBW": median([numeric(row["maxbw_expected_mbps"])
                                           for row in items]),
            "observed_median_mbpsMaxBW": median([
                numeric(row["maxbw_observed_median_mbps"]) for row in items]),
            "median_mbpsMaxBW_relative_error": maxbw_error,
            "median_active_usPktSndPeriod": median([
                numeric(row["active_usPktSndPeriod_median"]) for row in items]),
            "median_active_mbpsSendRate": median([
                numeric(row["active_mbpsSendRate_median"]) for row in items]),
            "median_pktRetransTotal": median([
                numeric(row["tx_pktRetransTotal"]) for row in items]),
            "restored_nominal_in_all_runs": all(
                int(numeric(row["final_ohead"])) == 25 for row in items
            ),
            "eligible": eligible,
        }
        if eligible:
            candidates.append((utility, ohead))

    reference_u = report_groups["25"]["median_U"]
    candidates = [candidate for candidate in candidates if candidate[0] > reference_u]
    reference_valid = (
        len(groups[25]) >= args.min_runs
        and all(row["source_valid"] for row in groups[25])
        and all(numeric(row["maxbw_relative_error"], math.inf) <= 0.03
                for row in groups[25])
    )
    if not reference_valid:
        best_ohead, best_u, direction = None, reference_u, "none"
        status = "calibration_invalid"
    elif candidates:
        best_u, best_ohead = max(candidates)
        status = "viable"
        direction = "decrease" if best_ohead < 25 else "increase"
    else:
        best_ohead, best_u, direction = None, reference_u, "none"
        status = "no_viable_change"

    result = {
        "status": status,
        "reference_ohead": 25,
        "recommended_ohead": best_ohead,
        "direction": direction,
        "reference_median_U": reference_u,
        "recommended_median_U": best_u,
        "reference_valid": reference_valid,
        "groups": report_groups,
    }
    write_json(Path(args.output), result)
    print(json.dumps(result, indent=2))


def interval_sum(rows: Sequence[dict[str, str]], field: str) -> float:
    return sum(numeric(row.get(field)) for row in rows)


def phase_metrics(run_dir: Path) -> dict[str, float]:
    metadata = read_key_value(run_dir / "run_metadata.csv")
    tx_rows = read_rows(run_dir / "tx_stats.csv")
    rx_rows = read_rows(run_dir / "rx_stats.csv")
    high_end, low_end = phase_bounds_ms(run_dir, metadata)
    run_end = numeric(metadata.get("duration_s"), 60.0) * 1000.0
    sample_ms = numeric(metadata.get("sample_ms"), 100.0)
    guard_ms = max(500.0, 2.0 * sample_ms)

    def window(rows: Sequence[dict[str, str]], begin: float,
               end: float) -> list[dict[str, str]]:
        return [row for row in rows
                if begin <= numeric(row.get("elapsed_ms")) < end]

    phases = {
        "high": (3000.0, high_end - guard_ms),
        "low": (high_end + guard_ms, low_end - guard_ms),
        "recovery": (low_end + guard_ms, run_end - guard_ms),
    }
    result: dict[str, float] = {}
    for name, (begin, end) in phases.items():
        tx = window(tx_rows, begin, end)
        rx = window(rx_rows, begin, end)
        received_unique = interval_sum(rx, "pktRecvUnique")
        result[f"{name}_duration_s"] = max((end - begin) / 1000.0, 0.0)
        result[f"{name}_belated"] = interval_sum(rx, "pktRcvBelated")
        result[f"{name}_drop"] = interval_sum(rx, "pktRcvDrop")
        result[f"{name}_belated_fraction"] = (
            result[f"{name}_belated"] / received_unique if received_unique else 0.0
        )
        result[f"{name}_drop_fraction"] = (
            result[f"{name}_drop"] / received_unique if received_unique else 0.0
        )
        result[f"{name}_tx_drop"] = interval_sum(tx, "pktSndDrop")
        result[f"{name}_rtt_p95_ms"] = percentile([
            numeric(row.get("msRTT")) for row in tx
            if numeric(row.get("msRTT")) > 0.0
        ], 0.95)
        result[f"{name}_snd_buffer_p95_ms"] = percentile([
            numeric(row.get("msSndBuf")) for row in tx
        ], 0.95)
    result["expected_base_rtt_ms"] = 2.0 * numeric(metadata.get("delay_ms"), 45.0)
    return result


def phase_gate(metrics: dict[str, float], args: argparse.Namespace) -> dict[str, bool]:
    high_stable = (
        metrics["high_belated_fraction"] <= args.max_high_belated_fraction
        and metrics["high_drop_fraction"] <= args.max_high_drop_fraction
        and metrics["high_tx_drop"] == 0.0
        and metrics["high_rtt_p95_ms"] <=
        metrics["expected_base_rtt_ms"] + args.max_high_rtt_excess_ms
    )
    belated_degraded = (
        metrics["low_belated_fraction"] >=
        metrics["high_belated_fraction"] * args.minimum_event_multiplier
        and metrics["low_belated_fraction"] - metrics["high_belated_fraction"] >=
        args.minimum_belated_fraction_increase
    )
    drop_degraded = (
        metrics["low_drop_fraction"] >=
        metrics["high_drop_fraction"] * args.minimum_event_multiplier
        and metrics["low_drop_fraction"] - metrics["high_drop_fraction"] >=
        args.minimum_drop_fraction_increase
    )
    congestion_observed = (
        metrics["low_rtt_p95_ms"] - metrics["high_rtt_p95_ms"] >=
        args.minimum_rtt_increase_ms
        or metrics["low_snd_buffer_p95_ms"] - metrics["high_snd_buffer_p95_ms"] >=
        args.minimum_buffer_increase_ms
    )
    recovered = (
        metrics["recovery_rtt_p95_ms"] <=
        metrics["high_rtt_p95_ms"] + args.maximum_recovery_rtt_excess_ms
        and metrics["recovery_snd_buffer_p95_ms"] <=
        metrics["high_snd_buffer_p95_ms"] + args.maximum_recovery_buffer_excess_ms
    )
    return {
        "high_stable": high_stable,
        "belated_degraded": belated_degraded,
        "drop_degraded": drop_degraded,
        "congestion_observed": congestion_observed,
        "low_degraded": (belated_degraded or drop_degraded) and congestion_observed,
        "recovered": recovered,
    }


def add_phase_gate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-high-belated-fraction", type=float, default=0.002)
    parser.add_argument("--max-high-drop-fraction", type=float, default=0.0005)
    parser.add_argument("--max-high-rtt-excess-ms", type=float, default=20.0)
    parser.add_argument("--minimum-event-multiplier", type=float, default=1.5)
    parser.add_argument("--minimum-belated-fraction-increase", type=float, default=0.0005)
    parser.add_argument("--minimum-drop-fraction-increase", type=float, default=0.0001)
    parser.add_argument("--minimum-rtt-increase-ms", type=float, default=5.0)
    parser.add_argument("--minimum-buffer-increase-ms", type=float, default=20.0)
    parser.add_argument("--maximum-recovery-rtt-excess-ms", type=float, default=5.0)
    parser.add_argument("--maximum-recovery-buffer-excess-ms", type=float, default=20.0)


def command_profile(args: argparse.Namespace) -> None:
    with Path(args.baseline).open() as stream:
        baseline = json.load(stream)
    if baseline.get("status") != "ok":
        raise SystemExit("El baseline limpio no satisface la variación máxima")
    rows = with_utility(
        (row for row in load_summary(Path(args.summary))
         if row["scenario"] == "dynamic" and row["variant"].startswith("profile")),
        baseline,
    )
    groups: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        capacity = int(numeric(row.get("low_capacity_percent")))
        if capacity == 0:
            capacity = int(row["variant"].removeprefix("profile"))
        metrics = phase_metrics(Path(row["run_dir"]))
        gates = phase_gate(metrics, args)
        row.update(metrics)
        row.update(gates)
        groups[capacity].append(row)
    if not groups:
        raise SystemExit("No se encontraron corridas de calibración de capacidad")

    report_groups = {}
    eligible = []
    for capacity, items in sorted(groups.items(), reverse=True):
        high_fraction = mean([row["high_stable"] for row in items])
        degraded_fraction = mean([row["low_degraded"] for row in items])
        recovery_fraction = mean([row["recovered"] for row in items])
        utility = median([row["U"] for row in items if row["source_valid"]])
        accepted = (
            len(items) >= args.min_runs
            and all(row["source_valid"] for row in items)
            and high_fraction >= args.minimum_pass_fraction
            and degraded_fraction >= args.minimum_pass_fraction
            and recovery_fraction >= args.minimum_pass_fraction
            and args.minimum_u <= utility <= args.maximum_u
        )
        report_groups[str(capacity)] = {
            "runs": len(items),
            "median_U": utility,
            "high_stable_fraction": high_fraction,
            "low_degraded_fraction": degraded_fraction,
            "recovery_fraction": recovery_fraction,
            "median_high_belated_fraction": median([
                row["high_belated_fraction"] for row in items]),
            "median_low_belated_fraction": median([
                row["low_belated_fraction"] for row in items]),
            "median_high_drop_fraction": median([
                row["high_drop_fraction"] for row in items]),
            "median_low_drop_fraction": median([
                row["low_drop_fraction"] for row in items]),
            "median_rtt_p95_increase_ms": median([
                row["low_rtt_p95_ms"] - row["high_rtt_p95_ms"] for row in items]),
            "median_snd_buffer_p95_increase_ms": median([
                row["low_snd_buffer_p95_ms"] - row["high_snd_buffer_p95_ms"]
                for row in items]),
            "eligible": accepted,
        }
        if accepted:
            eligible.append(capacity)
    selected = max(eligible) if eligible else None
    result = {
        "status": "selected" if selected is not None else "no_viable_profile",
        "selected_low_capacity_percent": selected,
        "selection_rule": "highest_eligible_capacity",
        "thresholds": {
            "minimum_pass_fraction": args.minimum_pass_fraction,
            "minimum_U": args.minimum_u,
            "maximum_U": args.maximum_u,
            "max_high_belated_fraction": args.max_high_belated_fraction,
            "max_high_drop_fraction": args.max_high_drop_fraction,
            "minimum_event_multiplier": args.minimum_event_multiplier,
            "minimum_belated_fraction_increase": args.minimum_belated_fraction_increase,
            "minimum_drop_fraction_increase": args.minimum_drop_fraction_increase,
            "minimum_rtt_increase_ms": args.minimum_rtt_increase_ms,
            "minimum_buffer_increase_ms": args.minimum_buffer_increase_ms,
        },
        "groups": report_groups,
    }
    write_json(Path(args.output), result)
    if selected is not None:
        frozen = Path(args.frozen_env)
        frozen.parent.mkdir(parents=True, exist_ok=True)
        frozen.write_text(
            "# generado por analysis/analyze.py profile; congelar antes de TSBPD\n"
            "HIGH_CAPACITY_PERCENT=${HIGH_CAPACITY_PERCENT:-140}\n"
            f"LOW_CAPACITY_PERCENT=${{LOW_CAPACITY_PERCENT:-{selected}}}\n"
        )
    print(json.dumps(result, indent=2))


def command_latency(args: argparse.Namespace) -> None:
    with Path(args.baseline).open() as stream:
        baseline = json.load(stream)
    if baseline.get("status") != "ok":
        raise SystemExit("El baseline limpio no satisface la variación máxima")
    rows = with_utility(
        (row for row in load_summary(Path(args.summary))
         if row["scenario"] == "dynamic"
         and row["variant"].startswith(args.variant_prefix)),
        baseline,
    )
    groups: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        latency = int(numeric(row["latency_ms"]))
        metrics = phase_metrics(Path(row["run_dir"]))
        row.update(metrics)
        row.update(phase_gate(metrics, args))
        groups[latency].append(row)
    if not groups:
        raise SystemExit("No se encontraron corridas de calibración de latencia")

    report_groups = {}
    eligible = []
    for latency, items in sorted(groups.items()):
        high_stable_fraction = mean([
            row["high_stable"] for row in items
        ])
        low_degraded_fraction = mean([
            row["low_degraded"] for row in items
        ])
        recovery_fraction = mean([row["recovered"] for row in items])
        median_u = median([row["U"] for row in items if row["source_valid"]])
        accepted = (
            len(items) >= args.min_runs
            and all(row["source_valid"] for row in items)
            and high_stable_fraction >= args.high_stable_fraction
            and low_degraded_fraction >= args.low_degraded_fraction
            and recovery_fraction >= args.recovery_fraction
            and median_u >= args.minimum_u
        )
        report_groups[str(latency)] = {
            "runs": len(items),
            "median_U": median_u,
            "high_stable_fraction": high_stable_fraction,
            "low_degraded_fraction": low_degraded_fraction,
            "recovery_fraction": recovery_fraction,
            "median_pktRcvBelated": median([
                numeric(row["rx_pktRcvBelated_sum"]) for row in items]),
            "median_pktRcvDropTotal": median([
                numeric(row["rx_pktRcvDropTotal"]) for row in items]),
            "eligible": accepted,
        }
        if accepted:
            eligible.append(latency)
    selected = min(eligible) if eligible else None
    result = {
        "status": "selected" if selected is not None else "no_viable_latency",
        "selected_latency_ms": selected,
        "selection_rule": "smallest_eligible_latency",
        "groups": report_groups,
    }
    write_json(Path(args.output), result)
    if selected is not None:
        frozen = Path(args.frozen_env)
        frozen.parent.mkdir(parents=True, exist_ok=True)
        frozen.write_text(
            "# generado por analysis/analyze.py latency; congelar antes del A/B\n"
            f"LATENCY_MS={selected}\n"
        )
    print(json.dumps(result, indent=2))


def bootstrap_paired(differences: Sequence[float], iterations: int, seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        sample = [rng.choice(differences) for _ in differences]
        estimates.append(mean(sample))
    return percentile(estimates, 0.025), percentile(estimates, 0.975)


def bootstrap_unpaired(a: Sequence[float], b: Sequence[float], iterations: int,
                       seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        sample_a = [rng.choice(a) for _ in a]
        sample_b = [rng.choice(b) for _ in b]
        estimates.append(mean(sample_a) - mean(sample_b))
    return percentile(estimates, 0.025), percentile(estimates, 0.975)


def svg_comparison(path: Path, reference: Sequence[float], treatment: Sequence[float],
                   difference: float, ci: tuple[float, float],
                   reference_label: str = "Vanilla",
                   treatment_label: str = "Asistido") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 720, 360
    maximum = max([1.0, *reference, *treatment]) * 1.05
    bars = [(reference_label, mean(reference), "#64748b"),
            (treatment_label, mean(treatment), "#0f766e")]
    chunks = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="30" y="32" font-family="sans-serif" font-size="18">Datos únicos recibidos a tiempo</text>',
        '<line x1="70" y1="300" x2="470" y2="300" stroke="#334155"/>',
    ]
    for index, (label, value, color) in enumerate(bars):
        x = 130 + index * 190
        bar_height = 220 * value / maximum
        y = 300 - bar_height
        chunks.append(f'<rect x="{x}" y="{y:.2f}" width="100" height="{bar_height:.2f}" fill="{color}"/>')
        chunks.append(f'<text x="{x + 50}" y="325" text-anchor="middle" font-family="sans-serif" font-size="14">{label}</text>')
        chunks.append(f'<text x="{x + 50}" y="{y - 8:.2f}" text-anchor="middle" font-family="sans-serif" font-size="14">{value:.5f}</text>')
    chunks.append(f'<text x="500" y="120" font-family="sans-serif" font-size="14">ΔU = {difference:.6f}</text>')
    chunks.append(f'<text x="500" y="148" font-family="sans-serif" font-size="14">IC95% [{ci[0]:.6f}, {ci[1]:.6f}]</text>')
    chunks.append('</svg>')
    path.write_text("\n".join(chunks))


def command_compare(args: argparse.Namespace) -> None:
    with Path(args.baseline).open() as stream:
        baseline = json.load(stream)
    rows = with_utility(
        (row for row in load_summary(Path(args.summary)) if row["scenario"] == "dynamic"),
        baseline,
    )
    reference_variant = args.reference_variant
    treatment_variant = args.treatment_variant
    comparison_rows = [row for row in rows
                       if row["variant"] in (reference_variant, treatment_variant)]
    signatures = {
        (row["input_rate_bps"], row["latency_ms"], row["source_sha256"])
        for row in comparison_rows
    }
    if len(signatures) != 1:
        raise SystemExit("Las corridas A/B no comparten bitrate, latencia y fuente")
    invalid = [row["run_dir"] for row in comparison_rows if not row["source_valid"]]
    if invalid:
        raise SystemExit(
            "El payload programado no coincide con el baseline en: " + ", ".join(invalid)
        )
    reference_rows = [row for row in comparison_rows
                      if row["variant"] == reference_variant]
    treatment_rows = [row for row in comparison_rows
                      if row["variant"] == treatment_variant]
    if not reference_rows or not treatment_rows:
        raise SystemExit(
            f"Faltan corridas dynamic {reference_variant} o {treatment_variant}"
        )

    reference_by_id = {row["run_id"]: row for row in reference_rows}
    treatment_by_id = {row["run_id"]: row for row in treatment_rows}
    common = sorted(set(reference_by_id).intersection(treatment_by_id))
    paired = (
        len(common) == len(reference_rows) == len(treatment_rows)
        and all(row.get("seed_reproducible") == "yes"
                for row in reference_rows + treatment_rows)
    )
    required = args.min_paired if paired else args.min_unpaired
    if len(reference_rows) < required or len(treatment_rows) < required:
        raise SystemExit(
            f"Diseño {'pareado' if paired else 'no pareado'}: se requieren "
            f"{required} corridas por variante"
        )
    if paired:
        differences = [
            treatment_by_id[key]["U"] - reference_by_id[key]["U"]
            for key in common
        ]
        ci = bootstrap_paired(differences, args.bootstrap, args.seed)
        delta = mean(differences)
    else:
        treatment_u = [row["U"] for row in treatment_rows]
        reference_u = [row["U"] for row in reference_rows]
        ci = bootstrap_unpaired(treatment_u, reference_u, args.bootstrap, args.seed)
        delta = mean(treatment_u) - mean(reference_u)

    interpretation = (
        "supported" if ci[0] > 0.0 else
        "negative" if ci[1] < 0.0 else "inconclusive"
    )
    result = {
        "status": interpretation,
        "design": "paired" if paired else "unpaired",
        "reference_variant": reference_variant,
        "treatment_variant": treatment_variant,
        "reference_runs": len(reference_rows),
        "treatment_runs": len(treatment_rows),
        "B_ref": baseline["B_ref"],
        "reference_mean_U": mean([row["U"] for row in reference_rows]),
        "treatment_mean_U": mean([row["U"] for row in treatment_rows]),
        "delta_U": delta,
        "ci95_low": ci[0],
        "ci95_high": ci[1],
        "hypothesis_supported": ci[0] > 0.0,
        "interpretation": interpretation,
    }
    if reference_variant == "vanilla" and treatment_variant == "assistant":
        result.update({
            "vanilla_runs": len(reference_rows),
            "assistant_runs": len(treatment_rows),
            "vanilla_mean_U": result["reference_mean_U"],
            "assistant_mean_U": result["treatment_mean_U"],
        })
    write_json(Path(args.output), result)
    if args.svg:
        svg_comparison(
            Path(args.svg), [row["U"] for row in reference_rows],
            [row["U"] for row in treatment_rows], delta, ci,
            reference_variant, treatment_variant,
        )
    print(json.dumps(result, indent=2))


def autocorrelation(values: Sequence[float], lag: int) -> float:
    if lag <= 0 or lag >= len(values):
        return 0.0
    left, right = values[:-lag], values[lag:]
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left) *
        sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator else 0.0


def lead_correlation(signal: Sequence[float], events: Sequence[float], lag: int) -> float:
    if lag < 0 or lag >= min(len(signal), len(events)):
        return 0.0
    x = signal[:len(signal) - lag] if lag else signal
    y = events[lag:]
    return autocorrelation_cross(x, y)


def autocorrelation_cross(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) < 2 or len(a) != len(b):
        return 0.0
    a_mean, b_mean = mean(a), mean(b)
    numerator = sum((x - a_mean) * (y - b_mean) for x, y in zip(a, b))
    denominator = math.sqrt(
        sum((x - a_mean) ** 2 for x in a) *
        sum((y - b_mean) ** 2 for y in b)
    )
    return numerator / denominator if denominator else 0.0


def alignment_clock_ms(row: dict[str, str]) -> float:
    timestamp = row.get("Timepoint", "")
    if timestamp:
        try:
            return datetime.datetime.fromisoformat(timestamp).timestamp() * 1000.0
        except ValueError:
            pass
    if row.get("elapsed_ms") not in (None, ""):
        return numeric(row.get("elapsed_ms"))
    if row.get("srt_ms") not in (None, ""):
        return numeric(row.get("srt_ms"))
    if row.get("Time") not in (None, ""):
        return numeric(row.get("Time"))
    return 0.0


def nearest_align(tx_rows: Sequence[dict[str, str]], rx_rows: Sequence[dict[str, str]]) -> list[tuple[dict, dict]]:
    aligned = []
    rx_index = 0
    for tx in tx_rows:
        target = alignment_clock_ms(tx)
        while rx_index + 1 < len(rx_rows):
            current = abs(alignment_clock_ms(rx_rows[rx_index]) - target)
            following = abs(alignment_clock_ms(rx_rows[rx_index + 1]) - target)
            if following >= current:
                break
            rx_index += 1
        if rx_rows:
            aligned.append((tx, rx_rows[rx_index]))
    return aligned


def svg_time_series(path: Path, elapsed: Sequence[float], rtt: Sequence[float],
                    belated: Sequence[float], dropped: Sequence[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 960, 420
    max_x = max(elapsed, default=1.0)
    max_rtt = max(rtt, default=1.0) * 1.1
    max_event = max([1.0, *belated, *dropped])
    def points(values: Sequence[float], scale: float) -> str:
        return " ".join(
            f"{50 + 850 * x / max_x:.1f},{360 - scale * value:.1f}"
            for x, value in zip(elapsed, values)
        )
    rtt_points = points(rtt, 300 / max_rtt)
    belated_points = points(belated, 100 / max_event)
    dropped_points = points(dropped, 100 / max_event)
    content = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="white"/>
<text x="50" y="28" font-family="sans-serif" font-size="18">RTT y eventos temporales</text>
<line x1="50" y1="360" x2="900" y2="360" stroke="#334155"/>
<polyline fill="none" stroke="#0f766e" stroke-width="2" points="{rtt_points}"/>
<polyline fill="none" stroke="#dc2626" stroke-width="1.5" points="{belated_points}"/>
<polyline fill="none" stroke="#7c3aed" stroke-width="1.5" points="{dropped_points}"/>
<text x="720" y="28" font-family="sans-serif" font-size="12" fill="#0f766e">RTT</text>
<text x="770" y="28" font-family="sans-serif" font-size="12" fill="#dc2626">belated</text>
<text x="850" y="28" font-family="sans-serif" font-size="12" fill="#7c3aed">drop</text>
</svg>'''
    path.write_text(content)


def command_temporal(args: argparse.Namespace) -> None:
    tx_all = read_rows(Path(args.tx_stats))
    if not tx_all:
        raise SystemExit("La serie del emisor está vacía")
    origin_ms = alignment_clock_ms(tx_all[0])
    tx_rows = [row for row in tx_all
               if alignment_clock_ms(row) - origin_ms >= args.warmup_ms]
    rx_rows = read_rows(Path(args.rx_stats))
    aligned = nearest_align(tx_rows, rx_rows)
    if len(aligned) < 5:
        raise SystemExit("No hay muestras suficientes para análisis temporal")
    elapsed = [(alignment_clock_ms(tx) - origin_ms) / 1000.0 for tx, _ in aligned]
    rtt = [numeric(tx.get("msRTT")) for tx, _ in aligned]
    belated = [numeric(rx.get("pktRcvBelated")) for _, rx in aligned]
    dropped = [numeric(rx.get("pktRcvDrop")) for _, rx in aligned]
    if args.resample_ms > 0:
        buckets: dict[int, dict[str, list[float]]] = defaultdict(
            lambda: {"rtt": [], "belated": [], "dropped": []}
        )
        for elapsed_s, rtt_value, belated_value, dropped_value in zip(
                elapsed, rtt, belated, dropped):
            bucket = int(elapsed_s * 1000.0 // args.resample_ms)
            buckets[bucket]["rtt"].append(rtt_value)
            buckets[bucket]["belated"].append(belated_value)
            buckets[bucket]["dropped"].append(dropped_value)
        ordered = sorted(buckets)
        elapsed = [bucket * args.resample_ms / 1000.0 for bucket in ordered]
        rtt = [median(buckets[bucket]["rtt"]) for bucket in ordered]
        belated = [sum(buckets[bucket]["belated"]) for bucket in ordered]
        dropped = [sum(buckets[bucket]["dropped"]) for bucket in ordered]
    trend = [0.0] + [rtt[index] - rtt[index - 1] for index in range(1, len(rtt))]

    output_dir = Path(args.output_dir)
    acf_rows = [{"lag_samples": lag, "acf_msRTT": autocorrelation(rtt, lag)}
                for lag in range(1, min(args.max_lag, len(rtt) - 1) + 1)]
    cross_rows = [
        {
            "lead_samples": lag,
            "corr_rtt_trend_to_belated": lead_correlation(trend, belated, lag),
            "corr_rtt_trend_to_drop": lead_correlation(trend, dropped, lag),
        }
        for lag in range(0, min(args.max_lag, len(rtt) - 1) + 1)
    ]
    write_csv(output_dir / "acf.csv", acf_rows)
    write_csv(output_dir / "lead_correlations.csv", cross_rows)
    write_csv(output_dir / "aligned_series.csv", [
        {"elapsed_s": elapsed[index], "msRTT": rtt[index],
         "delta_msRTT": trend[index], "pktRcvBelated": belated[index],
         "pktRcvDrop": dropped[index]}
        for index in range(len(rtt))
    ])
    svg_time_series(output_dir / "series.svg", elapsed, rtt, belated, dropped)
    sample_interval_ms = median([
        (elapsed[index] - elapsed[index - 1]) * 1000.0
        for index in range(1, len(elapsed))
    ])
    best_belated = max(cross_rows, key=lambda row: row["corr_rtt_trend_to_belated"])
    best_drop = max(cross_rows, key=lambda row: row["corr_rtt_trend_to_drop"])
    report = {
        "samples": len(aligned),
        "samples_after_resampling": len(rtt),
        "resample_ms": args.resample_ms,
        "sample_interval_ms_median": sample_interval_ms,
        "rtt_p50_ms": percentile(rtt, 0.50),
        "rtt_p95_ms": percentile(rtt, 0.95),
        "rtt_range_ms": max(rtt, default=0.0) - min(rtt, default=0.0),
        "delta_rtt_abs_p95_ms": percentile([abs(value) for value in trend], 0.95),
        "pktRcvBelated_total": sum(belated),
        "pktRcvDrop_total": sum(dropped),
        "lag0_belated_correlation": cross_rows[0]["corr_rtt_trend_to_belated"],
        "lag0_drop_correlation": cross_rows[0]["corr_rtt_trend_to_drop"],
        "best_belated_lead_samples": best_belated["lead_samples"],
        "best_belated_lead_ms": best_belated["lead_samples"] * sample_interval_ms,
        "best_belated_correlation": best_belated["corr_rtt_trend_to_belated"],
        "best_drop_lead_samples": best_drop["lead_samples"],
        "best_drop_lead_ms": best_drop["lead_samples"] * sample_interval_ms,
        "best_drop_correlation": best_drop["corr_rtt_trend_to_drop"],
    }
    write_json(output_dir / "temporal_report.json", report)
    print(json.dumps(report, indent=2))


def command_temporal_aggregate(args: argparse.Namespace) -> None:
    root = Path(args.input_root)
    report_paths = sorted(root.glob("**/temporal_report.json"))
    if not report_paths:
        raise SystemExit("No se encontraron reportes temporales")
    rows = []
    for path in report_paths:
        report = json.loads(path.read_text())
        rows.append({"run": path.parent.name, **report})
    write_csv(Path(args.output_csv), rows)
    drop_rows = [row for row in rows if numeric(row.get("pktRcvDrop_total")) > 0.0]
    aggregate = {
        "runs": len(rows),
        "runs_with_drop_events": len(drop_rows),
        "median_rtt_range_ms": median([
            numeric(row.get("rtt_range_ms")) for row in rows
        ]),
        "median_delta_rtt_abs_p95_ms": median([
            numeric(row.get("delta_rtt_abs_p95_ms")) for row in rows
        ]),
        "median_lag0_belated_correlation": median([
            numeric(row.get("lag0_belated_correlation")) for row in rows
        ]),
        "median_best_belated_correlation": median([
            numeric(row.get("best_belated_correlation")) for row in rows
        ]),
        "median_best_belated_lead_ms": median([
            numeric(row.get("best_belated_lead_ms")) for row in rows
        ]),
        "median_lag0_drop_correlation_when_observed": median([
            numeric(row.get("lag0_drop_correlation")) for row in drop_rows
        ]),
        "median_best_drop_correlation_when_observed": median([
            numeric(row.get("best_drop_correlation")) for row in drop_rows
        ]),
        "scope_warning": (
            "Los máximos se seleccionan entre múltiples retardos y son exploratorios. "
            "La anticipación debe validarse fuera de muestra contra persistencia y EWMA."
        ),
    }
    write_json(Path(args.output_json), aggregate)
    print(json.dumps(aggregate, indent=2, sort_keys=True))


def sum_column(rows: Sequence[dict[str, str]], field: str) -> float:
    return sum(numeric(row.get(field)) for row in rows)


def command_legacy(args: argparse.Namespace) -> None:
    source = Path(args.input_dir)
    rows_out = []
    for rx_path in sorted(source.glob("rx_run*.csv")):
        suffix = rx_path.stem.removeprefix("rx_run")
        tx_path = source / f"tx_run{suffix}.csv"
        rx_rows = read_rows(rx_path)
        tx_rows = read_rows(tx_path) if tx_path.exists() else []
        recv_all = sum_column(rx_rows, "pktRecv")
        recv_unique = sum_column(rx_rows, "pktRecvUnique")
        sent_unique = sum_column(tx_rows, "pktSentUnique")
        belated = sum_column(rx_rows, "pktRcvBelated")
        row = {
            "run": suffix,
            "pktRecv_including_retransmissions": int(recv_all),
            "pktRecvUnique": int(recv_unique),
            "pktSentUnique": int(sent_unique),
            "pktRcvBelated": int(belated),
            "legacy_belated_pct": 100.0 * belated / recv_all if recv_all else "",
            "belated_per_sent_unique_pct": 100.0 * belated / sent_unique if sent_unique else "",
            "note": "No equivale a U; B_ref requiere controles limpios",
        }
        rows_out.append(row)
    if not rows_out:
        raise SystemExit("No se encontraron rx_run*.csv")
    write_csv(Path(args.output), rows_out)
    recv_total = sum(numeric(row["pktRecv_including_retransmissions"]) for row in rows_out)
    belated_total = sum(numeric(row["pktRcvBelated"]) for row in rows_out)
    report = {
        "runs": len(rows_out),
        "mean_legacy_belated_pct": mean([
            numeric(row["legacy_belated_pct"]) for row in rows_out
        ]),
        "ratio_of_sums_legacy_belated_pct": (
            100.0 * belated_total / recv_total if recv_total else 0.0
        ),
        "mean_belated_per_sent_unique_pct": mean([
            numeric(row["belated_per_sent_unique_pct"]) for row in rows_out
        ]),
        "interpretation": (
            "La cifra histórica usa pktRecv, que incluye retransmisiones. "
            "No equivale a U y no demuestra incumplimiento temporal normalizado."
        ),
    }
    write_json(Path(args.report), report)
    print(json.dumps(report, indent=2, sort_keys=True))


def command_decision(args: argparse.Namespace) -> None:
    def load(path: str) -> dict:
        candidate = Path(path)
        return json.loads(candidate.read_text()) if candidate.exists() else {}

    controller = load(args.controller)
    model_selection = load(args.model_selection)
    model_validation = load(args.model_validation)
    model_revision = load(args.model_revision)
    model_validation_v2 = load(args.model_validation_v2)
    if model_validation_v2:
        predictor = model_validation_v2
    elif model_revision.get("status") == (
            "threshold_recalibrated_requires_fresh_validation"):
        predictor = {
            "status": "awaiting_fresh_validation_v2",
            "criteria": model_revision.get("validation_gates", {}),
        }
    elif model_validation:
        predictor = model_validation
    elif model_selection.get("status") == "development_candidate_identified":
        predictor = {
            "status": "awaiting_fresh_validation",
            "criteria": model_selection.get("candidate", {}),
        }
    else:
        predictor = controller
    reports = {
        "baseline": load(args.baseline),
        "profile": load(args.profile),
        "latency": load(args.latency),
        "actuator": load(args.actuator),
        "predictor": predictor,
        "comparison": load(args.comparison),
    }
    statuses = {name: value.get("status", "missing")
                for name, value in reports.items()}
    required = {
        "baseline": "ok",
        "profile": "selected",
        "latency": "selected",
        "actuator": "viable",
        "predictor": "validated",
    }
    failed = [name for name, expected in required.items()
              if statuses[name] != expected]
    if statuses["predictor"] in (
            "model_development_required", "awaiting_fresh_validation",
            "awaiting_fresh_validation_v2"):
        status = "ab_not_authorized"
        hypothesis_status = "not_tested_predictor_validation_pending"
        reason = (
            "El modelo fue desarrollado, pero todavía no ha sido validado "
            "sin reajuste sobre corridas nuevas."
        )
    elif statuses["predictor"] == "not_validated":
        status = "ab_not_authorized"
        hypothesis_status = "not_tested_predictor_validation_failed"
        reason = (
            "El predictor no superó todas sus compuertas congeladas. El A/B "
            "no puede atribuir desempeño a esta política."
        )
    elif failed:
        status = "ab_not_authorized"
        hypothesis_status = "not_supported_within_tested_architecture"
        reason = (
            "Una o más compuertas previas fallaron; ejecutar el A/B introduciría "
            "un controlador no validado."
        )
    elif statuses["comparison"] == "missing":
        status = "ready_for_ab"
        hypothesis_status = "pending_decisive_comparison"
        reason = "Todas las compuertas previas fueron superadas."
    else:
        status = "complete"
        interpretation = statuses["comparison"]
        hypothesis_status = {
            "supported": "supported",
            "negative": "not_supported",
            "inconclusive": "inconclusive",
        }.get(interpretation, "invalid_comparison")
        reason = "La interpretación procede del IC95 de la diferencia primaria."
    result = {
        "status": status,
        "hypothesis_status": hypothesis_status,
        "reason": reason,
        "failed_gates": failed,
        "gate_statuses": statuses,
        "ab_executed": statuses["comparison"] != "missing",
        "ab_required_for_performance_claim": True,
        "predictor_criteria": reports["predictor"].get("criteria", {}),
    }
    write_json(Path(args.output), result)
    print(json.dumps(result, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect")
    collect.add_argument("--results", default="results")
    collect.add_argument("--output", default="results/summary.csv")
    collect.set_defaults(function=command_collect)

    baseline = commands.add_parser("baseline")
    baseline.add_argument("--summary", default="results/summary.csv")
    baseline.add_argument("--output", default="results/baseline.json")
    baseline.add_argument("--min-runs", type=int, default=5)
    baseline.set_defaults(function=command_baseline)

    actuator = commands.add_parser("actuator")
    actuator.add_argument("--summary", default="results/summary.csv")
    actuator.add_argument("--baseline", default="results/baseline.json")
    actuator.add_argument("--output", default="results/actuator.json")
    actuator.add_argument("--min-runs", type=int, default=3)
    actuator.set_defaults(function=command_actuator)

    profile = commands.add_parser("profile")
    profile.add_argument("--summary", default="results/summary.csv")
    profile.add_argument("--baseline", default="results/baseline.json")
    profile.add_argument("--output", default="results/profile_calibration.json")
    profile.add_argument("--frozen-env", default="experiments/frozen_profile.env")
    profile.add_argument("--min-runs", type=int, default=3)
    profile.add_argument("--minimum-pass-fraction", type=float, default=2.0 / 3.0)
    profile.add_argument("--minimum-u", type=float, default=0.80)
    profile.add_argument("--maximum-u", type=float, default=0.9995)
    add_phase_gate_arguments(profile)
    profile.set_defaults(function=command_profile)

    latency = commands.add_parser("latency")
    latency.add_argument("--summary", default="results/summary.csv")
    latency.add_argument("--baseline", default="results/baseline.json")
    latency.add_argument("--output", default="results/latency_calibration.json")
    latency.add_argument("--frozen-env", default="experiments/frozen_latency.env")
    latency.add_argument("--min-runs", type=int, default=3)
    latency.add_argument("--minimum-u", type=float, default=0.80)
    latency.add_argument("--high-stable-fraction", type=float, default=0.80)
    latency.add_argument("--low-degraded-fraction", type=float, default=0.50)
    latency.add_argument("--recovery-fraction", type=float, default=2.0 / 3.0)
    latency.add_argument("--variant-prefix", default="latencycal")
    add_phase_gate_arguments(latency)
    latency.set_defaults(function=command_latency)

    compare = commands.add_parser("compare")
    compare.add_argument("--summary", default="results/summary.csv")
    compare.add_argument("--baseline", default="results/baseline.json")
    compare.add_argument("--output", default="results/comparison.json")
    compare.add_argument("--svg", default="results/comparison.svg")
    compare.add_argument("--bootstrap", type=int, default=10000)
    compare.add_argument("--seed", type=int, default=20260906)
    compare.add_argument("--min-paired", type=int, default=10)
    compare.add_argument("--min-unpaired", type=int, default=20)
    compare.add_argument("--reference-variant", default="vanilla")
    compare.add_argument("--treatment-variant", default="assistant")
    compare.set_defaults(function=command_compare)

    temporal = commands.add_parser("temporal")
    temporal.add_argument("--tx-stats", required=True)
    temporal.add_argument("--rx-stats", required=True)
    temporal.add_argument("--output-dir", required=True)
    temporal.add_argument("--max-lag", type=int, default=50)
    temporal.add_argument("--warmup-ms", type=float, default=3000.0)
    temporal.add_argument("--resample-ms", type=float, default=0.0)
    temporal.set_defaults(function=command_temporal)

    temporal_aggregate = commands.add_parser("temporal-aggregate")
    temporal_aggregate.add_argument("--input-root", required=True)
    temporal_aggregate.add_argument(
        "--output-csv", default="results/temporal_summary.csv"
    )
    temporal_aggregate.add_argument(
        "--output-json", default="results/temporal_summary.json"
    )
    temporal_aggregate.set_defaults(function=command_temporal_aggregate)

    legacy = commands.add_parser("legacy")
    legacy.add_argument("--input-dir", required=True)
    legacy.add_argument("--output", default="results/legacy_corrected.csv")
    legacy.add_argument("--report", default="results/legacy_report.json")
    legacy.set_defaults(function=command_legacy)

    decision = commands.add_parser("decision")
    decision.add_argument("--baseline", default="results/baseline.json")
    decision.add_argument("--profile", default="results/profile_calibration.json")
    decision.add_argument("--latency", default="results/latency_calibration.json")
    decision.add_argument("--actuator", default="results/actuator.json")
    decision.add_argument("--controller", default="results/controller_calibration.json")
    decision.add_argument(
        "--model-selection", default="results/model_development/model_selection.json"
    )
    decision.add_argument(
        "--model-validation", default="results/model_validation/validation.json"
    )
    decision.add_argument(
        "--model-revision", default="results/model_development_v2/frozen_model.json"
    )
    decision.add_argument(
        "--model-validation-v2",
        default="results/model_validation_v2/validation.json"
    )
    decision.add_argument("--comparison", default="results/comparison.json")
    decision.add_argument("--output", default="results/study_decision.json")
    decision.set_defaults(function=command_decision)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
