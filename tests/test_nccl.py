import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class NcclRankfileTest(unittest.TestCase):
    def run_setup(self, filename):
        script = (Path(__file__).resolve().parents[1] / "scripts/nccl_run_allreduce.sbatch").read_text()
        setup = script.split('mpivars_path=', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "ordering"
            helper.write_text("#!/bin/bash\n" + (
                f"printf 'rank 0=node-a slot=0\\n' > {filename}\n" if filename else "true\n"))
            helper.chmod(0o755)
            scontrol = root / "scontrol"
            scontrol.write_text("#!/bin/bash\nprintf 'node-a\\nnode-b\\n'\n")
            scontrol.chmod(0o755)
            setup = setup.replace("/opt/oci-hpc/nccl-test/build/all_reduce_perf", str(helper))
            setup = setup.replace("/opt/oci-hpc/bin/node_ordering_by_rack.py", str(helper))
            return subprocess.run(["/bin/bash", "-c", setup], capture_output=True, text=True,
                                  env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                                       "SLURM_CONSOLE_NCCL_LOG_DIR": str(root),
                                       "SLURM_JOB_NODELIST": "node-a,node-b"})

    def test_both_helper_versions_without_switch_file(self):
        for filename in ("ordered_rankfile", "rankfile_system_name"):
            with self.subTest(filename=filename):
                result = self.run_setup(filename)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Ordered ranks ({filename})", result.stdout)

    def test_missing_rankfile_stops_with_diagnostic(self):
        result = self.run_setup(None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Node ordering produced no nonempty rankfile", result.stderr)
