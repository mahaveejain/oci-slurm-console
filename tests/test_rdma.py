import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "scripts" / "rdma_per_link_bw_test.py"
spec = importlib.util.spec_from_file_location("rdma_test_helper", source)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class RdmaHelperTest(unittest.TestCase):
    def test_parse_perftest_average(self):
        output = """#bytes #iterations BW peak[Gb/sec] BW average[Gb/sec] MsgRate[Mpps]
65536 1000 200.00 196.05 0.38
"""
        self.assertEqual(helper.parse_bandwidth(output), 196.05)
        self.assertIsNone(helper.parse_bandwidth("client failed"))

    @patch.object(helper, "remote")
    def test_only_up_rdma_interfaces_are_discovered(self, remote):
        remote.return_value = "\n".join([
            "mlx5_0 port 1 ==> rdma0 (Up)",
            "mlx5_1 port 1 ==> rdma1 (Down)",
            "mlx5_2 port 1 ==> eth0 (Up)",
        ])
        self.assertEqual(helper.mappings("GPU-1"), {"rdma0": ("mlx5_0", 1)})

    def test_rejects_unsafe_hosts_before_ssh(self):
        for args in (["script", "GPU-1", "GPU-1"], ["script", "GPU-1", "-oProxyCommand=x"],
                     ["script", "ALL", "GPU-2"]):
            with self.subTest(args=args):
                self.assertEqual(helper.main(args), 2)


if __name__ == "__main__":
    unittest.main()
