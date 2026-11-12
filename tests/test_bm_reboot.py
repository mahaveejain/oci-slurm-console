import sys
from pathlib import Path
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import oci_node_reboot as reboot
from app import app, _lifecycle_confirmation, validate_command

INSTANCE = "ocid1.instance.oc1.test"


class BareMetalRebootTest(unittest.TestCase):
    def setUp(self):
        self.compute, self.network = Mock(), Mock()
        sdk = Mock()
        sdk.pagination.list_call_get_all_results.side_effect = lambda call, *a, **kw: call(*a, **kw)
        self.sdk = sdk
        self.network.list_subnets.return_value.data = [Obj(id="subnet", cidr_block="10.16.4.0/22")]
        self.network.list_private_ips.return_value.data = [Obj(vnic_id="vnic", is_primary=True, ip_address="10.16.5.175")]
        self.network.get_vnic.return_value.data = Obj(is_primary=True, private_ip="10.16.5.175")
        self.compute.list_vnic_attachments.return_value.data = [Obj(instance_id=INSTANCE, lifecycle_state="ATTACHED")]
        self.compute.get_instance.return_value = Obj(headers={"etag": "version1"}, data=Obj(
            id=INSTANCE, compartment_id="compartment", shape="BM.GPU.H100.8",
            lifecycle_state="RUNNING", display_name="worker"))
        self.compute.instance_action.return_value.headers = {"opc-request-id": "request"}
        self.client_patch = patch.object(reboot, "clients", return_value=(self.sdk, "compartment", self.compute, self.network, "us-chicago-1"))
        self.slurm_patch = patch.object(reboot, "slurm_target", return_value="10.16.5.175")
        self.client_patch.start()
        self.slurm_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(self.slurm_patch.stop)

    def test_resolve_is_read_only(self):
        target, _, etag = reboot.resolve("worker")
        self.assertEqual(target["instance_id"], INSTANCE)
        self.assertEqual(etag, "version1")
        self.compute.instance_action.assert_not_called()

    def test_hard_reboot_uses_reset_and_instance_version(self):
        reboot.hard_reboot("worker", INSTANCE)
        call = self.compute.instance_action.call_args
        self.assertEqual(call.args, (INSTANCE, "RESET"))
        self.assertEqual(call.kwargs["if_match"], "version1")
        self.assertIn("opc_retry_token", call.kwargs)

    def test_changed_mapping_blocks_reset(self):
        with self.assertRaisesRegex(ValueError, "mapping changed"):
            reboot.hard_reboot("worker", "ocid1.instance.oc1.other")
        self.compute.instance_action.assert_not_called()

    def test_virtual_machine_and_ambiguous_mapping_block_reset(self):
        self.compute.get_instance.return_value.data.shape = "VM.Standard.E5.Flex"
        with self.assertRaisesRegex(ValueError, "not bare metal"):
            reboot.hard_reboot("worker", INSTANCE)
        self.compute.get_instance.return_value.data.shape = "BM.GPU.H100.8"
        self.compute.list_vnic_attachments.return_value.data.append(Obj(instance_id="other", lifecycle_state="ATTACHED"))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            reboot.hard_reboot("worker", INSTANCE)
        self.compute.instance_action.assert_not_called()

    def test_resumed_node_blocks_reset(self):
        with patch.object(reboot, "slurm_target", side_effect=["10.16.5.175", ValueError("not drained")]):
            with self.assertRaisesRegex(ValueError, "not drained"):
                reboot.hard_reboot("worker", INSTANCE)
        self.compute.instance_action.assert_not_called()

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.subprocess.Popen")
    def test_confirmation_is_required_before_helper_runs(self, popen):
        raw = f"bm-hard-reboot worker {INSTANCE}"
        args, error = validate_command(raw)
        self.assertIsNone(error)
        self.assertEqual(_lifecycle_confirmation(args), "confirm")
        self.assertEqual(app.test_client().post('/api/command', json={"command": raw}).status_code, 400)
        popen.assert_not_called()
        for invalid in (f"bm-hard-reboot ALL {INSTANCE}", f"bm-hard-reboot worker,other {INSTANCE}",
                        "bm-hard-reboot worker --force"):
            self.assertIsNotNone(validate_command(invalid)[1])


class SlurmRebootEligibilityTest(unittest.TestCase):
    @patch.object(reboot.Path, "read_text", return_value="worker ansible_host=10.16.5.175 role=compute\n")
    @patch.object(reboot.subprocess, "run")
    def test_active_jobs_and_non_drained_nodes_are_rejected(self, run, _read):
        run.return_value.stdout = "NodeName=worker NodeAddr=10.16.5.175 State=IDLE CPUAlloc=0"
        with self.assertRaisesRegex(ValueError, "fully drained"):
            reboot.slurm_target("worker")
        run.side_effect = [Obj(stdout="NodeName=worker NodeAddr=10.16.5.175 State=DOWN+DRAIN CPUAlloc=0"),
                           Obj(stdout="12345\n")]
        with self.assertRaisesRegex(ValueError, "active jobs"):
            reboot.slurm_target("worker")
