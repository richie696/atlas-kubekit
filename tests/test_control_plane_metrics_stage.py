from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ControlPlaneMetricsStageTests(unittest.TestCase):
    def test_metrics_helper_is_distributed_with_native_stages(self):
        source = (ROOT / "02-deploy-cluster.sh").read_text()
        stages = re.search(r"^STAGES=\(([^\n]*)\)", source, re.MULTILINE)
        self.assertIsNotNone(stages)
        self.assertIn("configure-control-plane-metrics.sh", stages.group(1).split())

    def test_init_and_control_plane_join_invoke_metrics_helper(self):
        for relative in ("scripts/stages/init-cluster.sh", "scripts/stages/join-nodes.sh"):
            with self.subTest(stage=relative):
                source = (ROOT / relative).read_text()
                self.assertIn("configure-control-plane-metrics.sh", source)


if __name__ == "__main__":
    unittest.main()
