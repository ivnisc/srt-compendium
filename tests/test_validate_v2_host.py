import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
ANALYZE_SPEC = importlib.util.spec_from_file_location(
    "analyze", ROOT / "analysis" / "analyze.py"
)
analyze = importlib.util.module_from_spec(ANALYZE_SPEC)
sys.modules["analyze"] = analyze
ANALYZE_SPEC.loader.exec_module(analyze)

HOST_SPEC = importlib.util.spec_from_file_location(
    "validate_v2_host", ROOT / "analysis" / "validate_v2_host.py"
)
host_validation = importlib.util.module_from_spec(HOST_SPEC)
HOST_SPEC.loader.exec_module(host_validation)


class HostValidationTests(unittest.TestCase):
    def test_accepts_performance_governor_on_physical_host(self):
        self.assertTrue(host_validation.cpu_policy_accepted(
            "performance", "none", "available"))

    def test_accepts_unavailable_cpufreq_on_virtualized_host(self):
        self.assertTrue(host_validation.cpu_policy_accepted(
            "unavailable", "kvm", "unavailable"))

    def test_rejects_uncontrolled_physical_host(self):
        self.assertFalse(host_validation.cpu_policy_accepted(
            "unavailable", "none", "unavailable"))

    def test_rejects_unknown_virtualization(self):
        self.assertFalse(host_validation.cpu_policy_accepted(
            "unavailable", "unknown", "unavailable"))


if __name__ == "__main__":
    unittest.main()
