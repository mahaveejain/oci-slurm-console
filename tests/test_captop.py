import contextlib
import importlib.util
import io
import sys
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import CAPTOP_SCRIPT, _execution_args, _is_captop_command, app, validate_command

spec = importlib.util.spec_from_file_location('captop_report', CAPTOP_SCRIPT.with_suffix('.py'))
captop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(captop)


class ServiceError(Exception):
    status = 404
    code = 'NotAuthorizedOrNotFound'
    request_id = 'example-request'


class CaptopTest(unittest.TestCase):
    def sdk(self, pages):
        sdk = Mock()
        sdk.exceptions.ServiceError = ServiceError
        sdk.pagination.list_call_get_all_results.side_effect = [SimpleNamespace(data=page) for page in pages]
        return sdk

    def test_fixed_read_only_command_and_venv(self):
        self.assertEqual(validate_command('captop'), (['captop'], None))
        self.assertEqual(_execution_args(['captop']),
                         ['/usr/bin/env', f'CAPTOP_PYTHON={sys.executable}', '/bin/bash', str(CAPTOP_SCRIPT)])
        self.assertTrue(_is_captop_command(['captop', '--capacity-id', 'ocid1.computecapacitytopology.oc1.example']))
        for raw in ('captop --create', 'captop --queues-conf /etc/passwd',
                    'captop --region bad;whoami', 'captop --region',
                    'captop --region eu-frankfurt-1 --region us-ashburn-1'):
            with self.subTest(raw=raw):
                self.assertIsNotNone(validate_command(raw)[1])

    @patch('app._stream_fixed_helper', return_value=iter(['data: CAPTOP ID : example\n\n']))
    def test_report_streams_without_confirmation(self, stream):
        response = app.test_client().post('/api/command', json={'command': 'captop'})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'CAPTOP ID', response.data)
        stream.assert_called_once_with(['captop'], 120)

    @patch('app._stream_fixed_helper', side_effect=subprocess.TimeoutExpired('captop', 120))
    def test_report_timeout_is_clear(self, stream):
        response = app.test_client().post('/api/command', json={'command': 'captop'})
        self.assertIn(b'captop timed out after 120 seconds', response.data)

    def test_text_config_never_executes_content(self):
        with tempfile.NamedTemporaryFile(mode='w') as config:
            config.write('tenancy_ocid = "example" # note\ntenancy_ocid=example\n')
            config.flush()
            self.assertEqual(captop.config_value(config.name, 'tenancy_ocid', '='), 'example')

    def test_no_topology_is_an_error_not_zero_capacity(self):
        sdk = self.sdk([[]])
        output = io.StringIO()
        with patch.dict(sys.modules, {'oci': sdk}), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit):
                captop.main(['--tenancy-id', 'example', '--region', 'eu-frankfurt-1'])
        self.assertIn('No ACTIVE or UPDATING', output.getvalue())
        self.assertNotIn('Total :: 0', output.getvalue())
        sdk.core.ComputeClient.return_value.create_compute_capacity_topology.assert_not_called()

    def test_multiple_topologies_require_explicit_id(self):
        sdk = self.sdk([[SimpleNamespace(id='one', lifecycle_state='ACTIVE'),
                         SimpleNamespace(id='two', lifecycle_state='ACTIVE')]])
        with patch.dict(sys.modules, {'oci': sdk}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                captop.main(['--tenancy-id', 'example', '--region', 'eu-frankfurt-1'])

    def test_report_uses_supplied_state_mapping(self):
        hosts = [SimpleNamespace(id='host1', lifecycle_details='AVAILABLE', instance_id='instance1', instance_shape='BM.GPU'),
                 SimpleNamespace(id='host2', lifecycle_details='DEGRADED', instance_id=None, instance_shape='BM.GPU')]
        sdk = self.sdk([hosts])
        output = io.StringIO()
        with patch.dict(sys.modules, {'oci': sdk}), contextlib.redirect_stdout(output):
            captop.main(['--capacity-id', 'example', '--region', 'eu-frankfurt-1'])
        self.assertIn("'RUNNING': 1", output.getvalue())
        self.assertIn("'IN_REPAIR': 1", output.getvalue())
        self.assertIn('Total :: 2 BM.GPU', output.getvalue())
        self.assertIn('host2', output.getvalue())
        sdk.core.ComputeClient.return_value.list_compute_capacity_topologies.assert_not_called()

    def test_api_failure_does_not_report_zero_hosts(self):
        sdk = self.sdk([])
        sdk.pagination.list_call_get_all_results.side_effect = ServiceError()
        output = io.StringIO()
        with patch.dict(sys.modules, {'oci': sdk}), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit):
                captop.main(['--capacity-id', 'example'])
        self.assertIn('not authorized', output.getvalue())
        self.assertNotIn('Total :: 0', output.getvalue())
