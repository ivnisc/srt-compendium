import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
