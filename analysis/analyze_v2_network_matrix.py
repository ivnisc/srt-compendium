#!/usr/bin/env python3
"""Analiza la matriz factorial V2 de retardo y pérdida."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from pathlib import Path
from types import SimpleNamespace

import analyze
import analyze_v2_robustness as robust
import audit_v2_screening as audit


def read_plan(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def validate_clean_guards(results: Path, baseline: dict) -> dict:
    rows: list[dict] = []
    for run_id in ("pre", "mid", "post"):
        run_dir = results / "clean" / "network_matrix_guard" / f"run_{run_id}"
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
        "all_guards_present": len(rows) == 3,
        "performance_governor": all(row["governor_performance"] for row in rows),
        "cpu_affinity": all(row["cpu_affinity"] for row in rows),
        "source_matches": all(row["source_matches"] for row in rows),
        "payload_matches": all(row["payload_matches"] for row in rows),
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
        "summary": {
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


def phase_record(run_dir: Path, gate_args: SimpleNamespace) -> dict:
    metrics = analyze.phase_metrics(run_dir)
    gates = analyze.phase_gate(metrics, gate_args)
    selected = {
        key: metrics[key] for key in (
            "high_belated_fraction", "high_drop_fraction", "high_rtt_p95_ms",
            "high_snd_buffer_p95_ms", "low_belated_fraction",
            "low_drop_fraction", "low_rtt_p95_ms", "low_snd_buffer_p95_ms",
            "recovery_rtt_p95_ms", "recovery_snd_buffer_p95_ms",
        )
    }
    selected.update(gates)
    return selected


def bootstrap_factorial(groups: dict[str, dict[str, list[dict]]],
                        iterations: int, seed: int) -> dict:
    corners = ("d30_l05", "d30_l2", "d60_l05", "d60_l2")

    def observed_delta(condition: str) -> float:
        arms = groups[condition]
        return analyze.mean([float(row["U"]) for row in arms["assistant"]]) - analyze.mean(
            [float(row["U"]) for row in arms["vanilla"]])

    def contrasts(deltas: dict[str, float]) -> dict[str, float]:
        return {
            "delay_60_minus_30_on_delta_U": 0.5 * (
                deltas["d60_l05"] + deltas["d60_l2"]
                - deltas["d30_l05"] - deltas["d30_l2"]),
            "loss_2_minus_05_on_delta_U": 0.5 * (
                deltas["d30_l2"] + deltas["d60_l2"]
                - deltas["d30_l05"] - deltas["d60_l05"]),
            "delay_loss_interaction_on_delta_U": (
                deltas["d60_l2"] - deltas["d60_l05"]
                - deltas["d30_l2"] + deltas["d30_l05"]),
            "center_minus_corner_mean_on_delta_U": (
                observed_delta("d45_l1")
                - analyze.mean([deltas[name] for name in corners])),
        }

    observed = contrasts({name: observed_delta(name) for name in corners})
    rng = random.Random(seed)
    samples = {name: [] for name in observed}
    for _ in range(iterations):
        deltas: dict[str, float] = {}
        for condition in (*corners, "d45_l1"):
            arms = groups[condition]
            assistant = [float(row["U"]) for row in arms["assistant"]]
            vanilla = [float(row["U"]) for row in arms["vanilla"]]
            deltas[condition] = analyze.mean([
                rng.choice(assistant) for _ in assistant
            ]) - analyze.mean([
                rng.choice(vanilla) for _ in vanilla
            ])
        values = {
            "delay_60_minus_30_on_delta_U": 0.5 * (
                deltas["d60_l05"] + deltas["d60_l2"]
                - deltas["d30_l05"] - deltas["d30_l2"]),
            "loss_2_minus_05_on_delta_U": 0.5 * (
                deltas["d30_l2"] + deltas["d60_l2"]
                - deltas["d30_l05"] - deltas["d60_l05"]),
            "delay_loss_interaction_on_delta_U": (
                deltas["d60_l2"] - deltas["d60_l05"]
                - deltas["d30_l2"] + deltas["d30_l05"]),
            "center_minus_corner_mean_on_delta_U": (
                deltas["d45_l1"]
                - analyze.mean([deltas[name] for name in corners])),
        }
        for name, value in values.items():
            samples[name].append(value)
    report = {}
    for name, value in observed.items():
        ci = (analyze.percentile(samples[name], 0.025),
              analyze.percentile(samples[name], 0.975))
        report[name] = {
            "estimate": value,
            "ci95_low": ci[0],
            "ci95_high": ci[1],
            "interpretation": (
                "positive" if ci[0] > 0.0 else
                "negative" if ci[1] < 0.0 else "inconclusive"),
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="experiments/v2_network_matrix.json")
    parser.add_argument("--plan", default="results/v2/network_matrix/execution_plan.csv")
    parser.add_argument("--results", default="results/v2/network_matrix")
    parser.add_argument("--baseline", default="results/v2/baseline.json")
    parser.add_argument("--output", default="results/v2/network_matrix/comparison.json")
    parser.add_argument("--csv", default="results/v2/network_matrix/condition_runs.csv")
    parser.add_argument("--svg", default="results/v2/network_matrix/comparison.svg")
    parser.add_argument("--bootstrap", type=int, default=10000)
    args = parser.parse_args()

    config_path = Path(args.config)
    plan_path = Path(args.plan)
    results = Path(args.results)
    config = json.loads(config_path.read_text())
    plan = read_plan(plan_path)
    baseline = json.loads(Path(args.baseline).read_text())
    if baseline.get("status") != "ok":
        raise SystemExit("El baseline congelado no está aceptado")
    clean_guards = validate_clean_guards(results, baseline)
    matrix_hash = robust.file_hash(config_path)
    plan_hash = robust.file_hash(plan_path)
    controller_path = config_path.parent / "controller_v2_final.env"
    frozen = audit.read_env(controller_path)
    controller_hash = robust.file_hash(controller_path)
    selection_hash = frozen.get("V2_SELECTION_SHA256", "")
    plan_lookup = {
        (row["condition"], row["arm"], row["run_id"]): row for row in plan
    }
    gate_args = SimpleNamespace(**config["phase_gate"])
    minimum_phase_pass = float(config["minimum_phase_pass_fraction"])
    runs_expected = int(config["runs_per_variant"])
    grouped: dict[str, dict[str, list[dict]]] = {}
    reports: list[dict] = []
    all_records: list[dict] = []

    for index, condition in enumerate(config["conditions"]):
        condition_id = str(condition["id"])
        grouped[condition_id] = {}
        for arm in ("vanilla", "assistant"):
            variant = f"network_{condition_id}_{arm}"
            paths = sorted((results / "dynamic" / variant).glob("run_*"),
                           key=audit.natural_run_key)
            complete = [path for path in paths if (path / "run_metadata.csv").exists()]
            if len(complete) != runs_expected:
                raise SystemExit(
                    f"{variant}: se esperaban {runs_expected} corridas; hay {len(complete)}")
            records = []
            for path in complete:
                record = robust.make_record(path, baseline, arm == "assistant")
                record.update(phase_record(path, gate_args))
                record.update({"condition": condition_id, "arm": arm})
                records.append(record)
                all_records.append(record)
            grouped[condition_id][arm] = records

        arms = grouped[condition_id]
        rows = arms["vanilla"] + arms["assistant"]
        treatment = arms["assistant"]
        vanilla_u = [float(row["U"]) for row in arms["vanilla"]]
        assistant_u = [float(row["U"]) for row in treatment]
        ci = analyze.bootstrap_unpaired(
            assistant_u, vanilla_u, args.bootstrap,
            int(config["bootstrap_seed"]) + index)
        delta = analyze.mean(assistant_u) - analyze.mean(vanilla_u)
        status = "supported" if ci[0] > 0.0 else (
            "negative" if ci[1] < 0.0 else "inconclusive")
        equivalence_path = results / "equivalence" / f"{condition_id}.json"
        equivalence = (json.loads(equivalence_path.read_text())
                       if equivalence_path.exists() else {"status": "missing"})
        expected_queue = math.ceil(
            int(config["input_rate_bps"])
            * int(config["queue_formula_capacity_percent"]) / 100
            * (2 * int(condition["delay_ms"]) + 100) / 1000 / (1500 * 8))
        plan_matches = []
        for record in rows:
            expected = plan_lookup.get(
                (condition_id, str(record["arm"]), str(record["run_id"])))
            plan_matches.append(bool(expected) and all((
                str(record["phase_high_s"]) == expected["phase_high_s"],
                str(record["delay_ms"]) == expected["delay_ms"],
                float(record["loss_percent"]) == float(expected["loss_percent"]),
            )))
        activated = [row for row in treatment
                     if row["activation_relative_low_ms"] is not None]
        during_low = [
            row for row in activated
            if float(row["activation_relative_low_ms"])
                < float(row["recovery_begin_ms"]) - float(row["low_begin_ms"])
        ]
        criteria = {
            "run_count_exact": all(len(value) == runs_expected for value in arms.values()),
            "source_and_payload_constant": all(row["source_valid"] for row in rows),
            "plan_matches_metadata": all(plan_matches),
            "matrix_hash_matches": all(
                row["experiment_matrix_sha256"] == matrix_hash for row in rows),
            "plan_hash_matches": all(
                row["experiment_plan_sha256"] == plan_hash for row in rows),
            "experimental_signature_matches": all(
                row["protocol_version"] == config["protocol_version"]
                and int(row["input_rate_bps"]) == int(config["input_rate_bps"])
                and int(row["duration_s"]) == int(config["duration_s"])
                and int(row["phase_low_s"]) == int(config["phase_low_s"])
                and int(row["latency_ms"]) == int(config["latency_ms"])
                and int(row["delay_ms"]) == int(condition["delay_ms"])
                and float(row["loss_percent"]) == float(condition["loss_percent"])
                and float(row["reorder_percent_effective"]) == 0.0
                and int(row["queue_limit_packets"]) == expected_queue
                and int(row["queue_formula_capacity_percent"])
                    == int(config["queue_formula_capacity_percent"])
                and int(row["high_capacity_percent"])
                    == int(config["high_capacity_percent"])
                and int(row["low_capacity_percent"])
                    == int(config["low_capacity_percent"])
                for row in rows),
            "frozen_controller_hash_matches": all(
                row["controller_config_sha256"] == controller_hash for row in treatment),
            "frozen_selection_hash_matches": all(
                row["controller_selection_sha256"] == selection_hash for row in treatment),
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
            "controller_returned_to_nominal": all(
                int(row["final_ohead"]) == 25 for row in treatment),
            "recovery_activity_bounded": all(
                float(row["recovery_active_fraction_after_1s"]) <= 0.10
                for row in treatment),
            "zero_sender_drops": all(
                int(row["pktSndDropTotal"]) == 0 for row in treatment),
            "controller_runtime_bounded": all(
                float(row["v2_eval_p99_us"]) < 1000.0 for row in treatment),
            "python_cpp_equivalence_preflight": equivalence.get("status") == "equivalent",
        }
        profile_fractions = {
            "high_stable_fraction": analyze.mean([
                float(row["high_stable"]) for row in arms["vanilla"]]),
            "low_degraded_fraction": analyze.mean([
                float(row["low_degraded"]) for row in arms["vanilla"]]),
            "recovered_fraction": analyze.mean([
                float(row["recovered"]) for row in arms["vanilla"]]),
        }
        profile_criteria = {
            key: value >= minimum_phase_pass
            for key, value in profile_fractions.items()
        }
        reports.append({
            "condition": condition_id,
            "delay_ms_per_direction": int(condition["delay_ms"]),
            "loss_percent_forward": float(condition["loss_percent"]),
            "B_ref": baseline["B_ref"],
            "design": "unpaired_independent_netem_realizations",
            "status": status,
            "delta_U": delta,
            "ci95_low": ci[0],
            "ci95_high": ci[1],
            "vanilla": robust.summarize_arm(arms["vanilla"]),
            "assistant": robust.summarize_arm(treatment),
            "integrity_status": "accepted" if all(criteria.values()) else "invalid",
            "integrity_criteria": criteria,
            "profile_status": (
                "eligible_transition_profile" if all(profile_criteria.values())
                else "boundary_or_noneligible_profile"),
            "profile_fractions": profile_fractions,
            "profile_criteria": profile_criteria,
            "timing": {
                "pretransition_actuation_runs": sum(
                    bool(row["pretransition_ohead_change"]) for row in treatment),
                "activation_runs": len(activated),
                "activation_during_low_runs": len(during_low),
                "activation_after_recovery_runs": len(activated) - len(during_low),
                "activation_delay_from_low_median_ms": audit.finite_median([
                    float(row["activation_relative_low_ms"]) for row in activated]),
            },
            "equivalence_report": str(equivalence_path),
        })

    valid = (clean_guards["status"] == "accepted"
             and all(row["integrity_status"] == "accepted" for row in reports))
    eligible = sum(row["profile_status"] == "eligible_transition_profile"
                   for row in reports)
    supported = sum(row["status"] == "supported" for row in reports)
    negative = sum(row["status"] == "negative" for row in reports)
    result = {
        "status": (
            "supported_across_network_matrix"
            if valid and eligible == len(reports) and supported == len(reports)
            else "heterogeneous_or_limited" if valid else "invalid"),
        "scope": "Only the five tested delay/loss cells with the frozen CBR profile",
        "conditions_tested": len(reports),
        "eligible_transition_profiles": eligible,
        "supported_conditions": supported,
        "negative_conditions": negative,
        "inconclusive_conditions": len(reports) - supported - negative,
        "no_pooled_effect": True,
        "factorial_contrasts_exploratory": bootstrap_factorial(
            grouped, args.bootstrap, int(config["bootstrap_seed"]) + 100),
        "conditions": reports,
        "baseline": baseline,
        "clean_host_guards": clean_guards,
        "integrity": {
            "all_conditions_valid": valid,
            "matrix_sha256": matrix_hash,
            "plan_sha256": plan_hash,
            "controller_config_sha256": controller_hash,
            "controller_selection_sha256": selection_hash,
            "plan_jobs": len(plan),
            "expected_plan_jobs": 2 * runs_expected * len(config["conditions"]),
        },
    }
    analyze.write_json(Path(args.output), result)
    analyze.write_csv(Path(args.csv), all_records)
    robust.forest_svg(Path(args.svg), reports)
    print(json.dumps({
        "status": result["status"],
        "supported_conditions": supported,
        "eligible_transition_profiles": eligible,
        "conditions_tested": len(reports),
        "output": args.output,
    }, indent=2))
    if not valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
