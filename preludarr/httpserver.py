# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardened HTTP server for Preludarr (1.12 pre-release review, priority 1).

Python's ThreadingHTTPServer starts one thread per connection with no timeouts, so slow or idle clients (signed in or not)
could hold threads forever. This keeps the standard-library server — every route, the sign-in and same-origin checks,
streaming install progress, uploads and audio previews work unchanged — and adds:
  * bounded concurrency: at most MAX_CONNECTIONS requests at once and MAX_PER_CLIENT per client address;
    extra connections get an immediate 503 instead of a thread;
  * socket timeout: SOCKET_TIMEOUT seconds for any single read or write;
  * header deadline: the request line and headers must arrive within HEADER_DEADLINE seconds;
  * body deadline: a request body must arrive at MIN_BODY_RATE bytes/second or better (plus BODY_GRACE seconds);
  * request size: Content-Length above MAX_BODY is refused (413) before any route runs; chunked bodies are refused (411);
  * HTTP/1.0 responses (no keep-alive), so a finished request never keeps its slot.
Limits can be tuned with PRELUDARR_HTTP_MAX_CONNECTIONS (16-512) and PRELUDARR_HTTP_MAX_PER_CLIENT (4-128).
"""
import os
import socket
import threading
from http.server import ThreadingHTTPServer


def _env_int(name, default, low, high):
    try:
        return min(high, max(low, int(os.environ.get(name, default))))
    except ValueError:
        return default


MAX_CONNECTIONS = _env_int('PRELUDARR_HTTP_MAX_CONNECTIONS', 64, 16, 512)
MAX_PER_CLIENT = _env_int('PRELUDARR_HTTP_MAX_PER_CLIENT', 16, 4, 128)
SOCKET_TIMEOUT = 60
HEADER_DEADLINE = 20
BODY_GRACE = 20
MIN_BODY_RATE = 16 * 1024          # bytes per second
MAX_BODY = 48 * 1024 * 1024         # largest legitimate request: a 40 MB audio upload + form overhead
_BUSY = (b'HTTP/1.0 503 Service Unavailable\r\nContent-Type: text/plain\r\nContent-Length: 20\r\nRetry-After: 5\r\n'
         b'Connection: close\r\n\r\nServer busy, retry.\n')


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._per_client = {}
        self._count_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def _client(self, address):
        return address[0] if isinstance(address, tuple) and address else str(address)

    def process_request(self, request, client_address):
        who = self._client(client_address)
        with self._count_lock:
            busy = self._per_client.get(who, 0) >= MAX_PER_CLIENT
            if not busy:
                busy = not self._slots.acquire(blocking=False)
            if not busy:
                self._per_client[who] = self._per_client.get(who, 0) + 1
        if busy:
            try:
                request.settimeout(2)
                request.sendall(_BUSY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(who)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(self._client(client_address))

    def handle_error(self, request, client_address):
        """Dropped, timed-out or cut-off connections are normal under these limits: one short log line, no traceback."""
        import sys
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError, socket.timeout)):
            print('HTTP connection from ' + self._client(client_address) + ' closed early: ' + type(error).__name__, flush=True)
            return
        super().handle_error(request, client_address)

    def _release(self, who):
        with self._count_lock:
            n = self._per_client.get(who, 0) - 1
            if n > 0:
                self._per_client[who] = n
            else:
                self._per_client.pop(who, None)
        self._slots.release()


class _BodyReader:
    """Wraps rfile: once the declared body has been read, the body deadline is cancelled."""

    def __init__(self, raw, length, done):
        self._raw, self._left, self._done = raw, length, done

    def _count(self, n):
        self._left -= n
        if self._left <= 0:
            self._done()

    def read(self, size=-1):
        data = self._raw.read(size)
        self._count(len(data))
        return data

    def readline(self, size=-1):
        data = self._raw.readline(size)
        self._count(len(data))
        return data

    def readinto(self, buffer):
        n = self._raw.readinto(buffer) or 0
        self._count(n)
        return n

    def __getattr__(self, name):
        return getattr(self._raw, name)


class Hardened:
    """Mix in before BaseHTTPRequestHandler: class Handler(Hardened, BaseHTTPRequestHandler)."""
    timeout = SOCKET_TIMEOUT
    protocol_version = 'HTTP/1.0'

    def _abort(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _cancel(self, name):
        timer = getattr(self, name, None)
        if timer is not None:
            timer.cancel()
            setattr(self, name, None)

    def handle_one_request(self):
        self._header_timer = threading.Timer(HEADER_DEADLINE, self._abort)
        self._header_timer.daemon = True
        self._header_timer.start()
        try:
            super().handle_one_request()
        finally:
            self._cancel('_header_timer')
            self._cancel('_body_timer')

    def send_response(self, *args, **kwargs):
        self._cancel('_body_timer')  # answering: the body phase is over
        return super().send_response(*args, **kwargs)

    def parse_request(self):
        ok = super().parse_request()
        self._cancel('_header_timer')
        if not ok:
            return False
        if self.headers.get('Transfer-Encoding'):
            self.send_error(411, 'Content-Length required')
            return False
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) > 1:
            self.send_error(400, 'Duplicate Content-Length')
            return False
        if lengths:
            try:
                length = int(lengths[0])
                if length < 0:
                    raise ValueError
            except ValueError:
                self.send_error(400, 'Invalid Content-Length')
                return False
            if length > MAX_BODY:
                self.send_error(413, 'Request too large')
                return False
            if length:
                self._body_timer = threading.Timer(BODY_GRACE + length / MIN_BODY_RATE, self._abort)
                self._body_timer.daemon = True
                self._body_timer.start()
                self.rfile = _BodyReader(self.rfile, length, lambda: self._cancel('_body_timer'))
        return True
