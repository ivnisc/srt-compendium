#!/usr/bin/env python3
"""Valida estabilidad temporal y ausencia de degradación en controles V2."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import analyze


def validate(args: argparse.Namespace) -> dict:
    paths = [
        path for path in analyze.discover_run_directories(Path(args.results))
        if analyze.read_key_value(path / "run_metadata.csv").get("scenario") == "clean"
        and analyze.read_key_value(path / "run_metadata.csv").get("variant") == "vanilla"
    ]
    if len(paths) < args.min_runs:
        raise SystemExit(
            f"Se requieren {args.min_runs} controles limpios V2; hay {len(paths)}"
        )

    rows = []
    for path in paths:
        run = analyze.read_key_value(path / "run_metadata.csv")
        tx_meta = analyze.read_key_value(path / "tx_metadata.csv")
        rx_meta = analyze.read_key_value(path / "rx_metadata.csv")
        tx = analyze.summarize_stats(path / "tx_stats.csv")
        rx = analyze.summarize_stats(path / "rx_stats.csv")
        sample_ms = analyze.numeric(run.get("sample_ms"), 100.0)
        rows.append({
            "run": str(path),
            "protocol_v2": run.get("protocol_version") == "v2",
            "governor_performance": run.get("cpu_governors") == "performance",
            "affinity_valid": (
                run.get("sender_cpu") == args.sender_cpu
                and run.get("receiver_cpu") == args.receiver_cpu
            ),
            "source_sha256": run.get("source_sha256", ""),
            "payload_bytes_read": analyze.numeric(tx_meta.get("payload_bytes_read")),
            "payload_budget_bytes": analyze.numeric(tx_meta.get("payload_budget_bytes")),
            "byteSentUniqueTotal": analyze.numeric(tx.get("byteSentUniqueTotal")),
            "rtt_p99_ms": analyze.numeric(tx.get("rtt_p99_ms")),
            "tx_sample_lateness_p99_us": analyze.numeric(
                tx.get("sample_lateness_p99_us")),
            "rx_sample_lateness_p99_us": analyze.numeric(
                rx.get("sample_lateness_p99_us")),
            "tx_sample_interval_max_us": analyze.numeric(
                tx.get("sample_interval_max_us")),
            "rx_sample_interval_max_us": analyze.numeric(
                rx.get("sample_interval_max_us")),
            "pacing_lateness_p99_us": analyze.numeric(
                tx_meta.get("pacing_lateness_p99_us")),
            "pacing_lateness_max_us": analyze.numeric(
                tx_meta.get("pacing_lateness_max_us")),
            "payload_send_duration_s": analyze.numeric(
                tx_meta.get("payload_send_duration_s")),
            "zero_network_events": all(
                analyze.numeric(value) == 0.0 for value in (
                    tx.get("pktSndLossTotal"), tx.get("pktRetransTotal"),
                    tx.get("pktSndDropTotal"), rx.get("pktRcvLossTotal"),
                    rx.get("pktRcvDropTotal"), rx.get("pktRcvBelated_sum"),
                    rx.get("pktRcvRetrans_sum"),
                )
            ),
            "sample_ms": sample_ms,
        })

    payloads = [row["payload_bytes_read"] for row in rows]
    sent_unique = [row["byteSentUniqueTotal"] for row in rows]
    source_hashes = {row["source_sha256"] for row in rows}
    criteria = {
        "minimum_runs": len(rows) >= args.min_runs,
        "protocol_v2": all(row["protocol_v2"] for row in rows),
        "performance_governor": all(row["governor_performance"] for row in rows),
        "cpu_affinity": all(row["affinity_valid"] for row in rows),
        "source_constant": len(source_hashes) == 1 and bool(next(iter(source_hashes))),
        "payload_complete_and_constant": (
            analyze.coefficient_of_variation(payloads) < args.maximum_cv
            and all(row["payload_bytes_read"] == row["payload_budget_bytes"]
                    for row in rows)
        ),
        "srt_bytes_constant": (
            analyze.coefficient_of_variation(sent_unique) < args.maximum_cv
        ),
        "zero_loss_retransmission_belated_drop": all(
            row["zero_network_events"] for row in rows),
        "clean_rtt": all(row["rtt_p99_ms"] < args.maximum_rtt_p99_ms
                         for row in rows),
        "stats_scheduler": all(
            row["tx_sample_lateness_p99_us"] < args.maximum_lateness_p99_us
            and row["rx_sample_lateness_p99_us"] < args.maximum_lateness_p99_us
            and row["tx_sample_interval_max_us"] < row["sample_ms"] * 2000.0
            and row["rx_sample_interval_max_us"] < row["sample_ms"] * 2000.0
            for row in rows
        ),
        "sender_pacing": all(
            row["pacing_lateness_p99_us"] < args.maximum_lateness_p99_us
            and abs(row["payload_send_duration_s"] - args.duration_s)
            < args.maximum_duration_error_ms / 1000.0
            for row in rows
        ),
    }
    report = {
        "status": "accepted" if all(criteria.values()) else "rejected",
        "runs": len(rows),
        "criteria": criteria,
        "limits": {
            "maximum_cv": args.maximum_cv,
            "maximum_rtt_p99_ms": args.maximum_rtt_p99_ms,
            "maximum_lateness_p99_us": args.maximum_lateness_p99_us,
            "maximum_duration_error_ms": args.maximum_duration_error_ms,
            "maximum_sample_interval_us": "2 * configured sample interval",
        },
        "summary": {
            "byteSentUniqueTotal_cv": analyze.coefficient_of_variation(sent_unique),
            "payload_bytes_read_cv": analyze.coefficient_of_variation(payloads),
            "maximum_rtt_p99_ms": max(row["rtt_p99_ms"] for row in rows),
            "maximum_tx_sample_lateness_p99_us": max(
                row["tx_sample_lateness_p99_us"] for row in rows),
            "maximum_rx_sample_lateness_p99_us": max(
                row["rx_sample_lateness_p99_us"] for row in rows),
            "maximum_pacing_lateness_p99_us": max(
                row["pacing_lateness_p99_us"] for row in rows),
        },
        "per_run": rows,
    }
    analyze.write_json(Path(args.output), report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results/v2")
    parser.add_argument("--output", default="results/v2/host_validation.json")
    parser.add_argument("--min-runs", type=int, default=5)
    parser.add_argument("--sender-cpu", default="0")
    parser.add_argument("--receiver-cpu", default="1")
    parser.add_argument("--duration-s", type=float, default=60.0)
    parser.add_argument("--maximum-cv", type=float, default=0.001)
    parser.add_argument("--maximum-rtt-p99-ms", type=float, default=1.0)
    parser.add_argument("--maximum-lateness-p99-us", type=float, default=10000.0)
    parser.add_argument("--maximum-duration-error-ms", type=float, default=10.0)
    args = parser.parse_args()
    report = validate(args)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["status"] != "accepted":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
