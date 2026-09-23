import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "analysis" / "analyze.py"
SPEC = importlib.util.spec_from_file_location("analysis", MODULE_PATH)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)
sys.modules["analyze"] = analysis
CALIBRATION_PATH = Path(__file__).parents[1] / "analysis" / "calibrate_controller.py"
CALIBRATION_SPEC = importlib.util.spec_from_file_location("calibrate_controller", CALIBRATION_PATH)
calibration = importlib.util.module_from_spec(CALIBRATION_SPEC)
sys.modules["calibrate_controller"] = calibration
CALIBRATION_SPEC.loader.exec_module(calibration)
DESCRIPTION_PATH = Path(__file__).parents[1] / "analysis" / "describe_model_data.py"
DESCRIPTION_SPEC = importlib.util.spec_from_file_location(
    "describe_model_data", DESCRIPTION_PATH
)
description = importlib.util.module_from_spec(DESCRIPTION_SPEC)
sys.modules["describe_model_data"] = description
DESCRIPTION_SPEC.loader.exec_module(description)
MODEL_PATH = Path(__file__).parents[1] / "analysis" / "select_state_model.py"
MODEL_SPEC = importlib.util.spec_from_file_location("select_state_model", MODEL_PATH)
model_selection = importlib.util.module_from_spec(MODEL_SPEC)
sys.modules["select_state_model"] = model_selection
MODEL_SPEC.loader.exec_module(model_selection)
ROBUST_PLAN_PATH = (
    Path(__file__).parents[1] / "analysis" / "generate_v2_robustness_plan.py"
)
ROBUST_PLAN_SPEC = importlib.util.spec_from_file_location(
    "generate_v2_robustness_plan", ROBUST_PLAN_PATH
)
robust_plan = importlib.util.module_from_spec(ROBUST_PLAN_SPEC)
ROBUST_PLAN_SPEC.loader.exec_module(robust_plan)
NETWORK_PLAN_PATH = (
    Path(__file__).parents[1] / "analysis" / "generate_v2_network_plan.py"
)
NETWORK_PLAN_SPEC = importlib.util.spec_from_file_location(
    "generate_v2_network_plan", NETWORK_PLAN_PATH
)
network_plan = importlib.util.module_from_spec(NETWORK_PLAN_SPEC)
NETWORK_PLAN_SPEC.loader.exec_module(network_plan)


class AnalysisTests(unittest.TestCase):
    def test_network_plan_is_factorial_balanced_and_reproducible(self):
        config = json.loads((
            Path(__file__).parents[1] / "experiments" / "v2_network_matrix.json"
        ).read_text())
        first = network_plan.build(config)
        second = network_plan.build(config)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 200)
        for condition in config["conditions"]:
            schedules = []
            for arm in ("vanilla", "assistant"):
                selected = sorted(
                    (row for row in first
                     if row["condition"] == condition["id"] and row["arm"] == arm),
                    key=lambda row: int(row["run_id"]),
                )
                self.assertEqual(len(selected), 20)
                schedules.append([row["phase_high_s"] for row in selected])
            self.assertEqual(schedules[0], schedules[1])

    def test_robustness_plan_balances_arms_and_hides_transition_schedule(self):
        config = json.loads((
            Path(__file__).parents[1] / "experiments" / "v2_robustness.json"
        ).read_text())
        rows = robust_plan.build(config)
        self.assertEqual(len(rows), 200)
        for condition in config["conditions"]:
            schedules = []
            for arm in ("vanilla", "assistant"):
                selected = sorted(
                    (row for row in rows
                     if row["condition"] == condition["id"] and row["arm"] == arm),
                    key=lambda row: int(row["run_id"]),
                )
                self.assertEqual(len(selected), 20)
                schedules.append([row["phase_high_s"] for row in selected])
            self.assertEqual(schedules[0], schedules[1])

    def test_event_onsets_count_only_zero_to_positive_transitions(self):
        self.assertEqual(description.event_onsets([0, 1, 1, 0, 2]), [1, 4])

    def test_episode_onsets_require_configured_quiet_interval(self):
        events = [1, 0, 1, 0, 0, 2]
        elapsed = [0, 100, 200, 300, 400, 1300]
        self.assertEqual(
            description.episode_onsets(events, elapsed, quiet_ms=500), [0, 5]
        )

    def test_damped_trend_forecast_is_bounded(self):
        trace = model_selection.Trace([100.0], [10.0], [0.0], [1.0])
        prediction = model_selection.forecast_scores(
            trace, 1000.0, "K3", {"lambda": 2.0}
        )[0]
        self.assertGreater(prediction, 100.0)
        self.assertLess(prediction, 110.0)

    def test_percentile_and_cv(self):
        self.assertEqual(analysis.percentile([1, 2, 3], 0.5), 2)
        self.assertEqual(analysis.coefficient_of_variation([5, 5, 5]), 0)

    def test_paired_bootstrap_constant_difference(self):
        low, high = analysis.bootstrap_paired([0.01] * 10, 1000, 7)
        self.assertAlmostEqual(low, 0.01)
        self.assertAlmostEqual(high, 0.01)

    def test_stats_summary_uses_max_totals_and_sums_belated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stats.csv"
            path.write_text(
                "elapsed_ms,msRTT,mbpsSendRate,msSndBuf,pktRcvBelated,"
                "pktRcvRetrans,byteRecvUniqueTotal,pktRecvUniqueTotal\n"
                "0,50,8,0,2,1,100,1\n"
                "100,52,8,1,3,2,200,2\n"
            )
            result = analysis.summarize_stats(path)
            self.assertEqual(result["pktRcvBelated_sum"], 5)
            self.assertEqual(result["byteRecvUniqueTotal"], 200)

    def test_future_labels_respect_action_delay(self):
        labels = calibration.future_labels([0, 0, 0, 1, 0], 2, 3)
        self.assertEqual(labels, [True, True, False, False, False])

    def test_kalman_forecast_responds_to_rtt_trend(self):
        model = calibration.RttTrendKalman(1.0, 0.1)
        predictions = [model.update(value, 0.1, 0.3) for value in (50, 51, 52, 53)]
        self.assertGreater(predictions[-1], 53)

    def test_action_latency_is_measured_from_maxbw_response(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "dynamic" / "fixed10" / "run_1"
            run_dir.mkdir(parents=True)
            run_dir.joinpath("run_metadata.csv").write_text(
                "key,value\ninput_rate_bps,8000000\n"
            )
            run_dir.joinpath("tx_metadata.csv").write_text(
                "key,value\nnominal_ohead,25\nactive_ohead,10\n"
            )
            run_dir.joinpath("tx_stats.csv").write_text(
                "elapsed_ms,decision,mbpsMaxBW\n"
                "15000,fixed_activate,10.0\n"
                "15100,hold,8.8\n"
            )
            report = calibration.measure_action_latency(Path(directory))
            self.assertEqual(report["status"], "measured")
            self.assertEqual(report["median_ms"], 100)

    def test_alignment_clock_prefers_iso_timepoint(self):
        first = analysis.alignment_clock_ms({
            "Timepoint": "2026-06-30T21:52:28.373496-0400", "Time": "264"
        })
        second = analysis.alignment_clock_ms({
            "Timepoint": "2026-06-30T21:52:28.473496-0400", "Time": "999"
        })
        self.assertAlmostEqual(second - first, 100.0, places=3)

    def test_alignment_clock_prefers_elapsed_over_socket_timestamp(self):
        self.assertEqual(analysis.alignment_clock_ms({
            "elapsed_ms": "100", "srt_ms": "374"
        }), 100.0)

    def test_legacy_writes_denominator_warning_and_aggregate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("rx_run1.csv").write_text(
                "pktRecv,pktRecvUnique,pktRcvBelated\n10,8,2\n20,18,1\n"
            )
            root.joinpath("tx_run1.csv").write_text(
                "pktSentUnique\n10\n20\n"
            )
            output = root / "legacy.csv"
            report = root / "legacy.json"
            analysis.command_legacy(Namespace(
                input_dir=str(root), output=str(output), report=str(report)
            ))
            result = json.loads(report.read_text())
            self.assertEqual(result["runs"], 1)
            self.assertAlmostEqual(result["mean_legacy_belated_pct"], 10.0)
            self.assertIn("incluye retransmisiones", result["interpretation"])

    def test_temporal_aggregate_excludes_empty_drop_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for run, drops, correlation in ((1, 0, 0.0), (2, 3, 0.4)):
                run_dir = root / f"run_{run}"
                run_dir.mkdir()
                run_dir.joinpath("temporal_report.json").write_text(json.dumps({
                    "rtt_range_ms": 2.0,
                    "delta_rtt_abs_p95_ms": 0.2,
                    "pktRcvDrop_total": drops,
                    "lag0_belated_correlation": 0.1,
                    "best_belated_correlation": 0.2,
                    "best_belated_lead_ms": 300.0,
                    "lag0_drop_correlation": correlation,
                    "best_drop_correlation": correlation,
                }))
            output_json = root / "summary.json"
            analysis.command_temporal_aggregate(Namespace(
                input_root=str(root), output_csv=str(root / "summary.csv"),
                output_json=str(output_json)
            ))
            result = json.loads(output_json.read_text())
            self.assertEqual(result["runs_with_drop_events"], 1)
            self.assertAlmostEqual(
                result["median_lag0_drop_correlation_when_observed"], 0.4
            )

    def test_phase_gate_requires_delivery_and_congestion_change(self):
        args = analysis.build_parser().parse_args([
            "profile", "--summary", "unused", "--baseline", "unused"
        ])
        metrics = {
            "high_belated_fraction": 0.001,
            "high_drop_fraction": 0.0001,
            "high_tx_drop": 0.0,
            "high_rtt_p95_ms": 92.0,
            "low_belated_fraction": 0.002,
            "low_drop_fraction": 0.0001,
            "low_rtt_p95_ms": 100.0,
            "high_snd_buffer_p95_ms": 180.0,
            "low_snd_buffer_p95_ms": 185.0,
            "recovery_rtt_p95_ms": 93.0,
            "recovery_snd_buffer_p95_ms": 182.0,
            "expected_base_rtt_ms": 90.0,
        }
        result = analysis.phase_gate(metrics, args)
        self.assertTrue(result["high_stable"])
        self.assertTrue(result["belated_degraded"])
        self.assertTrue(result["congestion_observed"])
        self.assertTrue(result["low_degraded"])
        self.assertTrue(result["recovered"])

        metrics["low_rtt_p95_ms"] = 93.0
        result = analysis.phase_gate(metrics, args)
        self.assertFalse(result["congestion_observed"])
        self.assertFalse(result["low_degraded"])

    def test_decision_blocks_ab_when_predictor_is_not_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {
                "baseline": {"status": "ok"},
                "profile": {"status": "selected"},
                "latency": {"status": "selected"},
                "actuator": {"status": "viable"},
                "controller": {
                    "status": "predictor_not_validated",
                    "criteria": {"kalman_f1_exceeds_both_baselines": False},
                },
            }
            paths = {}
            for name, value in values.items():
                paths[name] = root / f"{name}.json"
                paths[name].write_text(json.dumps(value))
            output = root / "decision.json"
            analysis.command_decision(Namespace(
                baseline=str(paths["baseline"]), profile=str(paths["profile"]),
                latency=str(paths["latency"]), actuator=str(paths["actuator"]),
                controller=str(paths["controller"]),
                model_selection=str(root / "missing_selection.json"),
                model_validation=str(root / "missing_validation.json"),
                model_revision=str(root / "missing_revision.json"),
                model_validation_v2=str(root / "missing_validation_v2.json"),
                comparison=str(root / "missing.json"), output=str(output)
            ))
            result = json.loads(output.read_text())
            self.assertEqual(result["status"], "ab_not_authorized")
            self.assertEqual(result["failed_gates"], ["predictor"])
            self.assertFalse(result["ab_executed"])

    def test_decision_does_not_treat_pilot_as_hypothesis_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {
                "baseline": {"status": "ok"},
                "profile": {"status": "selected"},
                "latency": {"status": "selected"},
                "actuator": {"status": "viable"},
                "controller": {"status": "model_development_required"},
            }
            paths = {}
            for name, value in values.items():
                paths[name] = root / f"{name}.json"
                paths[name].write_text(json.dumps(value))
            output = root / "decision.json"
            analysis.command_decision(Namespace(
                baseline=str(paths["baseline"]), profile=str(paths["profile"]),
                latency=str(paths["latency"]), actuator=str(paths["actuator"]),
                controller=str(paths["controller"]),
                model_selection=str(root / "missing_selection.json"),
                model_validation=str(root / "missing_validation.json"),
                model_revision=str(root / "missing_revision.json"),
                model_validation_v2=str(root / "missing_validation_v2.json"),
                comparison=str(root / "missing.json"), output=str(output)
            ))
            result = json.loads(output.read_text())
            self.assertEqual(
                result["hypothesis_status"],
                "not_tested_predictor_validation_pending"
            )


if __name__ == "__main__":
    unittest.main()
