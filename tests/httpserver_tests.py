# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardened HTTP server (1.12 review, priority 1): slow clients, size limits, bounded concurrency. Run in a Preludarr image, --network none."""
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, '/src/preludarr')
import httpserver as hs  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


hs.HEADER_DEADLINE, hs.BODY_GRACE, hs.MIN_BODY_RATE, hs.MAX_PER_CLIENT, hs.MAX_BODY = 1.0, 1.0, 1000, 4, 10_000
hs.MAX_CONNECTIONS = 6


class H(hs.Hardened, BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == '/stream':
            self.send_response(200); self.send_header('Content-Type', 'application/x-ndjson'); self.end_headers()
            for i in range(3):
                self.wfile.write(b'{"step":%d}\n' % i); self.wfile.flush(); time.sleep(0.3)
            return
        body = b'ok'
        self.send_response(200); self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get('Content-Length', '0'))
        data = self.rfile.read(n)
        time.sleep(1.5)  # longer than the body grace: work after reading must not be cut off
        self.send_response(200); self.send_header('Content-Length', str(len(str(len(data))))); self.end_headers(); self.wfile.write(str(len(data)).encode())


srv = hs.Server(('127.0.0.1', 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def raw(data=b'', wait=0.0, read=True):
    s = socket.create_connection(('127.0.0.1', port), timeout=5)
    if data:
        s.sendall(data)
    time.sleep(wait)
    out = b''
    if read:
        try:
            while chunk := s.recv(65536):
                out += chunk
        except OSError:
            pass
    s.close()
    return out


check('A normal request works', raw(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n').endswith(b'ok'))
out = raw(b'GET /stream HTTP/1.1\r\nHost: x\r\n\r\n')
check('Streaming responses (install progress) still arrive line by line', out.count(b'"step"') == 3, out[-80:])
t = time.time(); out = raw(b'GET / HTTP/1.1\r\nHost: x\r\n', wait=0)  # headers never finished
check('Unfinished headers: connection closed after the header deadline', out == b'' and time.time() - t < 4, (out, time.time() - t))
t = time.time(); out = raw()
check('Idle connection (sends nothing) is closed after the header deadline', out == b'' and time.time() - t < 4)
out = raw(b'POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 999999\r\n\r\n')
check('Too-large body refused with 413 before any route runs', out.startswith(b'HTTP/1.0 413'), out[:40])
out = raw(b'POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n')
check('Chunked bodies refused (411)', out.startswith(b'HTTP/1.0 411'), out[:40])
out = raw(b'POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\nhello')
check('Duplicate Content-Length refused (400)', out.startswith(b'HTTP/1.0 400'), out[:40])
t = time.time(); out = raw(b'POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5000\r\n\r\nabc')  # body never completes
check('Body that stops arriving is cut off (grace + size / minimum rate)', b'200' not in out and time.time() - t < 10, (out[:40], time.time() - t))
out = raw(b'POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello')
check('A complete body is processed even when the work takes longer than the grace', out.endswith(b'5'), out[-40:])

idle = [socket.create_connection(('127.0.0.1', port)) for _ in range(hs.MAX_PER_CLIENT)]
time.sleep(0.2)
out = raw(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
check('One client cannot hold more than MAX_PER_CLIENT connections: the next gets 503', out.startswith(b'HTTP/1.0 503'), out[:40])
for s in idle:
    s.close()
time.sleep(1.5)
check('Slots are released after the idle connections time out or close', raw(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n').endswith(b'ok'))
check('Responses are HTTP/1.0 (no keep-alive slots)', raw(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n').startswith(b'HTTP/1.0 200'))

src = open('/src/preludarr/app.py').read()
check('The app uses the hardened server and handler', 'httpserver.Server((' in src and 'class Handler(httpserver.Hardened, BaseHTTPRequestHandler)' in src)
srv.shutdown()
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
