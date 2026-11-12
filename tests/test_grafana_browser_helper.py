"""Helper access checks and browser session contract; no real login is made."""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location('grafana_helper', Path(__file__).resolve().parents[1] / 'scripts/grafana_browser_helper.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
TARGET = helper.DEFAULT_TARGET
ORIGIN = 'http://127.0.0.1:8080'


class HelperTests(unittest.TestCase):
    def handler(self, origin=ORIGIN, host='127.0.0.1:8765', token='Bearer test-token', url=TARGET):
        handler = helper.Handler.__new__(helper.Handler)
        handler.server = SimpleNamespace(server_port=8765, origins={ORIGIN}, token='test-token',
            targets={TARGET}, slots=threading.BoundedSemaphore(4))
        body = json.dumps({'url':url,'username':'test-user','password':'test-password'}).encode()
        handler.headers = {'Origin':origin,'Host':host,'Authorization':token,
                           'Content-Type':'application/json','Content-Length':str(len(body))}
        handler.path = '/open'
        handler.rfile = io.BytesIO(body)
        handler.connection = MagicMock()
        handler.reply = MagicMock()
        return handler

    @patch.object(helper, 'open_browser')
    def test_rejects_other_origins_hosts_tokens_and_destinations(self, launch):
        for options in [dict(origin='https://evil.example'),dict(origin='null'),
                        dict(host='evil.example:8765'),dict(token=''),
                        dict(url='http://169.254.169.254/login')]:
            with self.subTest(options=options):
                handler = self.handler(**options)
                handler.do_POST()
                self.assertEqual(handler.reply.call_args.args[0],403)
                launch.assert_not_called()

    @patch.object(helper, 'open_browser')
    def test_trusted_console_can_launch_only_configured_target(self, launch):
        handler = self.handler()
        launch.side_effect = lambda payload, report: report(200, {'ok':True})
        handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0],200)
        self.assertEqual(handler.server.slots._value,4)

    @patch.object(helper, 'open_browser')
    def test_rejects_large_non_json_and_busy_requests(self, launch):
        for key,value,status in [('Content-Length','9999999',400),('Content-Type','text/plain',415)]:
            handler = self.handler()
            handler.headers[key] = value
            handler.do_POST()
            self.assertEqual(handler.reply.call_args.args[0],status)
        handler = self.handler()
        handler.server.slots = threading.BoundedSemaphore(0)
        handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0],429)
        launch.assert_not_called()

    def test_context_cookie_login_and_no_credential_logging(self):
        for login_status,session_status,expected in [(200,200,True),(401,200,False),(200,401,False)]:
            with self.subTest(login=login_status,session=session_status):
                playwright = MagicMock()
                browser = playwright.chromium.launch.return_value
                context = browser.new_context.return_value
                context.request.post.return_value.status = login_status
                context.request.get.return_value.status = session_status
                browser.is_connected.return_value = False
                api = SimpleNamespace(sync_playwright=MagicMock())
                api.sync_playwright.return_value.__enter__.return_value = playwright
                payload = {'url':TARGET,'username':'user','password':'private-test-password'}
                report = MagicMock()
                with patch.dict(sys.modules, {'playwright':SimpleNamespace(),'playwright.sync_api':api}):
                    if login_status == 200 and session_status != 200:
                        with self.assertRaises(RuntimeError): helper.open_browser(payload,report)
                    else:
                        helper.open_browser(payload,report)
                playwright.chromium.launch.assert_called_once_with(channel='chrome',headless=False,chromium_sandbox=True)
                browser.new_context.assert_called_once_with(accept_downloads=False,no_viewport=True)
                if expected:
                    self.assertTrue(report.call_args.args[1]['ok'])
                    context.request.get.assert_called_once_with(TARGET.removesuffix('/login')+'/api/user',timeout=15000,max_redirects=0)
                    context.new_page.return_value.goto.assert_called_once_with(TARGET.removesuffix('/login')+'/',wait_until='domcontentloaded',timeout=30000)
                else:
                    context.new_page.assert_not_called()
                self.assertNotIn('private-test-password',str(report.call_args))
                self.assertEqual(payload,{})


if __name__ == '__main__':
    unittest.main()
