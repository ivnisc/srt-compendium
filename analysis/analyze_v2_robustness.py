#!/usr/bin/env python3
"""Analiza la matriz de robustez V2 sin agregar condiciones heterogéneas."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path

import analyze
import audit_v2_screening as audit


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean_baseline(run_dirs: list[Path], minimum: int = 5) -> dict:
    complete = [path for path in run_dirs if (path / "run_metadata.csv").exists()]
    if len(complete) < minimum:
        raise SystemExit(
            f"Se requieren {minimum} controles limpios; hay {len(complete)}"
        )
    sent: list[float] = []
    payload: list[float] = []
    sources: set[str] = set()
    for run_dir in complete:
        run = analyze.read_key_value(run_dir / "run_metadata.csv")
        tx_meta = analyze.read_key_value(run_dir / "tx_metadata.csv")
        tx = analyze.summarize_stats(run_dir / "tx_stats.csv")
        sent.append(analyze.numeric(str(tx.get("byteSentUniqueTotal", 0))))
        payload.append(analyze.numeric(tx_meta.get("payload_bytes_read")))
        sources.add(run.get("source_sha256", ""))
    sent_cv = analyze.coefficient_of_variation(sent)
    payload_cv = analyze.coefficient_of_variation(payload)
    status = (
        "ok" if sent_cv < 0.001 and payload_cv < 0.001
        and len(sources) == 1 and "" not in sources else "unstable"
    )
    return {
        "status": status,
        "clean_runs": len(complete),
        "B_ref": analyze.median(sent),
        "payload_ref": analyze.median(payload),
        "source_sha256": next(iter(sources)) if len(sources) == 1 else None,
        "byteSentUniqueTotal_cv": sent_cv,
        "payload_bytes_read_cv": payload_cv,
        "acceptance_limit_cv": 0.001,
        "run_dirs": [str(path) for path in complete],
    }


def load_baselines(results: Path, base_v2: Path, config: dict) -> dict[str, dict]:
    base_85 = json.loads((base_v2 / "baseline.json").read_text())
    baselines: dict[str, dict] = {"8500000": base_85}
    for rate, variant in (
        (4_000_000, "robust_b4_clean"),
        (12_000_000, "robust_b12_clean"),
    ):
        paths = sorted((results / "clean" / variant).glob("run_*"))
        baselines[str(rate)] = clean_baseline(paths)
    expected_rates = {str(int(item["input_rate_bps"])) for item in config["conditions"]}
    if set(baselines) != expected_rates:
        raise SystemExit("No existe un baseline para cada bitrate de la matriz")
    unstable = [rate for rate, value in baselines.items() if value.get("status") != "ok"]
    if unstable:
        raise SystemExit("Baselines inestables para: " + ", ".join(unstable))
    return baselines


def validate_clean_guards(results: Path, baselines: dict[str, dict]) -> dict:
    definitions = (
        (4_000_000, "robust_b4_clean", ("1", "2", "3", "4", "5")),
        (4_000_000, "robust_b4_guard", ("post",)),
        (8_500_000, "robust_b85_guard", ("pre", "post")),
        (12_000_000, "robust_b12_clean", ("1", "2", "3", "4", "5")),
        (12_000_000, "robust_b12_guard", ("post",)),
    )
    rows: list[dict] = []
    for rate, variant, run_ids in definitions:
        baseline = baselines[str(rate)]
        for run_id in run_ids:
            run_dir = results / "clean" / variant / f"run_{run_id}"
            if not (run_dir / "run_metadata.csv").exists():
                raise SystemExit(f"Falta el control limpio {run_dir}")
            run = analyze.read_key_value(run_dir / "run_metadata.csv")
            tx_meta = analyze.read_key_value(run_dir / "tx_metadata.csv")
            tx = analyze.summarize_stats(run_dir / "tx_stats.csv")
            rx = analyze.summarize_stats(run_dir / "rx_stats.csv")
            sample_ms = analyze.numeric(run.get("sample_ms"), 100.0)
            sent = analyze.numeric(str(tx.get("byteSentUniqueTotal", 0)))
            payload = analyze.numeric(tx_meta.get("payload_bytes_read"))
            rows.append({
                "run_dir": str(run_dir),
                "input_rate_bps": rate,
                "governor_performance": run.get("cpu_governors") == "performance",
                "cpu_affinity": run.get("sender_cpu") == "0" and run.get("receiver_cpu") == "1",
                "source_matches": run.get("source_sha256") == baseline["source_sha256"],
                "payload_matches": abs(payload - float(baseline["payload_ref"])) < 0.5,
                "srt_bytes_relative_error": (
                    abs(sent - float(baseline["B_ref"])) / float(baseline["B_ref"])
                    if float(baseline["B_ref"]) else math.inf
                ),
                "zero_network_events": all(analyze.numeric(str(value)) == 0.0 for value in (
                    tx.get("pktSndLossTotal"), tx.get("pktRetransTotal"),
                    tx.get("pktSndDropTotal"), rx.get("pktRcvLossTotal"),
                    rx.get("pktRcvDropTotal"), rx.get("pktRcvBelated_sum"),
                    rx.get("pktRcvRetrans_sum"),
                )),
                "rtt_p99_ms": analyze.numeric(str(tx.get("rtt_p99_ms", 0))),
                "tx_sample_lateness_p99_us": analyze.numeric(
                    str(tx.get("sample_lateness_p99_us", 0))),
                "rx_sample_lateness_p99_us": analyze.numeric(
                    str(rx.get("sample_lateness_p99_us", 0))),
                "tx_sample_interval_max_us": analyze.numeric(
                    str(tx.get("sample_interval_max_us", 0))),
                "rx_sample_interval_max_us": analyze.numeric(
                    str(rx.get("sample_interval_max_us", 0))),
                "pacing_lateness_p99_us": analyze.numeric(
                    tx_meta.get("pacing_lateness_p99_us")),
                "payload_send_duration_s": analyze.numeric(
                    tx_meta.get("payload_send_duration_s")),
                "sample_ms": sample_ms,
            })
    criteria = {
        "all_guards_present": len(rows) == 14,
        "performance_governor": all(row["governor_performance"] for row in rows),
        "cpu_affinity": all(row["cpu_affinity"] for row in rows),
        "sources_match": all(row["source_matches"] for row in rows),
        "payloads_match": all(row["payload_matches"] for row in rows),
        "srt_bytes_stable": all(
            row["srt_bytes_relative_error"] < 0.001 for row in rows),
        "zero_loss_retransmission_belated_drop": all(
            row["zero_network_events"] for row in rows),
        "clean_rtt": all(row["rtt_p99_ms"] < 1.0 for row in rows),
        "stats_scheduler": all(
            row["tx_sample_lateness_p99_us"] < 10_000.0
            and row["rx_sample_lateness_p99_us"] < 10_000.0
            and row["tx_sample_interval_max_us"] < row["sample_ms"] * 2000.0
            and row["rx_sample_interval_max_us"] < row["sample_ms"] * 2000.0
            for row in rows),
        "sender_pacing": all(
            row["pacing_lateness_p99_us"] < 10_000.0
            and abs(row["payload_send_duration_s"] - 60.0) < 0.010
            for row in rows),
    }
    return {
        "status": "accepted" if all(criteria.values()) else "rejected",
        "criteria": criteria,
        "limits": {
            "maximum_srt_bytes_relative_error": 0.001,
            "maximum_rtt_p99_ms": 1.0,
            "maximum_lateness_p99_us": 10_000.0,
            "maximum_duration_error_ms": 10.0,
        },
        "summary": {
            "maximum_srt_bytes_relative_error": max(
                row["srt_bytes_relative_error"] for row in rows),
            "maximum_rtt_p99_ms": max(row["rtt_p99_ms"] for row in rows),
            "maximum_tx_sample_lateness_p99_us": max(
                row["tx_sample_lateness_p99_us"] for row in rows),
            "maximum_rx_sample_lateness_p99_us": max(
                row["rx_sample_lateness_p99_us"] for row in rows),
            "maximum_pacing_lateness_p99_us": max(
                row["pacing_lateness_p99_us"] for row in rows),
        },
        "runs": rows,
    }


def read_plan(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "mean": analyze.mean(values),
        "median": analyze.median(values),
        "minimum": min(values, default=0.0),
        "maximum": max(values, default=0.0),
        "sample_sd": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def summarize_arm(rows: list[dict]) -> dict:
    fields = (
        "U", "pktRcvBelated_sum", "pktRcvDropTotal", "pktRetransTotal",
        "pktSndDropTotal", "snd_buffer_peak_ms", "snd_buffer_tail_median_ms",
        "rtt_p95_ms", "v2_eval_p99_us",
    )
    output: dict[str, object] = {"runs": len(rows)}
    for field in fields:
        output[field] = summarize([float(row[field]) for row in rows])
    return output


def first_change_before_low(run_dir: Path, low_begin: float) -> bool:
    rows = analyze.read_rows(run_dir / "tx_stats.csv")
    previous: int | None = None
    for row in rows:
        current = int(analyze.numeric(row.get("ohead_pct")))
        elapsed = analyze.numeric(row.get("elapsed_ms"))
        if previous is not None and current != previous and elapsed < low_begin:
            return True
        previous = current
    return False


def make_record(run_dir: Path, baseline: dict, treatment: bool) -> dict:
    record = audit.audit_run(
        run_dir, float(baseline["B_ref"]), float(baseline["payload_ref"]),
        baseline.get("source_sha256"), treatment,
    )
    metadata = analyze.read_key_value(run_dir / "run_metadata.csv")
    record.update({
        "experiment_matrix_sha256": metadata.get("experiment_matrix_sha256", ""),
        "experiment_plan_sha256": metadata.get("experiment_plan_sha256", ""),
        "queue_formula_capacity_percent": metadata.get(
            "queue_formula_capacity_percent", ""),
        "pretransition_ohead_change": (
            first_change_before_low(run_dir, float(record["low_begin_ms"]))
            if treatment else False
        ),
    })
    return record


def forest_svg(path: Path, conditions: list[dict]) -> None:
    width = 900
    height = 100 + 70 * len(conditions)
    largest = max(
        [abs(float(item[key])) for item in conditions
         for key in ("ci95_low", "ci95_high")], default=0.001,
    )
    limit = max(largest * 1.15, 0.0001)
    left, right = 250.0, 850.0

    def scale(value: float) -> float:
        return left + (value + limit) * (right - left) / (2.0 * limit)

    zero = scale(0.0)
    chunks = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="20" y="32" font-family="sans-serif" font-size="18">Robustez del efecto por condición</text>',
        f'<line x1="{zero:.2f}" y1="55" x2="{zero:.2f}" y2="{height - 35}" stroke="#94a3b8" stroke-dasharray="4 4"/>',
    ]
    for index, item in enumerate(conditions):
        y = 78 + 70 * index
        low = scale(float(item["ci95_low"]))
        high = scale(float(item["ci95_high"]))
        point = scale(float(item["delta_U"]))
        color = "#0f766e" if item["status"] == "supported" else (
            "#b91c1c" if item["status"] == "negative" else "#64748b"
        )
        chunks.extend([
            f'<text x="20" y="{y + 5}" font-family="sans-serif" font-size="14">{item["condition"]}</text>',
            f'<line x1="{low:.2f}" y1="{y}" x2="{high:.2f}" y2="{y}" stroke="{color}" stroke-width="3"/>',
            f'<circle cx="{point:.2f}" cy="{y}" r="6" fill="{color}"/>',
            f'<text x="{right}" y="{y + 5}" text-anchor="end" font-family="sans-serif" font-size="12">ΔU={float(item["delta_U"]):.6f}</text>',
        ])
    chunks.extend([
        f'<text x="{left}" y="{height - 12}" font-family="sans-serif" font-size="12">{-limit:.5f}</text>',
        f'<text x="{zero}" y="{height - 12}" text-anchor="middle" font-family="sans-serif" font-size="12">0</text>',
        f'<text x="{right}" y="{height - 12}" text-anchor="end" font-family="sans-serif" font-size="12">{limit:.5f}</text>',
        '</svg>',
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(chunks))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="experiments/v2_robustness.json")
    parser.add_argument("--plan", default="results/v2/robustness/execution_plan.csv")
    parser.add_argument("--results", default="results/v2/robustness")
    parser.add_argument("--base-v2", default="results/v2")
    parser.add_argument("--output", default="results/v2/robustness/comparison.json")
    parser.add_argument("--csv", default="results/v2/robustness/condition_runs.csv")
    parser.add_argument("--svg", default="results/v2/robustness/comparison.svg")
    parser.add_argument("--bootstrap", type=int, default=10000)
    args = parser.parse_args()

    config_path = Path(args.config)
    plan_path = Path(args.plan)
    results = Path(args.results)
    config = json.loads(config_path.read_text())
    plan = read_plan(plan_path)
    baselines = load_baselines(results, Path(args.base_v2), config)
    clean_guards = validate_clean_guards(results, baselines)
    expected_matrix_hash = file_hash(config_path)
    expected_plan_hash = file_hash(plan_path)
    controller_path = config_path.parent / "controller_v2_final.env"
    frozen = audit.read_env(controller_path)
    expected_controller_hash = file_hash(controller_path)
    expected_selection_hash = frozen.get("V2_SELECTION_SHA256", "")
    runs_expected = int(config["runs_per_variant"])
    plan_lookup = {
        (row["condition"], row["arm"], row["run_id"]): row for row in plan
    }
    all_records: list[dict] = []
    reports: list[dict] = []

    for index, condition in enumerate(config["conditions"]):
        condition_id = str(condition["id"])
        baseline = baselines[str(int(condition["input_rate_bps"]))]
        arms: dict[str, list[dict]] = {}
        for arm in ("vanilla", "assistant"):
            variant = f"robust_{condition_id}_{arm}"
            run_dirs = sorted(
                (results / "dynamic" / variant).glob("run_*"),
                key=audit.natural_run_key,
            )
            complete = [path for path in run_dirs
                        if (path / "run_metadata.csv").exists()]
            if len(complete) != runs_expected:
                raise SystemExit(
                    f"{variant}: se esperaban {runs_expected} corridas; hay {len(complete)}"
                )
            arms[arm] = [make_record(path, baseline, arm == "assistant")
                         for path in complete]
            for record in arms[arm]:
                record["condition"] = condition_id
                record["arm"] = arm
                all_records.append(record)

        rows = arms["vanilla"] + arms["assistant"]
        plan_matches = []
        for record in rows:
            expected = plan_lookup.get(
                (condition_id, str(record["arm"]), str(record["run_id"])))
            plan_matches.append(bool(expected) and all((
                str(record["phase_high_s"]) == expected["phase_high_s"],
                str(record["phase_low_s"]) == expected["phase_low_s"],
                str(record["input_rate_bps"]) == expected["input_rate_bps"],
                str(record["low_capacity_percent"]) == expected[
                    "low_capacity_percent"],
            )))
        vanilla_u = [float(row["U"]) for row in arms["vanilla"]]
        assistant_u = [float(row["U"]) for row in arms["assistant"]]
        ci = analyze.bootstrap_unpaired(
            assistant_u, vanilla_u, args.bootstrap,
            int(config["bootstrap_seed"]) + index,
        )
        delta = analyze.mean(assistant_u) - analyze.mean(vanilla_u)
        status = "supported" if ci[0] > 0.0 else (
            "negative" if ci[1] < 0.0 else "inconclusive"
        )
        treatment = arms["assistant"]
        equivalence_path = results / "equivalence" / f"{condition_id}.json"
        equivalence = (
            json.loads(equivalence_path.read_text())
            if equivalence_path.exists() else {"status": "missing"}
        )
        expected_queue = math.ceil(
            int(condition["input_rate_bps"])
            * int(config["queue_formula_capacity_percent"]) / 100
            * (2 * int(config["delay_ms"]) + 100) / 1000 / (1500 * 8)
        )
        activated = [row for row in treatment
                     if row["activation_relative_low_ms"] is not None]
        criteria = {
            "run_count_exact": all(len(arms[arm]) == runs_expected for arm in arms),
            "source_and_payload_constant": all(row["source_valid"] for row in rows),
            "plan_matches_metadata": all(plan_matches),
            "matrix_hash_matches": all(
                row["experiment_matrix_sha256"] == expected_matrix_hash
                for row in rows),
            "plan_hash_matches": all(
                row["experiment_plan_sha256"] == expected_plan_hash
                for row in rows),
            "experimental_signature_matches": all(
                row["protocol_version"] == config["protocol_version"]
                and int(row["input_rate_bps"]) == int(condition["input_rate_bps"])
                and int(row["duration_s"]) == int(config["duration_s"])
                and int(row["phase_low_s"]) == int(config["phase_low_s"])
                and int(row["latency_ms"]) == int(config["latency_ms"])
                and int(row["delay_ms"]) == int(config["delay_ms"])
                and float(row["loss_percent"]) == float(config["loss_percent"])
                and float(row["reorder_percent_effective"]) == 0.0
                and int(row["queue_limit_packets"]) == expected_queue
                and int(row["queue_formula_capacity_percent"])
                    == int(config["queue_formula_capacity_percent"])
                and int(row["high_capacity_percent"])
                    == int(config["high_capacity_percent"])
                and int(row["low_capacity_percent"])
                    == int(condition["low_capacity_percent"])
                for row in rows),
            "frozen_controller_hash_matches": all(
                row["controller_config_sha256"] == expected_controller_hash
                for row in treatment),
            "frozen_selection_hash_matches": all(
                row["controller_selection_sha256"] == expected_selection_hash
                for row in treatment),
            "controller_returned_to_nominal": all(
                int(row["final_ohead"]) == 25 for row in treatment),
            "candidate_never_actuated": all(
                row["candidate_kept_nominal"] for row in treatment),
            "no_pretransition_actuation": all(
                not row["pretransition_ohead_change"] for row in treatment),
            "activation_confirmed_when_present": all(
                row["activation_was_confirmed"] for row in activated),
            "ohead_changes_coherent": all(
                int(row["actual_ohead_transition_count"])
                    == (2 if row["activation_relative_low_ms"] is not None else 0)
                and int(row["controller_changes_metadata"])
                    == (2 if row["activation_relative_low_ms"] is not None else 0)
                for row in treatment),
            "maxbw_matches_actuator_when_present": all(
                float(row["active_mbpsMaxBW_relative_error"]) <= 0.03
                for row in activated),
            "recovery_activity_bounded": all(
                float(row["recovery_active_fraction_after_1s"]) <= 0.10
                for row in treatment),
            "zero_sender_drops": all(
                int(row["pktSndDropTotal"]) == 0 for row in treatment),
            "controller_runtime_bounded": all(
                float(row["v2_eval_p99_us"]) < 1000.0 for row in treatment),
            "python_cpp_equivalence_preflight": (
                equivalence.get("status") == "equivalent"),
        }
        high_counts = {
            arm: dict(sorted(Counter(
                int(row["phase_high_s"]) for row in arms[arm]
            ).items())) for arm in arms
        }
        during_low = [
            row for row in activated
            if float(row["activation_relative_low_ms"])
                < float(row["recovery_begin_ms"]) - float(row["low_begin_ms"])
        ]
        report = {
            "condition": condition_id,
            "input_rate_bps": int(condition["input_rate_bps"]),
            "low_capacity_percent": int(condition["low_capacity_percent"]),
            "B_ref": baseline["B_ref"],
            "design": "unpaired_independent_netem_realizations",
            "status": status,
            "delta_U": delta,
            "ci95_low": ci[0],
            "ci95_high": ci[1],
            "vanilla": summarize_arm(arms["vanilla"]),
            "assistant": summarize_arm(treatment),
            "integrity_status": "accepted" if all(criteria.values()) else "invalid",
            "integrity_criteria": criteria,
            "equivalence_report": str(equivalence_path),
            "timing": {
                "phase_high_counts": high_counts,
                "same_schedule_in_both_arms": high_counts["vanilla"] == high_counts["assistant"],
                "pretransition_actuation_runs": sum(
                    bool(row["pretransition_ohead_change"]) for row in treatment),
                "activation_runs": len(activated),
                "activation_fraction": len(activated) / len(treatment),
                "activation_during_low_runs": len(during_low),
                "activation_after_recovery_runs": len(activated) - len(during_low),
                "activation_delay_from_low_median_ms": audit.finite_median([
                    float(row["activation_relative_low_ms"])
                    for row in activated
                ]),
            },
        }
        reports.append(report)

    valid = (
        clean_guards["status"] == "accepted"
        and all(item["integrity_status"] == "accepted" for item in reports)
    )
    supported = sum(item["status"] == "supported" for item in reports)
    negative = sum(item["status"] == "negative" for item in reports)
    capacity = [item for item in reports if item["input_rate_bps"] == 8_500_000]
    bitrate = [item for item in reports if item["low_capacity_percent"] == 104]
    result = {
        "status": (
            "supported_across_tested_matrix" if valid and supported == len(reports)
            else "heterogeneous_or_limited" if valid else "invalid"
        ),
        "scope": "Only the five tested CBR bitrate/capacity cells",
        "conditions_tested": len(reports),
        "supported_conditions": supported,
        "negative_conditions": negative,
        "inconclusive_conditions": len(reports) - supported - negative,
        "no_pooled_effect": True,
        "inference_note": (
            "No se calcula un delta global porque mezclar bitrates y capacidades "
            "cambiaría el peso científico de cada condición."
        ),
        "randomized_transition_test": next(
            item for item in reports if item["condition"] == "b85_c104"),
        "capacity_sensitivity": capacity,
        "bitrate_sensitivity": bitrate,
        "conditions": reports,
        "baselines": baselines,
        "clean_host_guards": clean_guards,
        "integrity": {
            "all_conditions_valid": valid,
            "matrix_sha256": expected_matrix_hash,
            "plan_sha256": expected_plan_hash,
            "controller_config_sha256": expected_controller_hash,
            "controller_selection_sha256": expected_selection_hash,
            "plan_jobs": len(plan),
            "expected_plan_jobs": 2 * runs_expected * len(config["conditions"]),
            "equivalence_reports": sum(
                (results / "equivalence" / f"{item['condition']}.json").exists()
                for item in reports),
        },
    }
    analyze.write_json(Path(args.output), result)
    analyze.write_csv(Path(args.csv), all_records)
    forest_svg(Path(args.svg), reports)
    print(json.dumps({
        "status": result["status"],
        "supported_conditions": supported,
        "conditions_tested": len(reports),
        "output": args.output,
    }, indent=2))
    if not valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
