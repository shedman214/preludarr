# SPDX-License-Identifier: GPL-3.0-or-later
"""Trace diagnostics through a real HTTP request, with no credentials in trace records."""
import io
import logging
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
import app_logs as logs

results = []
def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name)

stream = io.StringIO()
logger = logging.getLogger('trace.tests')
logger.propagate = False
handler = logging.StreamHandler(stream)
handler.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
logger.addHandler(handler)
logs._LOGGER = logger
completed = threading.Event()
class Handler(logs.RequestTrace, BaseHTTPRequestHandler):
    def handle_one_request(self):
        try:
            return super().handle_one_request()
        finally:
            completed.set()
    def do_GET(self):
        self.send_response(204)
        self.end_headers()
    def log_message(self, *args):
        pass

server = HTTPServer(('127.0.0.1', 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
def request():
    completed.clear()
    req = urllib.request.Request('http://127.0.0.1:%s/private?token=private-value' % server.server_port,
                                 headers={'Authorization': 'Bearer secret-value'})
    with urllib.request.urlopen(req, timeout=5) as response:
        status = response.status
    assert completed.wait(5), 'Request did not finish'
    return status
try:
    for level in ('info', 'debug'):
        logs.apply_settings(level)
        request()
        check(level + ' omits trace details', not stream.getvalue())
    logs.apply_settings('trace')
    check('Trace preserves HTTP response', request() == 204)
    text = stream.getvalue()
    check('Trace records method, status and duration', 'TRACE HTTP request method=GET status=204 duration_ms=' in text)
    check('Trace excludes URL and credentials', all(s not in text for s in ('private', 'token', 'secret', 'Authorization')))
    stream.seek(0); stream.truncate(0)
    logs.apply_settings('info')
    request()
    check('Switching back disables trace immediately', not stream.getvalue())
finally:
    server.shutdown(); server.server_close(); thread.join(timeout=5)
    logger.removeHandler(handler)
print('%s/%s passed' % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
