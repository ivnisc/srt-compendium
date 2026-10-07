import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_v2_screening_plan",
    ROOT / "analysis" / "generate_v2_screening_plan.py",
)
PLAN = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PLAN)


class ScreeningPlanTests(unittest.TestCase):
    def test_plan_is_balanced_unique_and_checkpointed(self):
        rows = PLAN.build_rows("final_ab", "vanilla", "assistant", 3, 17, 2)
        experiments = [row for row in rows if row["kind"] == "experiment"]
        checkpoints = [row for row in rows if row["kind"] == "checkpoint"]

        self.assertEqual(len(experiments), 6)
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(
            {row["run_id"] for row in checkpoints},
            {"final_ab_checkpoint_0002", "final_ab_checkpoint_0004"},
        )
        self.assertEqual(
            len({(row["variant"], row["run_id"]) for row in experiments}), 6)
        self.assertEqual(
            sum(row["variant"] == "vanilla" for row in experiments), 3)
        self.assertEqual(
            sum(row["variant"] == "assistant" for row in experiments), 3)
        self.assertEqual([row["step"] for row in rows], list(range(1, 9)))

    def test_generation_is_deterministic(self):
        first = PLAN.render(
            PLAN.build_rows("final_ab", "vanilla", "assistant", 10, 29, 5))
        second = PLAN.render(
            PLAN.build_rows("final_ab", "vanilla", "assistant", 10, 29, 5))
        self.assertEqual(first, second)

    def test_frozen_plan_rejects_a_different_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            first = PLAN.render(
                PLAN.build_rows("final_ab", "vanilla", "assistant", 2, 7, 2))
            second = PLAN.render(
                PLAN.build_rows("final_ab", "vanilla", "assistant", 3, 7, 2))
            self.assertEqual(PLAN.freeze(path, first), "created")
            self.assertEqual(PLAN.freeze(path, first), "existing")
            with self.assertRaises(ValueError):
                PLAN.freeze(path, second)


if __name__ == "__main__":
    unittest.main()
