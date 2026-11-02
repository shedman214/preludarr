# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise urllib's HTTP transport offline, including inherited proxy regression."""
import importlib
import io
import json
import os
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
import arr

results = []
def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name)

class Wire:
    def __init__(self, response):
        self.response = response
        self.sent = b''
    def makefile(self, *args, **kwargs):
        return io.BytesIO(self.response)
    def sendall(self, data):
        self.sent += data
    def close(self):
        pass
    def setsockopt(self, *args):
        pass

def response(status=200, body=b'{"appName":"Radarr"}', extra=b''):
    return (b'HTTP/1.1 ' + str(status).encode() + b' Result\r\nContent-Length: '
            + str(len(body)).encode() + b'\r\n' + extra + b'\r\n' + body)

inst = {'kind': 'radarr', 'url': 'http://192.168.1.134:7878/radarr', 'api_key': 'test-key-only'}
with patch.dict(os.environ, {'http_proxy': 'http://proxy.invalid:9999', 'https_proxy': 'http://proxy.invalid:9999',
                            'HTTP_PROXY': 'http://proxy.invalid:9999', 'HTTPS_PROXY': 'http://proxy.invalid:9999',
                            'no_proxy': '', 'NO_PROXY': ''}):
    importlib.reload(arr)
    wire = Wire(response())
    with patch('socket.create_connection', return_value=wire) as connect:
        result = arr._get(inst, 'system/status', timeout=12)
    check('LAN API bypasses inherited proxy', connect.call_args.args[0] == ('192.168.1.134', 7878))
    check('URL prefix and API response preserved', b'GET /radarr/api/v3/system/status HTTP/1.1' in wire.sent and result['appName'] == 'Radarr')
    check('API key stays in header, not URL', b'X-Api-Key: test-key-only' in wire.sent and b'test-key-only' not in wire.sent.split(b'\r\n')[0])
    wire = Wire(response(302, b'', b'Location: http://192.168.1.9:7777/steal\r\n'))
    with patch('socket.create_connection', return_value=wire) as connect:
        try:
            arr._get(inst, 'system/status')
            ok = False
        except urllib.error.HTTPError as error:
            ok = error.code == 302
    check('Redirects do not forward API keys', ok and connect.call_count == 1)
    with patch('socket.create_connection', side_effect=TimeoutError('private error text')):
        try:
            arr._get(inst, 'system/status')
            message = ''
        except urllib.error.URLError as error:
            message = arr._error_text(error, inst)
    check('Timeout explains container access without leaking private exception', 'Preludarr container' in message and 'private error text' not in message and 'test-key-only' not in message)
    with patch('socket.create_connection') as connect:
        try:
            arr._get(dict(inst, url='http://8.8.8.8:7878'), 'system/status')
            ok = False
        except ValueError:
            ok = True
    check('Address validation still rejects public HTTP before connecting', ok and not connect.called)
importlib.reload(arr)

# Simulated server latency at the wire boundary: no real waits or network.
# Reproduces the measured Radarr response (rootfolder: 15s, status: 0.1s).
class SlowRadarr(Wire):
    def __init__(self, timeout, root_delay=15, status_delay=0.1):
        super().__init__(b'')
        self.timeout, self.root_delay, self.status_delay = timeout, root_delay, status_delay
    def makefile(self, *args, **kwargs):
        route = self.sent.split(b'\r\n')[0]
        if b'/rootfolder ' in route:
            delay, payload = self.root_delay, [{'path': '/data/movies'}]
        elif b'/system/status ' in route:
            delay, payload = self.status_delay, {'appName': 'Radarr', 'version': '5.9.1'}
        else:
            delay, payload = 0, {'movieFolderFormat': '{Movie Title} ({Release Year})'}
        if delay > self.timeout:
            class DelayedHeaders(io.BytesIO):
                def readline(self, *args, **kwargs):
                    raise TimeoutError('Simulated server took longer than the socket timeout')
            return DelayedHeaders()
        return io.BytesIO(response(body=json.dumps(payload).encode()))

with patch('socket.create_connection', side_effect=lambda address, timeout, *args: SlowRadarr(timeout)):
    info = arr.test(inst)
check('Connection test accepts measured 15-second Radarr root response', info['roots'] == ['/data/movies'] and info['version'] == '5.9.1')
check('Naming settings are retained after slow root response', info['naming'].get('movieFolderFormat') == '{Movie Title} ({Release Year})')
with patch('socket.create_connection', side_effect=lambda address, timeout, *args: SlowRadarr(timeout, root_delay=35)):
    try:
        arr.test(inst)
        ok = False
    except (TimeoutError, urllib.error.URLError):
        ok = True
check('Unresponsive root-folder endpoint still times out', ok)
with patch('socket.create_connection', side_effect=lambda address, timeout, *args: SlowRadarr(timeout, status_delay=13)):
    try:
        arr.test(inst)
        ok = False
    except (TimeoutError, urllib.error.URLError):
        ok = True
check('Status check still fails promptly on an unresponsive server', ok)
print('%s/%s passed' % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
