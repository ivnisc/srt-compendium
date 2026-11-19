import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

import generate_ohead_ablation_plan as plan  # noqa: E402
import analyze_ohead_ablation as analysis  # noqa: E402


class OheadAblationPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(
            (ROOT / "experiments" / "ohead_ablation_protocol.json").read_text()
        )

    def test_plan_is_balanced_in_permuted_blocks(self) -> None:
        rows = plan.build(self.config)
        arms = {item["id"] for item in self.config["arms"]}
        runs = self.config["runs_per_arm"]
        self.assertEqual(len(rows), runs * len(arms))
        self.assertEqual(Counter(row["arm"] for row in rows),
                         Counter({arm: runs for arm in arms}))
        for block in range(1, runs + 1):
            block_rows = [row for row in rows if row["block"] == block]
            self.assertEqual({row["arm"] for row in block_rows}, arms)
            self.assertEqual({row["run_id"] for row in block_rows}, {block})
            self.assertEqual(
                [row["position"] for row in block_rows],
                list(range(1, len(arms) + 1)),
            )

    def test_generation_is_deterministic(self) -> None:
        self.assertEqual(plan.build(self.config), plan.build(self.config))

    def test_runtime_protocol_version_matches_frozen_id(self) -> None:
        wrapper = (ROOT / "scripts" /
                   "run_ohead_ablation_experiment.sh").read_text()
        self.assertIn(
            f'PROTOCOL_VERSION={self.config["protocol_id"]}', wrapper)

    def test_existing_plan_must_match(self) -> None:
        expected = plan.normalized(plan.build(self.config))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            config_path = Path(directory) / "protocol.json"
            config_path.write_text(json.dumps(self.config))
            old_argv = sys.argv
            try:
                sys.argv = [
                    "generate_ohead_ablation_plan.py",
                    "--config", str(config_path),
                    "--output", str(path),
                ]
                plan.main()
                self.assertTrue(path.exists())
                sys.argv = [
                    "generate_ohead_ablation_plan.py",
                    "--config", str(config_path),
                    "--output", str(path),
                ]
                plan.main()
            finally:
                sys.argv = old_argv
            self.assertEqual(len(expected), 400)


class OheadAblationAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = json.loads(
            (ROOT / "experiments" / "ohead_ablation_protocol.json").read_text()
        )

    @staticmethod
    def transition(elapsed: float, previous: int, current: int,
                   decision: str, confirmed: int = 0) -> dict[str, object]:
        return {
            "elapsed_ms": elapsed, "previous": previous, "current": current,
            "decision": decision, "v2_confirmed": confirmed,
        }

    def test_student_t_quantiles(self) -> None:
        self.assertAlmostEqual(
            analysis.student_t_ppf(0.95, 10.0), 1.8124611228, places=8)
        self.assertAlmostEqual(
            analysis.student_t_ppf(0.95, 198.0), 1.6525857836, places=8)

    def test_welch_tost_detects_equivalence(self) -> None:
        left = [1.0000 + index * 0.00001 for index in range(100)]
        right = [1.0001 + index * 0.00001 for index in range(100)]
        result = analysis.welch_tost(left, right, 0.0003, 0.05)
        self.assertTrue(result["equivalent"])
        self.assertGreater(result["ci90_low"], -0.0003)
        self.assertLess(result["ci90_high"], 0.0003)

    def test_schedule_excludes_final_out_of_grid_row(self) -> None:
        rows = [
            {
                "elapsed_ms": str(index * 100),
                "sample_lateness_us": "0",
            }
            for index in range(601)
        ]
        rows.append({"elapsed_ms": "60417", "sample_lateness_us": "17"})
        report = analysis.schedule_audit(rows, 100.0, 60.0)
        self.assertTrue(report["valid"])
        self.assertEqual(report["scheduled_rows"], 601)

    def test_schedule_rejects_missing_position(self) -> None:
        rows = [
            {
                "elapsed_ms": str(index * 100),
                "sample_lateness_us": "0",
            }
            for index in range(601)
            if index != 250
        ]
        rows.append({"elapsed_ms": "60417", "sample_lateness_us": "0"})
        self.assertFalse(analysis.schedule_audit(rows, 100.0, 60.0)["valid"])

    def test_phase_bounds_accept_submillisecond_negative_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network_events.csv"
            path.write_text(
                "phase,monotonic_s,sender_elapsed_ms,rate_kbit\n"
                "high,201399.70,-0.555,11900\n"
                "low,201414.75,15049.445,8840\n"
                "recovery,201444.80,45099.445,11900\n"
            )
            bounds = analysis.phase_bounds(Path(directory), 60.0, 250.0)
        self.assertEqual(bounds["high"][0], -0.555)
        timing = analysis.network_timing_audit(bounds, self.protocol)
        self.assertTrue(timing["valid"])
        self.assertEqual(timing["errors_ms"]["high_ms"], -0.555)

    def test_phase_bounds_reject_origin_outside_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network_events.csv"
            path.write_text(
                "phase,monotonic_s,sender_elapsed_ms,rate_kbit\n"
                "high,1.0,-300.0,11900\n"
                "low,16.0,15000.0,8840\n"
                "recovery,46.0,45000.0,11900\n"
            )
            with self.assertRaises(ValueError):
                analysis.phase_bounds(Path(directory), 60.0, 250.0)

    def test_phase_metrics_difference_cumulative_counters(self) -> None:
        tx = [
            {
                "elapsed_ms": str(elapsed), "pktRetransTotal": str(total),
                "pktSndDropTotal": "0", "msRTT": "90", "msSndBuf": "0",
                "mbpsSendRate": "8.5", "mbpsMaxBW": "10.625",
            }
            for elapsed, total in ((0, 0), (100, 2), (200, 5), (300, 9))
        ]
        rx = [
            {
                "elapsed_ms": str(elapsed), "pktRcvBelated": "1",
                "pktRcvDropTotal": str(total),
            }
            for elapsed, total in ((0, 0), (100, 1), (200, 4), (300, 7))
        ]
        bounds = {
            "high": (0.0, 150.0),
            "low": (150.0, 250.0),
            "recovery": (250.0, 400.0),
        }
        result = analysis.phase_metrics(tx, rx, bounds)
        self.assertEqual(result["high_pktRetransTotal"], 2.0)
        self.assertEqual(result["low_pktRetransTotal"], 3.0)
        self.assertEqual(result["recovery_pktRetransTotal"], 4.0)
        self.assertEqual(result["low_pktRcvDropTotal"], 3.0)

    def test_fixed_arm_assignments_are_checked(self) -> None:
        bounds = {
            "high": (0.0, 15050.0),
            "low": (15050.0, 45070.0),
            "recovery": (45070.0, 60000.0),
        }
        vanilla = analysis.arm_assignment_and_behavior(
            "vanilla", [25, 25], [], 25, 0, bounds, 100.0, 0)
        const10 = analysis.arm_assignment_and_behavior(
            "const10", [10, 10], [], 10, 0, bounds, 100.0, 0)
        oracle = analysis.arm_assignment_and_behavior(
            "oracle10", [25, 10, 25], [
                self.transition(15000.0, 25, 10, "fixed_activate"),
                self.transition(45000.0, 10, 25, "fixed_restore"),
            ], 25, 2, bounds, 100.0, 0)
        self.assertTrue(vanilla[0])
        self.assertTrue(const10[0])
        self.assertTrue(oracle[0])

    def test_early_assistant_activation_is_valid_and_marked(self) -> None:
        bounds = {
            "high": (0.0, 15050.0),
            "low": (15050.0, 45070.0),
            "recovery": (45070.0, 60000.0),
        }
        valid, behavior, crosscheck = analysis.arm_assignment_and_behavior(
            "assistant", [25, 10, 25], [
                self.transition(14000.0, 25, 10, "v2_activate", 1),
                self.transition(46000.0, 10, 25, "v2_restore", 1),
            ], 25, 2, bounds, 100.0, 1)
        self.assertTrue(valid)
        self.assertTrue(crosscheck)
        self.assertTrue(behavior["early_activation"])
        self.assertFalse(behavior["protocol_conforming"])

    def test_assistant_without_restore_is_valid_and_marked(self) -> None:
        bounds = {
            "high": (0.0, 15050.0),
            "low": (15050.0, 45070.0),
            "recovery": (45070.0, 60000.0),
        }
        valid, behavior, crosscheck = analysis.arm_assignment_and_behavior(
            "assistant", [25, 10, 10], [
                self.transition(18000.0, 25, 10, "v2_activate", 1),
            ], 25, 2, bounds, 100.0, 1)
        self.assertTrue(valid)
        self.assertTrue(crosscheck)
        self.assertTrue(behavior["missing_restore"])
        self.assertTrue(behavior["unexpected_transition_count"])

    def test_ready_mismatch_halts_after_calibrable_baseline(self) -> None:
        bounds = {
            "high": (0.0, 15050.0),
            "low": (15050.0, 45070.0),
            "recovery": (45070.0, 60000.0),
        }
        valid, behavior, crosscheck = analysis.arm_assignment_and_behavior(
            "assistant", [25, 25], [], 25, 0, bounds, 100.0, 0)
        self.assertTrue(valid)
        self.assertFalse(crosscheck)
        self.assertTrue(behavior["readiness_failure"])
        self.assertFalse(behavior["protocol_conforming"])

    def test_non_calibrable_baseline_rejects_const10_symmetrically(self) -> None:
        rows = [
            {"elapsed_ms": str(3000 + index * 100), "msRTT": "90"}
            for index in range(19)
        ]
        report = analysis.baseline_calibration_audit(rows, self.protocol)
        self.assertFalse(report["valid"])
        self.assertEqual(report["samples"], 19)

    def test_active_maxbw_ignores_transition_sample(self) -> None:
        rows = [
            {"ohead_pct": "25", "mbpsMaxBW": "10.625"},
            # la consulta de estadísticas precede al setsockopt de esta muestra
            {"ohead_pct": "10", "mbpsMaxBW": "10.625"},
            {"ohead_pct": "10", "mbpsMaxBW": "9.35"},
            {"ohead_pct": "10", "mbpsMaxBW": "9.35"},
        ]
        report = analysis.active_maxbw_diagnostic(rows, 8_500_000.0)
        self.assertEqual(report["status"], "matches")
        self.assertEqual(report["active_samples"], 3)
        self.assertEqual(report["usable_samples"], 2)

    def test_short_actuation_is_descriptive_not_invalid(self) -> None:
        rows = [
            {"ohead_pct": "25", "mbpsMaxBW": "10.625"},
            {"ohead_pct": "10", "mbpsMaxBW": "10.625"},
            {"ohead_pct": "25", "mbpsMaxBW": "9.35"},
        ]
        report = analysis.active_maxbw_diagnostic(rows, 8_500_000.0)
        self.assertEqual(report["status"], "unverified_short_actuation")

    def test_active_maxbw_mismatch_halts_actuator(self) -> None:
        rows = [
            {"ohead_pct": "25", "mbpsMaxBW": "10.625"},
            {"ohead_pct": "10", "mbpsMaxBW": "10.625"},
            {"ohead_pct": "10", "mbpsMaxBW": "10.625"},
            {"ohead_pct": "10", "mbpsMaxBW": "10.625"},
        ]
        diagnostic = analysis.active_maxbw_diagnostic(rows, 8_500_000.0)
        self.assertEqual(diagnostic["status"], "mismatch")
        status = analysis.classify_audit_status(
            True, diagnostic["status"] == "mismatch", "ready", True,
            "assistant", "not_required")
        self.assertEqual(status, "actuator_application_error")

    def test_failure_disposition_is_bound_to_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disposition.json"
            path.write_text(json.dumps({
                "decision": "infrastructure_failure_replace",
                "plan_execution_order": 7,
                "attempt": 0,
                "run_id": 42,
                "justification": "proceso receptor terminado por el host",
            }))
            accepted = analysis.validated_disposition(
                path, "infrastructure_failure_replace", {
                    "plan_execution_order": 7,
                    "attempt": 0,
                    "run_id": 42,
                })
            rejected = analysis.validated_disposition(
                path, "infrastructure_failure_replace", {
                    "plan_execution_order": 7,
                    "attempt": 1,
                    "run_id": 42,
                })
        self.assertIsNotNone(accepted)
        self.assertIsNone(rejected)

    def test_control_disposition_is_bound_to_logical_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disposition.json"
            path.write_text(json.dumps({
                "decision": "infrastructure_failure_replace",
                "control_slot": "pre_1",
                "attempt": 0,
                "run_id": "pre_1",
                "prior_interval_impact": "isolated",
                "justification": "latencia anómala del host confirmada en el log",
            }))
            accepted = analysis.validated_control_disposition(
                path, self.protocol, {
                    "control_slot": "pre_1", "attempt": 0,
                    "run_id": "pre_1",
                })
            rejected = analysis.validated_control_disposition(
                path, self.protocol, {
                    "control_slot": "pre_2", "attempt": 0,
                    "run_id": "pre_1",
                })
        self.assertIsNotNone(accepted)
        self.assertIsNone(rejected)

    def test_expected_clean_control_slots(self) -> None:
        slots = analysis.expected_control_slots(self.protocol)
        self.assertEqual(slots[:5], [f"pre_{index}" for index in range(1, 6)])
        self.assertEqual(
            slots[5:9], ["mid_100", "mid_200", "mid_300", "mid_400"])
        self.assertEqual(
            slots[-5:], [f"post_{index}" for index in range(1, 6)])
        self.assertEqual(len(slots), 14)

    def test_clean_control_replacement_requires_documentation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            disposition = Path(directory) / "pre_1.json"
            disposition.write_text(json.dumps({
                "decision": "infrastructure_failure_replace",
                "control_slot": "pre_1", "attempt": 0,
                "run_id": "pre_1", "prior_interval_impact": "isolated",
                "justification": "falla acotada al proceso del control",
            }))
            slots = analysis.expected_control_slots(self.protocol)
            progress = []
            for slot in slots:
                run_id = "pre_1_r1" if slot == "pre_1" else slot
                progress.append({
                    "event_id": f"control_{slot}_attempt_0_valid",
                    "kind": "control", "outcome": "valid",
                    "plan_execution_order": slot, "run_id": run_id,
                    "run_dir": f"/runs/{run_id}",
                    "audit_report": "",
                })

            def valid_control(
                    _path, _protocol, _protocol_hash, _plan_hash, run_id):
                return {
                    "status": "valid_control", "valid": True,
                    "run_id": run_id, "byteSentUniqueTotal": 100.0,
                    "payload_bytes_read": 100.0,
                }

            with mock.patch.object(
                    analysis, "safe_audit_clean_control_run",
                    side_effect=valid_control):
                rejected = analysis.audit_clean_controls(
                    Path("/results"), self.protocol, "protocol", "plan",
                    progress)
                progress.append({
                    "event_id":
                        "control_pre_1_attempt_0_replacement_authorized",
                    "kind": "control", "outcome": "replacement_authorized",
                    "plan_execution_order": "pre_1", "run_id": "pre_1",
                    "run_dir": "/runs/pre_1",
                    "audit_report": str(disposition),
                })
                accepted = analysis.audit_clean_controls(
                    Path("/results"), self.protocol, "protocol", "plan",
                    progress)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(accepted["status"], "accepted")

    def test_possible_mid_control_impact_selects_prior_interval(self) -> None:
        clean = {
            "runs": [
                {
                    "control_slot": "mid_100",
                    "prior_interval_impacts": ["possible"],
                },
                {
                    "control_slot": "mid_200",
                    "prior_interval_impacts": ["isolated"],
                },
                {
                    "control_slot": "pre_1",
                    "prior_interval_impacts": ["possible"],
                },
            ]
        }
        affected = analysis.affected_execution_orders(clean, self.protocol)
        self.assertEqual(affected, list(range(1, 101)))

    def test_host_interval_sensitivity_is_secondary(self) -> None:
        protocol = dict(self.protocol)
        protocol["bootstrap_iterations"] = 100
        groups = {
            "assistant": [
                {"execution_order": order, "metrics": {"U": value}}
                for order, value in ((1, 0.991), (3, 0.992), (5, 0.993))
            ],
            "const10": [
                {"execution_order": order, "metrics": {"U": value}}
                for order, value in ((2, 0.990), (4, 0.991), (6, 0.992))
            ],
            "vanilla": [],
            "oracle10": [],
        }
        report = analysis.host_interval_sensitivity(
            groups, [1, 2], protocol)
        self.assertEqual(report["scope"], "secondary")
        self.assertEqual(report["status"], "reported")
        self.assertEqual(report["remaining_runs_by_arm"]["assistant"], 2)
        self.assertEqual(report["remaining_runs_by_arm"]["const10"], 2)
        self.assertIn("comparison", report)

    def test_invalid_control_impact_classification_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disposition.json"
            path.write_text(json.dumps({
                "decision": "infrastructure_failure_replace",
                "control_slot": "mid_100", "attempt": 0,
                "run_id": "mid_100", "prior_interval_impact": "unknown",
                "justification": "clasificación deliberadamente inválida",
            }))
            disposition = analysis.validated_control_disposition(
                path, self.protocol, {
                    "control_slot": "mid_100", "attempt": 0,
                    "run_id": "mid_100",
                })
        self.assertIsNone(disposition)

    def test_missing_clean_control_files_are_rejected_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = analysis.audit_clean_control_run(
                Path(directory), self.protocol, "protocol", "plan", "pre_1")
        self.assertEqual(report["status"], "invalid_control")
        self.assertFalse(report["valid"])

    def test_controller_readiness_disposition_retains_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disposition.json"
            path.write_text(json.dumps({
                "decision": "retain_primary_controller_readiness_failure",
                "run_id": 42,
                "justification": "el binario congelado no marcó v2_ready",
            }))
            status, disposition = analysis.resolve_controller_crosscheck(
                "assistant", False, path, self.protocol, "42")
        self.assertEqual(status, "readiness_failure_retained")
        self.assertIsNotNone(disposition)

    def test_controller_readiness_without_disposition_halts(self) -> None:
        status, disposition = analysis.resolve_controller_crosscheck(
            "assistant", False, None, self.protocol, "42")
        self.assertEqual(status, "discrepancy")
        self.assertIsNone(disposition)

    def test_set_ohead_error_is_detected(self) -> None:
        self.assertTrue(analysis.has_actuator_error([
            {"decision": ""}, {"decision": "set_ohead_error"},
        ]))
        self.assertFalse(analysis.has_actuator_error([
            {"decision": "v2_activate"}, {"decision": "v2_restore"},
        ]))

    def test_actuator_error_halts_before_integrity_replacement(self) -> None:
        status = analysis.classify_audit_status(
            False, True, "ready", True, "assistant", "not_required")
        self.assertEqual(status, "actuator_application_error")

    def test_retained_readiness_failure_remains_valid(self) -> None:
        status = analysis.classify_audit_status(
            True, False, "readiness_failure_retained", False, "assistant",
            "not_applicable_controller_readiness_failure")
        self.assertEqual(status, "valid")

    def test_atomic_json_replaces_previous_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.json"
            path.write_text('{"status":"old"}')
            analysis.write_json_atomic(path, {"status": "valid"})
            self.assertEqual(json.loads(path.read_text()), {"status": "valid"})
            self.assertEqual(list(path.parent.glob(".audit.json.tmp.*")), [])

    def test_auditor_exception_has_distinct_status(self) -> None:
        with mock.patch.object(
                analysis, "audit_run", side_effect=RuntimeError("boom")):
            report = analysis.safe_audit_run(Path("run"))
        self.assertEqual(report["status"], "audit_error")
        self.assertEqual(report["error_type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
