#!/usr/bin/env python3
"""Descripción compacta de las series usadas para plantear el modelo de estado."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Sequence

import analyze


def selected(rows: Sequence[dict[str, str]], begin: float,
             end: float) -> list[dict[str, str]]:
    return [row for row in rows
            if begin <= analyze.numeric(row.get("elapsed_ms")) < end]


def values(rows: Sequence[dict[str, str]], field: str,
           positive: bool = False) -> list[float]:
    result = [analyze.numeric(row.get(field)) for row in rows]
    return [value for value in result if value > 0.0] if positive else result


def variance(series: Sequence[float]) -> float:
    return statistics.variance(series) if len(series) > 1 else 0.0


def event_onsets(events: Sequence[float]) -> list[int]:
    return [index for index, value in enumerate(events)
            if value > 0.0 and (index == 0 or events[index - 1] <= 0.0)]


def episode_onsets(events: Sequence[float], elapsed_ms: Sequence[float],
                   quiet_ms: float) -> list[int]:
    onsets = []
    last_event_time: float | None = None
    for index, value in enumerate(events):
        if value <= 0.0:
            continue
        if last_event_time is None or elapsed_ms[index] - last_event_time > quiet_ms:
            onsets.append(index)
        last_event_time = elapsed_ms[index]
    return onsets


def nearest_index(elapsed: Sequence[float], target: float) -> int | None:
    if not elapsed or target < elapsed[0] or target > elapsed[-1]:
        return None
    return min(range(len(elapsed)), key=lambda index: abs(elapsed[index] - target))


def correlation_rows(signal: Sequence[float], belated: Sequence[float],
                     dropped: Sequence[float], sample_ms: float,
                     maximum_lead_ms: float) -> list[dict]:
    maximum = min(len(signal) - 1, int(maximum_lead_ms / sample_ms))
    return [{
        "lead_ms": lag * sample_ms,
        "corr_rtt_slope_to_belated": analyze.lead_correlation(signal, belated, lag),
        "corr_rtt_slope_to_drop": analyze.lead_correlation(signal, dropped, lag),
    } for lag in range(maximum + 1)]


def describe(args: argparse.Namespace) -> None:
    root = Path(args.results) / "dynamic" / args.variant
    run_dirs = sorted(path for path in root.glob("run_*")
                      if (path / "run_metadata.csv").exists())
    if len(run_dirs) < args.minimum_runs:
        raise SystemExit(
            f"Se requieren {args.minimum_runs} corridas de desarrollo; hay {len(run_dirs)}"
        )

    per_run = []
    event_samples: dict[tuple[str, float], list[dict[str, float]]] = defaultdict(list)
    correlations = []
    slope_autocorrelations = []
    transition_events = []
    total_onsets = {"belated": 0, "drop": 0}
    quiet_values = [float(item) for item in args.episode_quiet_ms.split(",")]
    total_episodes = {
        endpoint: {str(int(quiet)): 0 for quiet in quiet_values}
        for endpoint in ("belated", "drop")
    }
    offsets = [float(item) for item in args.event_offsets_ms.split(",")]

    for run_dir in run_dirs:
        metadata = analyze.read_key_value(run_dir / "run_metadata.csv")
        tx_rows = analyze.read_rows(run_dir / "tx_stats.csv")
        rx_rows = analyze.read_rows(run_dir / "rx_stats.csv")
        aligned = analyze.nearest_align(tx_rows, rx_rows)
        aligned = [(tx, rx) for tx, rx in aligned
                   if analyze.numeric(tx.get("elapsed_ms")) >= args.warmup_ms
                   and analyze.numeric(tx.get("msRTT")) > 0.0]
        elapsed = [analyze.numeric(tx.get("elapsed_ms")) for tx, _ in aligned]
        rtt = [analyze.numeric(tx.get("msRTT")) for tx, _ in aligned]
        snd_buffer = [analyze.numeric(tx.get("msSndBuf")) for tx, _ in aligned]
        belated = [analyze.numeric(rx.get("pktRcvBelated")) for _, rx in aligned]
        dropped = [analyze.numeric(rx.get("pktRcvDrop")) for _, rx in aligned]
        intervals = [elapsed[index] - elapsed[index - 1]
                     for index in range(1, len(elapsed))]
        sample_ms = max(analyze.median(intervals), 1.0)
        slope = [0.0] + [
            (rtt[index] - rtt[index - 1]) * 1000.0 /
            max(elapsed[index] - elapsed[index - 1], 1.0)
            for index in range(1, len(rtt))
        ]

        high_end, low_end = analyze.phase_bounds_ms(run_dir, metadata)
        run_end = analyze.numeric(metadata.get("duration_s"), 60.0) * 1000.0
        guard = args.transition_guard_ms
        phase_windows = {
            "high": (args.warmup_ms, high_end - guard),
            "low": (high_end + guard, low_end - guard),
            "recovery": (low_end + guard, run_end - guard),
        }
        row: dict[str, float | str] = {
            "run": run_dir.name,
            "samples": len(aligned),
            "sample_ms": sample_ms,
        }
        for phase, (begin, end) in phase_windows.items():
            indexes = [index for index, time in enumerate(elapsed)
                       if begin <= time < end]
            phase_rtt = [rtt[index] for index in indexes]
            phase_slope = [slope[index] for index in indexes]
            phase_buffer = [snd_buffer[index] for index in indexes]
            row[f"{phase}_rtt_p50_ms"] = analyze.percentile(phase_rtt, 0.50)
            row[f"{phase}_rtt_p95_ms"] = analyze.percentile(phase_rtt, 0.95)
            row[f"{phase}_rtt_variance"] = variance(phase_rtt)
            row[f"{phase}_abs_slope_p95_ms_s"] = analyze.percentile(
                [abs(value) for value in phase_slope], 0.95
            )
            row[f"{phase}_snd_buffer_p95_ms"] = analyze.percentile(phase_buffer, 0.95)
            row[f"{phase}_belated"] = sum(belated[index] for index in indexes)
            row[f"{phase}_drop"] = sum(dropped[index] for index in indexes)

        raw_onsets = {
            "belated": event_onsets(belated),
            "drop": event_onsets(dropped),
        }
        for endpoint, onsets in raw_onsets.items():
            row[f"{endpoint}_sample_onsets"] = len(onsets)
            total_onsets[endpoint] += len(onsets)
            events = belated if endpoint == "belated" else dropped
            episode_indexes = {}
            for quiet in quiet_values:
                grouped = episode_onsets(events, elapsed, quiet)
                episode_indexes[quiet] = grouped
                key = str(int(quiet))
                row[f"{endpoint}_episodes_quiet_{key}ms"] = len(grouped)
                total_episodes[endpoint][key] += len(grouped)
            primary = min(quiet_values, key=lambda value: abs(value - 1000.0))
            post_transition = [
                index for index, value in enumerate(events)
                if elapsed[index] >= high_end and value > 0.0
            ]
            first_index = post_transition[0] if post_transition else None
            transition_events.append({
                "run": run_dir.name,
                "endpoint": endpoint,
                "transition_ms": high_end,
                "first_event_ms": (
                    elapsed[first_index] if first_index is not None else ""
                ),
                "delay_from_transition_ms": (
                    elapsed[first_index] - high_end if first_index is not None else ""
                ),
            })
            for onset in episode_indexes[primary]:
                for offset in offsets:
                    index = nearest_index(elapsed, elapsed[onset] + offset)
                    if index is None:
                        continue
                    event_samples[(endpoint, offset)].append({
                        "rtt_ms": rtt[index],
                        "rtt_slope_ms_s": slope[index],
                        "snd_buffer_ms": snd_buffer[index],
                    })

        for item in correlation_rows(slope, belated, dropped, sample_ms,
                                     args.maximum_lead_ms):
            correlations.append({"run": run_dir.name, **item})
        maximum_autocorrelation_lag = min(
            len(slope) - 1, int(args.maximum_lead_ms / sample_ms)
        )
        for lag in range(1, maximum_autocorrelation_lag + 1):
            slope_autocorrelations.append({
                "run": run_dir.name,
                "lag_ms": lag * sample_ms,
                "slope_autocorrelation": analyze.autocorrelation(slope, lag),
            })
        per_run.append(row)

    event_aligned = []
    for (endpoint, offset), samples in sorted(event_samples.items()):
        for field in ("rtt_ms", "rtt_slope_ms_s", "snd_buffer_ms"):
            series = [sample[field] for sample in samples]
            event_aligned.append({
                "endpoint": endpoint,
                "offset_ms": offset,
                "variable": field,
                "observations": len(series),
                "p25": analyze.percentile(series, 0.25),
                "median": analyze.median(series),
                "p75": analyze.percentile(series, 0.75),
            })

    output = Path(args.output_dir)
    analyze.write_csv(output / "per_run_phase_summary.csv", per_run)
    analyze.write_csv(output / "event_aligned_summary.csv", event_aligned)
    analyze.write_csv(output / "lead_correlations.csv", correlations)
    by_lead: dict[float, list[dict]] = defaultdict(list)
    for row in correlations:
        by_lead[row["lead_ms"]].append(row)
    correlation_summary = [{
        "lead_ms": lead,
        "runs": len(items),
        "median_corr_rtt_slope_to_belated": analyze.median([
            item["corr_rtt_slope_to_belated"] for item in items
        ]),
        "median_corr_rtt_slope_to_drop": analyze.median([
            item["corr_rtt_slope_to_drop"] for item in items
        ]),
    } for lead, items in sorted(by_lead.items())]
    analyze.write_csv(output / "lead_correlations_summary.csv", correlation_summary)
    autocorrelation_by_lag: dict[float, list[float]] = defaultdict(list)
    for row in slope_autocorrelations:
        autocorrelation_by_lag[row["lag_ms"]].append(
            row["slope_autocorrelation"]
        )
    autocorrelation_summary = [{
        "lag_ms": lag,
        "runs": len(items),
        "median_slope_autocorrelation": analyze.median(items),
    } for lag, items in sorted(autocorrelation_by_lag.items())]
    analyze.write_csv(
        output / "slope_autocorrelation_summary.csv", autocorrelation_summary
    )
    analyze.write_csv(output / "transition_event_summary.csv", transition_events)

    def aggregate_phase(field: str) -> float:
        return analyze.median([analyze.numeric(row.get(field)) for row in per_run])

    best_belated = max(
        correlation_summary,
        key=lambda row: row["median_corr_rtt_slope_to_belated"],
    )
    best_drop = max(
        correlation_summary,
        key=lambda row: row["median_corr_rtt_slope_to_drop"],
    )
    report = {
        "status": "described",
        "role": "model_development_only",
        "variant": args.variant,
        "runs": len(run_dirs),
        "sample_ms_median": aggregate_phase("sample_ms"),
        "sample_level_event_onsets": total_onsets,
        "degradation_episodes_by_quiet_ms": total_episodes,
        "event_alignment_episode_quiet_ms": min(
            quiet_values, key=lambda value: abs(value - 1000.0)
        ),
        "phase_medians": {
            phase: {
                "rtt_p50_ms": aggregate_phase(f"{phase}_rtt_p50_ms"),
                "rtt_p95_ms": aggregate_phase(f"{phase}_rtt_p95_ms"),
                "rtt_variance": aggregate_phase(f"{phase}_rtt_variance"),
                "abs_slope_p95_ms_s": aggregate_phase(
                    f"{phase}_abs_slope_p95_ms_s"
                ),
                "snd_buffer_p95_ms": aggregate_phase(
                    f"{phase}_snd_buffer_p95_ms"
                ),
                "belated": aggregate_phase(f"{phase}_belated"),
                "drop": aggregate_phase(f"{phase}_drop"),
            } for phase in ("high", "low", "recovery")
        },
        "maximum_exploratory_correlations": {
            "belated": best_belated,
            "drop": best_drop,
            "warning": "Máximos exploratorios; no constituyen validación del modelo.",
        },
        "slope_persistence": {
            "median_autocorrelation_by_lag": autocorrelation_summary,
            "warning": "Calculada sobre diferencias finitas de msRTT.",
        },
        "first_post_transition_event": {
            endpoint: {
                "runs_observed": len(values),
                "median_delay_ms": analyze.median(values),
                "minimum_delay_ms": min(values) if values else 0.0,
                "maximum_delay_ms": max(values) if values else 0.0,
            }
            for endpoint in ("belated", "drop")
            for values in [[
                analyze.numeric(row["delay_from_transition_ms"])
                for row in transition_events
                if row["endpoint"] == endpoint
                and row["delay_from_transition_ms"] != ""
            ]]
        },
        "outputs": {
            "per_run": str(output / "per_run_phase_summary.csv"),
            "event_aligned": str(output / "event_aligned_summary.csv"),
            "correlations": str(output / "lead_correlations.csv"),
            "correlations_summary": str(
                output / "lead_correlations_summary.csv"
            ),
            "slope_autocorrelation": str(
                output / "slope_autocorrelation_summary.csv"
            ),
            "transition_events": str(
                output / "transition_event_summary.csv"
            ),
        },
    }
    analyze.write_json(output / "description.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--results", default="results")
    result.add_argument("--variant", default="fixed25")
    result.add_argument("--output-dir", default="results/model_development")
    result.add_argument("--minimum-runs", type=int, default=5)
    result.add_argument("--warmup-ms", type=float, default=3000.0)
    result.add_argument("--transition-guard-ms", type=float, default=500.0)
    result.add_argument("--maximum-lead-ms", type=float, default=2000.0)
    result.add_argument("--episode-quiet-ms", default="500,1000,2000")
    result.add_argument(
        "--event-offsets-ms",
        default="-2000,-1000,-500,-300,-200,-100,0,100,300,500",
    )
    return result


if __name__ == "__main__":
    describe(parser().parse_args())
