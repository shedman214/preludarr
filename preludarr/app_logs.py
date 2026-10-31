# SPDX-License-Identifier: GPL-3.0-or-later
"""Themearr Settings > Logs. Stdlib only; never accepts arbitrary filesystem paths.

Creates small rotating /config/logs/themearr.log files while preserving Docker stdout.
All browser log responses are authenticated by the existing Handler.do_GET guard.
"""
from __future__ import annotations

import html
import io
import logging
import logging.handlers
import os
from pathlib import Path

from login import brand_html, full_sidebar
import re
import sys
import threading
import time
import urllib.parse

MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 5
LOG_NAME = re.compile(r'\A(?:themearr|poster)\.log(?:\.[1-5])?\Z')
_INSTALLED = False
_LOGDIR = None
_LOGGER = None
_LOCK = threading.RLock()
_SETTINGS = {'access': True}
_HANDLERS = []


def apply_settings(level='info', max_mb=2, keep=5):
    """V1.9.7 Settings → General → Logging: Info hides successful web requests; size limit and archive count apply live."""
    _SETTINGS['access'] = level == 'debug'
    for handler in list(_HANDLERS):
        handler.maxBytes = max(1, min(50, int(max_mb))) * 1024 * 1024
        handler.backupCount = max(1, min(5, int(keep)))


def _poster_message(line):
    """Only safe poster events enter the dedicated downloadable poster log."""
    if line.startswith('POSTER_FILTERS_V1_4 event='):
        # V1.4 creates structured events containing only allowlisted fields.
        return line[:700]
    if line.startswith('Background poster worker started'):
        return 'Poster worker started'
    if line.startswith('Background poster worker error:'):
        return 'Poster worker error (' + _safe_error(line) + ')'
    if line.startswith('Poster AniList lookup:'):
        return 'Poster AniList lookup (' + _safe_error(line) + ')'
    if line.startswith('Poster lookup error ('):
        prefix = line.split(')', 1)[0].removeprefix('Poster lookup error (')
        provider = prefix if prefix in ('tmdb','tvdb') else 'unknown'
        return 'Poster lookup error provider=' + provider + ' reason=' + _safe_error(line)
    if line.startswith('Poster error:'):
        return 'Poster error (' + _safe_error(line) + ')'
    return None


def _safe_error(message):
    """Categorize legacy errors without copying private paths or request URLs."""
    m = re.search(r'HTTP Error\s+(\d{3})', message)
    if m:
        return 'HTTP_' + m.group(1)
    for name in ('ValueError','TimeoutError','HTTPError','URLError','OSError',
                 'PermissionError','FileNotFoundError','RuntimeError',
                 'ConnectionError','UnidentifiedImageError'):
        if name in message:
            return name
    return 'See_combined_log'

# BaseHTTPRequestHandler writes both successful requests and errors to stderr.
# Classify actual HTTP access records by their status, not their stream.
_ACCESS = re.compile(
    r'^\S+ - - \[[^\]\n]+\] "(?:GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD) '
    r'[^"\n]+ HTTP/\d(?:\.\d+)?" (?P<status>\d{3})\b'
)

# Defense in depth. Logs may still contain private paths and hostnames;
# treat downloads as private. Never show raw environment variables.
_SECRETS = [
    re.compile(r'(?i)(\b(?:api[_-]?key|apikey|token|secret|password|passwd|cookie|authorization|gpg_key|write_auth|session[_-]?id|x-plex-token)\b\s*[=:]\s*)([^\s&;,\"\']+)'),
    re.compile(r'(?i)(\bBearer\s+)([A-Za-z0-9._~+/-]{8,})'),
    re.compile(r'(?i)([?&](?:api_key|apikey|token|password|x-plex-token)=)([^\s&#]+)'),
]


def redact(s):
    s = str(s)
    for rx in _SECRETS:
        s = rx.sub(lambda m: m.group(1) + '[REDACTED]', s)
    return s


def permitted(name):
    return isinstance(name, str) and bool(LOG_NAME.fullmatch(name))


def log_dir(config):
    return Path(config) / 'logs'


class _PrivateRotatingHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        flags = os.O_APPEND | os.O_WRONLY | os.O_CREAT
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.baseFilename, flags, 0o600)
        return os.fdopen(fd, 'a', encoding='utf-8')


class _Tee(io.TextIOBase):
    def __init__(self, original, target, level, poster_target=None):
        self.original = original
        self.target = target
        self.level = level
        self.poster_target = poster_target
        self.pending = ''
        self.lock = threading.RLock()

    @property
    def encoding(self):
        return getattr(self.original, 'encoding', 'utf-8')

    def writable(self):
        return True

    def write(self, value):
        if not isinstance(value, str):
            value = str(value)
        # Always maintain Docker's original log stream; file mirroring is secondary.
        # V1.9.14: redacted too — before, `docker logs` showed ?apikey= / tokens that the log FILES already hid.
        count = self.original.write(redact(value))
        with self.lock:
            self.pending += value
            while '\n' in self.pending:
                line, self.pending = self.pending.split('\n', 1)
                clean_line = line.rstrip('\r')
                http = _ACCESS.match(clean_line) if self.level == logging.ERROR else None
                level = self.level
                if http:
                    status = int(http.group('status'))
                    level = logging.INFO if status < 400 else (logging.WARNING if status < 500 else logging.ERROR)
                    if status < 400 and not _SETTINGS['access']:
                        continue  # V1.9.7 Log level Info: successful web requests stay out of the log file (Docker log keeps them)
                safe_line = redact(clean_line)
                self.target.log(level, safe_line)
                if self.poster_target is not None:
                    poster_line = _poster_message(safe_line)
                    if poster_line is not None:
                        self.poster_target.log(level, poster_line)
            if len(self.pending) > 8192:
                self.target.log(self.level, redact(self.pending[:8192]))
                self.pending = self.pending[8192:]
        return count if isinstance(count, int) else len(value)

    def flush(self):
        return self.original.flush()

    def isatty(self):
        return False


def install(config):
    """Enable capped, private, redacted file mirroring. Never block app startup."""
    global _INSTALLED, _LOGDIR, _LOGGER
    if _INSTALLED:
        return True
    root = log_dir(config)
    try:
        if root.is_symlink():
            return False
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.is_dir() or root.is_symlink():
            return False
        target = root / 'themearr.log'
        if target.is_symlink():
            return False
        logger = logging.getLogger('themearr.private_file_v1')
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = _PrivateRotatingHandler(str(target), maxBytes=MAX_BYTES,
                                          backupCount=BACKUPS, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s',
                                             '%Y-%m-%d %H:%M:%S'))
        logger.addHandler(handler)
        _HANDLERS.append(handler)
        poster_target = root / 'poster.log'
        if poster_target.is_symlink():
            return False
        poster_logger = logging.getLogger('themearr.private_poster_file_v1_4_1')
        poster_logger.setLevel(logging.INFO)
        poster_logger.propagate = False
        poster_handler = _PrivateRotatingHandler(str(poster_target),
                                                 maxBytes=MAX_BYTES,
                                                 backupCount=BACKUPS,
                                                 encoding='utf-8')
        poster_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s',
                                                     '%Y-%m-%d %H:%M:%S'))
        poster_logger.addHandler(poster_handler)
        _HANDLERS.append(poster_handler)
        _LOGGER = logger
        _LOGDIR = root
        sys.stdout = _Tee(sys.stdout, logger, logging.INFO, poster_logger)
        sys.stderr = _Tee(sys.stderr, logger, logging.ERROR, poster_logger)
        _INSTALLED = True
        try:
            __import__('general_settings').apply_logging()  # saved Logging settings
        except Exception:
            pass
        return True
    except (OSError, ValueError):
        # Application remains usable with its existing Docker logging.
        return False


def _files(config):
    root = log_dir(config)
    if not root.is_dir() or root.is_symlink():
        return []
    rows = []
    for path in root.iterdir():
        if not permitted(path.name) or not path.is_file() or path.is_symlink():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        rows.append((path.name, st.st_size, st.st_mtime))
    return sorted(rows, key=lambda row: row[2], reverse=True)


def _read(config, name, limit=512*1024):
    if not permitted(name):
        raise FileNotFoundError('Invalid log file')
    root = log_dir(config)
    if root.is_symlink():
        raise FileNotFoundError('Logs unavailable')
    path = root / name
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        stream.seek(max(0, size - limit), os.SEEK_SET)
        blob = stream.read(limit)
    text = blob.decode('utf-8', errors='replace')
    if size > limit:
        text = '(Showing last ' + str(limit//1024) + ' KiB of this file)\n' + text
    return redact(text)


def _headers(handler, status, ctype, size, disposition=None):
    handler.send_response(status)
    handler.send_header('Content-Type', ctype)
    handler.send_header('Content-Length', str(size))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'SAMEORIGIN')
    handler.send_header('Referrer-Policy', 'no-referrer')
    if disposition:
        handler.send_header('Content-Disposition', disposition)
    handler.end_headers()


def _send(handler, status, text, kind='text/plain; charset=utf-8', disposition=None):
    if kind.startswith('text/html') and not disposition:
        text = __import__('login').add_icons(text)  # V1.9.7 site emblem
    data = text.encode('utf-8')
    _headers(handler, status, kind, len(data), disposition)
    handler.wfile.write(data)


def page(config, query=None):
    """Render a compact Sonarr-inspired log viewer beneath the Settings section."""
    query = query or {}
    files = _files(config)
    # The combined Log Viewer always shows Themearr activity.
    # Poster files are independently downloadable; no extra viewer is added.
    chosen = query.get('file', ['themearr.log'])[0]
    if not permitted(chosen) or chosen.startswith('poster.log'):
        chosen = 'themearr.log'
    search = (query.get('q', [''])[0] or '')[:100].strip()
    detail = query.get('view', [''])[0] == '1'
    if chosen not in {f[0] for f in files}:
        general = [f[0] for f in files if f[0].startswith('themearr.log')]
        if general:
            chosen = general[0]
        else:
            chosen = 'themearr.log'
    try:
        content = _read(config, chosen) if files else 'No log files yet. New Preludarr entries will appear here.'
    except OSError:
        content = 'This log file could not be opened.'
    lines = content.splitlines()[-500:]
    if search:
        lines = [line for line in lines if search.casefold() in line.casefold()]
    # Always escape any arbitrary log text as HTML, including error messages.
    log_view = html.escape('\n'.join(lines) if lines else ('No matching log entries.' if search else 'No entries yet.'))
    esc = lambda s: html.escape(str(s), quote=True)
    table = []
    for name, size, modified in files:
        sel = ' class="selected"' if name == chosen else ''
        download_link = '/logs/download?' + urllib.parse.urlencode({'file': name})
        display_name = name + (' — Poster activity' if name.startswith('poster.log') else ' — All activity')
        table.append(f'<tr{sel}><td>{esc(display_name)}</td><td>{size/1024:,.1f} KiB</td>'
                     f'<td>{esc(time.strftime("%Y-%m-%d %H:%M", time.localtime(modified)))}</td>'
                     f'<td><a href="{esc(download_link)}">Download</a></td></tr>')
    if not table:
        table = ['<tr><td colspan="4">No saved logs yet</td></tr>']
    query_parts = {'file': chosen, 'q': search}
    if detail:
        query_parts['view'] = '1'
    refresh = '/logs?' + urllib.parse.urlencode(query_parts)
    view_field = '<input type="hidden" name="view" value="1">' if detail else ''
    detail_nav = ('<p><a href="/logs">← Back to Log Files</a></p>' if detail else '')
    files_panel = ('' if detail else (
        f'<div class="panel"><h1>Log Files</h1>'
        f'<p class="muted">Combined app activity stays in the viewer. Poster activity is also saved separately in poster.log for download. Up to {BACKUPS} rotated files per log, '
        f'{MAX_BYTES//1048576} MiB each. Viewing and downloads are redacted, but may '
        f'still include private server paths; keep them private.</p>'
        f'<table><thead><tr><th>Filename</th><th>Size</th><th>Last Write Time</th>'
        f'<th>Actions</th></tr></thead><tbody>{"".join(table)}</tbody></table></div>'
    ))
    selected_status = ('<p class="muted">Selected log file: '+esc(chosen)+'</p>'
                       if detail else '')
    css = '''
:root{color-scheme:dark}*{box-sizing:border-box}
body{font:15px system-ui,-apple-system,sans-serif;background:#0e1525;color:#f1f5ff;margin:0}
.shell{display:grid;grid-template-columns:235px 1fr;min-height:100vh}
aside{background:#101827;border-right:1px solid #30415e;padding:28px 16px}
.brand{font-weight:800;font-size:21px;margin-bottom:24px}
a{color:#85b6ff;text-decoration:none}a:hover{text-decoration:underline}
aside a{display:block;padding:10px 12px;border-radius:7px;color:#d4def1}
aside a.selected{background:#233854;color:#fff;font-weight:700}
main{padding:28px 32px;max-width:1500px;width:100%}
.crumb{font-size:13px;color:#a9bad6;margin-bottom:20px}
.panel{background:#1a2741;border:1px solid #344a70;border-radius:12px;padding:20px;margin-bottom:18px}
h1{margin:0 0 8px;font-size:25px}h2{font-size:19px;margin:0 0 15px}
.muted{color:#acbdd8;font-size:13px}table{width:100%;border-collapse:collapse}
th,td{padding:12px;text-align:left;border-bottom:1px solid #34445f;font-size:13px}
th{color:#c3d0e8}tr.selected{background:#203859}
button{border:0;background:#427cf0;color:#fff;font-weight:700;border-radius:6px;padding:10px 16px;cursor:pointer}
input{background:#101827;border:1px solid #516486;color:#fff;border-radius:5px;padding:10px;width:min(350px,100%)}
.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
pre{max-height:620px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:#0c1425;border:1px solid #354967;border-radius:8px;padding:18px;font:12px/1.6 ui-monospace,Consolas,monospace;color:#e2eaf6}
.shell.tm-shell{display:flex}.shell.tm-shell>main{flex:1;min-width:0}@media(max-width:700px){.shell.tm-shell{display:block}}
@media(max-width:850px){.shell{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid #30415e;padding:14px}aside a{display:inline-block}main{padding:15px}}
'''
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Settings / Logs | Preludarr</title><style>{css}</style></head>
<body><div class="shell tm-shell">{full_sidebar('system')}<main>
<div class="crumb">Settings / <strong>Logs</strong></div>{files_panel}<div class="panel" id="log-viewer">
{detail_nav}<h2>{'Viewing log file' if detail else 'Log Viewer'} — {esc(chosen)}</h2>{selected_status}
<form class="controls" method="GET" action="/logs"><input type="hidden" name="file" value="{esc(chosen)}">{view_field}
<input type="search" name="q" placeholder="Search messages or errors" value="{esc(search)}" aria-label="Search log lines">
<button type="submit">Search</button><a href="{esc(refresh)}">Refresh</a>
<label class="muted"><input type="checkbox" id="log-auto" style="width:auto"> Auto-refresh (30s)</label></form>
<pre id="log-content">{log_view}</pre></div></main></div><script>
(function(){{var check=document.getElementById('log-auto');try{{check.checked=localStorage.getItem('themearr-auto-log-refresh')==='yes';}}catch(e){{}}
check.addEventListener('change',function(){{try{{localStorage.setItem('themearr-auto-log-refresh',check.checked?'yes':'no');}}catch(e){{}}}});
setInterval(function(){{if(check.checked)location.replace({refresh!r});}},30000);}})();
</script></body></html>'''


def route(handler, parsed, config):
    """Called only after the existing Themearr auth.get(handler) succeeds."""
    if parsed.path not in ('/logs', '/logs/download'):
        return _send(handler, 404, 'Not found')
    try:
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True,
                                      max_num_fields=5)
    except ValueError:
        return _send(handler, 400, 'Invalid request')
    if parsed.path == '/logs':
        return _send(handler, 200, page(config, query), 'text/html; charset=utf-8')
    name = query.get('file', [''])[0]
    if not permitted(name):
        return _send(handler, 400, 'Invalid log filename')
    try:
        data = _read(config, name, MAX_BYTES + 1024)
    except OSError:
        return _send(handler, 404, 'Log unavailable')
    return _send(handler, 200, data, 'text/plain; charset=utf-8',
                 'attachment; filename="' + name + '.txt"')
