#!/usr/bin/env python3
"""Mac loopback bridge to sign into Grafana in a fresh Chrome Incognito context.
Credentials are accepted only from configured local console origins, passed to
the explicitly allowed Grafana address, and never logged or persisted here.
"""
import argparse
import hmac
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

HOST, PORT = '127.0.0.1', 8765
DEFAULT_TARGET = 'http://192.0.2.30:3000/login'


def login_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value) or '\\' in value:
        raise ValueError('Invalid address')
    url = urlsplit(value)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise ValueError('Invalid address')
    _ = url.port
    path = url.path.rstrip('/')
    if not path.endswith('/login'):
        path += '/login'
    return urlunsplit((url.scheme, url.netloc.lower(), path, '', ''))


def open_browser(payload, report):
    """Each invocation owns a new cookie jar and leaves its window open."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=False, chromium_sandbox=True)
        context = browser.new_context(accept_downloads=False, no_viewport=True)
        try:
            url = payload['url']
            signed_in = bool(payload['username'])
            if signed_in:
                # This request shares cookies with this fresh Incognito context only.
                response = context.request.post(url,
                    data={'user': payload['username'], 'password': payload['password']},
                    headers={'Content-Type': 'application/json'}, timeout=20000, max_redirects=0)
                payload.clear()
                if response.status != 200:
                    report(502, {'ok': False, 'error': 'Grafana rejected the sign-in. Check the saved username and password.'})
                    return
                base = url.removesuffix('/login')
                session = context.request.get(base + '/api/user', timeout=15000, max_redirects=0)
                if session.status != 200:
                    raise RuntimeError('Session not established')
                destination = base + '/'
            else:
                payload.clear()
                destination = url
            page = context.new_page()
            page.goto(destination, wait_until='domcontentloaded', timeout=30000)
            report(200, {'ok': True, 'signed_in': signed_in})
            # Keep the session alive while its Chrome window is in use.
            while browser.is_connected() and context.pages:
                page.wait_for_timeout(1000)
        finally:
            context.close()
            browser.close()


class HelperServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, origins, targets, port=PORT):
        super().__init__((HOST, port), Handler)
        self.origins = frozenset(origins)
        self.targets = frozenset(login_url(url) for url in targets)
        self.token = secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(4)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Request bodies, credentials, tokens and URLs must not enter logs.

    def trusted(self):
        return (self.headers.get('Host') == f'{HOST}:{self.server.server_port}'
                and self.headers.get('Origin') in self.server.origins)

    def reply(self, status, body):
        encoded = json.dumps(body).encode()
        self.send_response(status)
        if self.trusted():
            self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
            self.send_header('Vary', 'Origin')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Type', 'application/json')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        self.wfile.flush()

    def do_OPTIONS(self):
        if not self.trusted():
            return self.reply(403, {'error': 'Console origin not allowed.'})
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
        self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()

    def do_GET(self):
        if not self.trusted():
            return self.reply(403, {'error': 'Console origin not allowed.'})
        if self.path != '/status':
            return self.reply(404, {'error': 'Not found.'})
        self.reply(200, {'service': 'slurm-grafana-browser-helper', 'token': self.server.token})

    def do_POST(self):
        if not self.trusted():
            return self.reply(403, {'error': 'Console origin not allowed.'})
        if self.path != '/open':
            return self.reply(404, {'error': 'Not found.'})
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + self.server.token).encode()):
            return self.reply(403, {'error': 'Local helper token is missing or expired.'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reply(415, {'error': 'JSON required.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16384:
                raise ValueError()
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(size))
            url = login_url(payload.get('url'))
            if url not in self.server.targets:
                return self.reply(403, {'error': 'Grafana address is not allowed by the Mac helper. Restart it with --grafana-url for this address.'})
            user, password = payload.get('username', ''), payload.get('password', '')
            if not isinstance(user, str) or not isinstance(password, str) or bool(user) != bool(password):
                raise ValueError()
            payload = {'url': url, 'username': user, 'password': password}
            del user, password
        except (ValueError, AttributeError, TypeError, TimeoutError):
            return self.reply(400, {'error': 'Provide a valid address and both credentials, or neither.'})
        if not self.server.slots.acquire(blocking=False):
            return self.reply(429, {'error': 'Close an earlier helper Chrome window before opening another.'})
        reported = False

        def report(status, body):
            nonlocal reported
            reported = True
            self.reply(status, body)
        try:
            open_browser(payload, report)
        except Exception:
            # Errors from browser libraries can include request data. Never expose them.
            if not reported:
                report(502, {'ok': False, 'error': 'Could not open the Chrome session. Check Chrome installation, Grafana connectivity, and credentials.'})
        finally:
            payload.clear()
            self.server.slots.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', action='append', help='Exact allowed console origin; repeat for additional ports.')
    parser.add_argument('--grafana-url', action='append', help='Allowed Grafana login URL; repeat for more addresses.')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Run this helper on your Mac, not on the Slurm controller.')
    origins = args.origin or ['http://127.0.0.1:8080', 'http://localhost:8080']
    for origin in origins:
        url = urlsplit(origin)
        if url.scheme not in ('http', 'https') or url.hostname not in ('127.0.0.1', 'localhost') or url.path or url.query or url.fragment or url.username:
            parser.error('Console origins must be exact localhost or 127.0.0.1 origins without a path.')
    server = HelperServer(origins, args.grafana_url or [DEFAULT_TARGET])
    print(f'Grafana browser helper listening on http://{HOST}:{PORT} (Mac only).', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
