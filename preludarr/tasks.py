# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable, bounded background work and a shared resource policy.

Only background work obeys the window/global pause; explicit manual actions
retain their existing behavior. Cancellation is cooperative: an atomic install
already underway is never interrupted. A restart requeues unfinished tasks.
"""
import html
import json
import os
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path

PROFILES = {
    'eco': ('Eco', 1, 8, 3),
    'balanced': ('Balanced', 50, 2, 1),
    'performance': ('Performance', 50, 1, 1),
}
DEFAULTS = {'profile': 'balanced', 'paused': False, 'start': 0, 'end': 0,
            'busy_percent': 0, 'priority_root': ''}
MAX_PENDING = 1000
HISTORY = 500
WAKE = threading.Event()
_START = threading.Lock()
_THREAD = None
# All theme MP3 conversions (including collection conversion) share one slot.
# Keeping one safe publisher avoids added CPU/memory pressure on media servers.
CONVERSION_LOCK = threading.Lock()


class Deferred(Exception):
    pass


class Cancelled(Exception):
    pass


def schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS task_control (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS background_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, '
                 'task_key TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, due INTEGER NOT NULL, '
                 'created INTEGER NOT NULL, updated INTEGER NOT NULL, note TEXT NOT NULL, cancel INTEGER NOT NULL DEFAULT 0)')
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_background_task ON background_tasks(task_key) WHERE state IN ('queued','running')")
    conn.execute('CREATE TABLE IF NOT EXISTS auto_attempts (at INTEGER NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS auto_attempts_at ON auto_attempts(at)')
    conn.execute('CREATE TABLE IF NOT EXISTS arr_webhooks (instance TEXT PRIMARY KEY, token_hash TEXT NOT NULL)')
    if 'rerun' not in {r[1] for r in conn.execute('PRAGMA table_info(background_tasks)')}:
        conn.execute('ALTER TABLE background_tasks ADD COLUMN rerun INTEGER NOT NULL DEFAULT 0')


def setting(conn, key, default=''):
    row = conn.execute('SELECT value FROM task_control WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def put(conn, key, value):
    conn.execute('INSERT INTO task_control(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (key, str(value)))


def settings(app):
    with app.db() as conn:
        return _policy(setting(conn, 'policy', '{}'))


def _policy(raw):
    try:
        saved = json.loads(raw)
    except ValueError:
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    out = dict(DEFAULTS)
    if saved.get('profile') in PROFILES:
        out['profile'] = saved['profile']
    if isinstance(saved.get('paused'), bool):
        out['paused'] = saved['paused']
    for key, maximum in (('start', 23), ('end', 23), ('busy_percent', 200)):
        if type(saved.get(key)) is int and 0 <= saved[key] <= maximum:
            out[key] = saved[key]
    if isinstance(saved.get('priority_root'), str):
        out['priority_root'] = saved['priority_root']
    return out


def policy_reason(app, *, hour=None, load_percent=None):
    s = settings(app)
    if s['paused']:
        return 'Background work paused'
    hour = time.localtime().tm_hour if hour is None else hour
    start, end = s['start'], s['end']
    if start != end:
        inside = start <= hour < end if start < end else hour >= start or hour < end
        if not inside:
            return 'Outside processing hours'
    if s['busy_percent']:
        if load_percent is None:
            try:
                cpus = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else (os.cpu_count() or 1)
                load_percent = 100 * os.getloadavg()[0] / max(cpus, 1)
            except (OSError, AttributeError):
                load_percent = 0
        if load_percent >= s['busy_percent']:
            return 'Host load is above the configured limit'
    return ''


def queue_delay(app):
    return PROFILES[settings(app)['profile']][2]


def poster_delay(app, configured):
    return configured * PROFILES[settings(app)['profile']][3]


def automatic_limit(app, requested):
    return min(requested, PROFILES[settings(app)['profile']][1])


def take_auto_attempt(app, now=None):
    """One shared rolling scan-interval budget for scheduled and import-triggered installs.

    Count attempts, including failures, before lookup so retries or a restart
    cannot multiply requests. A manual Run now still has its explicit per-run cap.
    """
    s = app.auto_themes.settings()
    if s['mode'] == 'off':
        return False
    count, now = automatic_limit(app, s['per_scan']), int(time.time() if now is None else now)
    with app.db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM auto_attempts WHERE at<?', (now - 168*3600,))
        used = conn.execute('SELECT COUNT(*) FROM auto_attempts WHERE at>?', (now - s['interval_hours']*3600,)).fetchone()[0]
        if used >= count:
            return False
        conn.execute('INSERT INTO auto_attempts(at) VALUES(?)', (now,))
    return True


def enqueue(app, kind, key, payload, delay=0):
    if kind not in ('scan', 'arr', 'mapping'):
        raise ValueError('Unknown background task.')
    now = int(time.time())
    encoded = json.dumps(payload, separators=(',', ':'))
    if len(encoded) > 4096 or len(key) > 200:
        raise ValueError('Task is too large.')
    with app.db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute("SELECT id,state FROM background_tasks WHERE task_key=? AND state IN ('queued','running')", (key,)).fetchone()
        if old:
            if old[1] == 'running':
                # It may have read older settings already: run once more when it ends.
                conn.execute('UPDATE background_tasks SET rerun=1,payload=? WHERE id=?', (encoded, old[0]))
            return old[0], False
        pending = conn.execute("SELECT COUNT(*) FROM background_tasks WHERE state IN ('queued','running')").fetchone()[0]
        if pending >= MAX_PENDING:
            raise ValueError('Background task queue is full.')
        cur = conn.execute("INSERT INTO background_tasks(kind,task_key,payload,state,due,created,updated,note) "
                           "VALUES(?,?,?,'queued',?,?,?,'Waiting')", (kind, key, encoded, now+delay, now, now))
        task_id = cur.lastrowid
        conn.execute("DELETE FROM background_tasks WHERE state NOT IN ('queued','running') AND id NOT IN "
                     "(SELECT id FROM background_tasks WHERE state NOT IN ('queued','running') ORDER BY updated DESC,id DESC LIMIT ?)", (HISTORY,))
    WAKE.set()
    return task_id, True


def recover(app):
    with app.db() as conn:
        conn.execute("UPDATE background_tasks SET state='cancelled',note='Cancelled before restart finished',updated=? "
                     "WHERE state='running' AND cancel=1", (int(time.time()),))
        conn.execute("UPDATE background_tasks SET state='queued',note='Recovered after restart',due=? WHERE state='running'",
                     (int(time.time()),))


def checkpoint(app, task_id):
    with app.db() as conn:
        row = conn.execute('SELECT cancel FROM background_tasks WHERE id=?', (task_id,)).fetchone()
    if not row or row[0]:
        raise Cancelled('Stopped at a safe boundary.')
    reason = policy_reason(app)
    if reason:
        raise Deferred(reason)


def cancel_task(app, task_id):
    with app.db() as conn:
        now = int(time.time())
        n = conn.execute("UPDATE background_tasks SET state='cancelled',cancel=1,updated=?,note='Cancelled before starting' "
                         "WHERE id=? AND state='queued'", (now, task_id)).rowcount
        if not n:
            n = conn.execute("UPDATE background_tasks SET cancel=1,updated=?,note='Stopping after current safe step' "
                             "WHERE id=? AND state='running'", (now, task_id)).rowcount
    WAKE.set()
    return bool(n)


def _finish(app, task_id, state, note, delay=0):
    now = int(time.time())
    with app.db() as conn:
        conn.execute("UPDATE background_tasks SET state=CASE WHEN cancel=1 THEN 'cancelled' WHEN rerun=1 THEN 'queued' ELSE ? END,"
                     "note=CASE WHEN cancel=1 THEN 'Stopped after the last safe step' WHEN rerun=1 THEN 'Settings changed; running again' ELSE ? END,"
                     "due=?,updated=?,rerun=0 WHERE id=? AND state='running'",
                     (state, str(note)[:240], now+delay, now, task_id))


def step(app):
    if policy_reason(app) or not app.LOCK.acquire(blocking=False):
        return False
    row = None
    try:
        with app.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM background_tasks WHERE state='queued' AND due<=? "
                               "ORDER BY CASE kind WHEN 'arr' THEN 0 WHEN 'scan' THEN 1 ELSE 2 END,due,id LIMIT 1",
                               (int(time.time()),)).fetchone()
            if not row:
                return False
            conn.execute("UPDATE background_tasks SET state='running',note='Working',updated=? WHERE id=?", (int(time.time()), row['id']))
        app.STATUS.update(running=True, stage='scanning', active_path=None,
                          message={'scan': 'Scheduled library scan', 'arr': 'Processing media import', 'mapping': 'Mapping media-server titles'}[row['kind']])
        payload = json.loads(row['payload'])
        check = lambda: checkpoint(app, row['id'])
        check()
        if row['kind'] == 'scan':
            result = app.scan_library(download=app.auto_themes.settings()['mode'] != 'off', checkpoint=check, managed=True)
            app.STATUS['last_scan'] = app.now()
        elif row['kind'] == 'arr':
            result = __import__('arr_events').run(app, payload, check)
        else:
            result = __import__('media_mapping').rebuild(app, payload['server'], check)
        _finish(app, row['id'], 'done', result or 'Complete')
        if row['kind'] != 'scan':
            app.STATUS['message'] = result or 'Background task complete.'
    except Cancelled as error:
        _finish(app, row['id'], 'cancelled', str(error))
    except Deferred as error:
        _finish(app, row['id'], 'queued', str(error), delay=60)
    except Exception as error:
        if row is None:
            raise
        # No raw URLs, tokens or remote response bodies in the task log.
        _finish(app, row['id'], 'failed', 'Task could not finish (' + type(error).__name__ + '). Check the connection or run a scan again.')
        app.event('Background tasks', 'warning', 'A ' + row['kind'] + ' task could not finish (' + type(error).__name__ + ').')
    finally:
        if row is not None:
            app.STATUS.update(running=False, active_path=None, stage='idle')
        app.LOCK.release()
        if row is not None:
            app.drain_pending_scans()
    return row is not None


def _worker(app):
    recover(app)
    while True:
        WAKE.clear()
        try:
            did = step(app)
        except Exception as error:
            print('Preludarr task worker: ' + type(error).__name__, flush=True)
            did = False
        WAKE.wait(2 if did else 15)


def start(app):
    global _THREAD
    with _START:
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(target=_worker, args=(app,), name='preludarr-tasks', daemon=True)
            _THREAD.start()


def schedule_scan(app):
    return enqueue(app, 'scan', 'scheduled-scan', {})


def schedule_mappings(app):
    """Queue at most one rebuild per selected server daily, including after a restart."""
    from media_mapping import connections, signature
    now = int(time.time())
    with app.db() as conn:
        reports = {r['server_key']: (r['signature'], r['checked']) for r in conn.execute('SELECT server_key,signature,checked FROM media_mapping_report')}
        recent = {r['task_key'] for r in conn.execute("SELECT task_key FROM background_tasks WHERE kind='mapping' AND updated>?", (now-3600,))}
    for key, record in connections(app.CONFIG).items():
        config = record.get('library_mapping') or {}
        if not config.get('libraries') or not config.get('automatic'):
            continue
        task_key = 'mapping:' + key
        report = reports.get(key)
        if task_key not in recent and (not report or report[0] != signature(record) or report[1] <= now-86400):
            enqueue(app, 'mapping', task_key, {'server': key})


def _e(value):
    return html.escape(str(value), quote=True)


def body(app, query=None):
    query = query or {}
    s = settings(app)
    reason = policy_reason(app)
    with app.db() as conn:
        rows = conn.execute("SELECT * FROM background_tasks ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,updated DESC,id DESC LIMIT 150").fetchall()
        counts = dict(conn.execute('SELECT state,COUNT(*) FROM background_tasks GROUP BY state').fetchall())
    queue_counts = __import__('theme_queue').counts(app)
    queue_waiting = sum(queue_counts.get(k, 0) for k in ('queued', 'searching', 'approved', 'installing'))
    queue_review = queue_counts.get('proposed', 0)
    notice = query.get('notice', [''])[0][:180]
    options = ''.join('<option value="' + k + '"' + (' selected' if k == s['profile'] else '') + '>' + v[0] + '</option>' for k, v in PROFILES.items())
    roots = '<option value="">Newest first, all roots</option>' + ''.join(
        '<option value="' + _e(root) + '"' + (' selected' if str(root) == s['priority_root'] else '') + '>' + _e(label + ' · ' + str(root)) + '</option>'
        for label, root in app.ROOTS)
    parts = ['<section class="panel"><h2>Tasks &amp; Resource Management</h2>',
             '<p class="message" role="status">' + _e(notice) + '</p>' if notice else '',
             '<p><strong>' + _e(reason or 'Background work ready') + '</strong> · ' + str(counts.get('queued', 0)) + ' waiting · '
             + str(counts.get('running', 0)) + ' running.</p>',
             '<p class="muted">Background scans, import events, mapping, theme queue and background lookups follow this policy. Manual actions remain available. '
             'Pause or cancel stops at the next safe step, never during an atomic MP3 install. One theme conversion at a time; provider limits still apply.</p>',
             '<form method="post" action="/system/tasks"><input type="hidden" name="action" value="policy">',
             '<p><label>Resource profile <select name="profile">' + options + '</select></label></p>',
             '<p class="muted">Eco: at most one automatic attempt per interval, slower background posters and queue. '
             'Balanced: existing limits. Performance: shorter queue delay, without increasing provider or conversion limits.</p>',
             '<p><label>Process from (hour, 0–23) <input type="number" min="0" max="23" name="start" value="' + str(s['start']) + '"></label> ',
             '<label>until <input type="number" min="0" max="23" name="end" value="' + str(s['end']) + '"></label></p>',
             '<p class="muted">Container local time; equal hours mean all day. Overnight windows are supported.</p>',
             '<p><label>Defer at normalized 1-minute host load (%) <input type="number" name="busy_percent" min="0" max="200" value="' + str(s['busy_percent']) + '"></label> (0 disables)</p>',
             '<p><label>Automatic theme priority <select name="priority_root">' + roots + '</select></label></p>',
             '<button type="submit">Save policy</button></form>',
             '<form method="post" action="/system/tasks" style="margin-top:12px"><input type="hidden" name="action" value="' + ('resume' if s['paused'] else 'pause') + '">',
             '<button type="submit">' + ('Resume background work' if s['paused'] else 'Pause background work') + '</button></form>',
             '<p><a href="/queue?tab=waiting">Theme Queue</a>: ' + str(queue_waiting) + ' waiting / active, ' + str(queue_review) + ' ready for review · '
             '<a href="/?view=activity#poster-recovery">Poster Recovery</a>: ' + ('On' if app.POSTER_BACKGROUND_ENABLED else 'Off') + '</p>',
             '<p>'
             '<a href="/system?tab=backup">Backups</a> · <a href="/media-servers?tab=arr">Sonarr &amp; Radarr</a> · '
             '<a href="/media-servers/mapping">Media server mappings</a></p></section>',
             '<section class="panel"><h2>Managed tasks</h2><p class="muted">Pending tasks recover after restart. '
             'Import notifications for the same title are coalesced; scheduled scans remain the fallback. '
             'Cancellation never removes media or installed themes.</p><div class="table-scroll"><table>'
             '<tr><th>Task</th><th>State</th><th>Details</th><th>Updated</th><th>Action</th></tr>']
    for row in rows:
        label = {'scan': 'Library scan', 'arr': 'Sonarr / Radarr import', 'mapping': 'Media server mapping'}.get(row['kind'], 'Background task')
        form = ('<form method="post" action="/system/tasks"><input type="hidden" name="action" value="cancel">'
                '<input type="hidden" name="id" value="' + str(row['id']) + '"><button type="submit">Cancel</button></form>'
                if row['state'] in ('queued', 'running') and not row['cancel'] else '')
        parts.append('<tr><td>' + _e(label) + '</td><td>' + _e(row['state']) + '</td><td>' + _e(row['note']) + '</td><td>'
                     + _e(time.strftime('%Y-%m-%d %H:%M', time.localtime(row['updated']))) + '</td><td>' + form + '</td></tr>')
    if not rows:
        parts.append('<tr><td colspan="5">No tasks yet.</td></tr>')
    parts.append('</table></div><p><a href="/system?tab=tasks">Refresh task status</a></p></section>')
    parts.append(__import__('arr_events').settings_html(app))
    return ''.join(parts)


def post(app, handler, redirect):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 8192:
            raise ValueError('Invalid task form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=8)
        if any(len(v) != 1 for v in form.values()):
            raise ValueError('Invalid task form.')
        action = form.get('action', [''])[0]
        if action == 'cancel':
            raw = form.get('id', [''])[0]
            if not raw.isascii() or not raw.isdecimal() or len(raw) > 15:
                raise ValueError('Unknown task.')
            msg = 'Cancellation requested.' if cancel_task(app, int(raw)) else 'Task is no longer pending.'
        else:
            if action in ('pause', 'resume'):
                change = {'paused': action == 'pause'}
            elif action == 'policy':
                profile = form.get('profile', [''])[0]
                root = form.get('priority_root', [''])[0]
                if profile not in PROFILES or (root and root not in {str(r) for _, r in app.ROOTS}):
                    raise ValueError('Choose a valid resource profile and root.')
                change = {'profile': profile, 'priority_root': root}
                for key, maximum in (('start', 23), ('end', 23), ('busy_percent', 200)):
                    raw = form.get(key, [''])[0]
                    if not raw.isascii() or not raw.isdecimal() or len(raw) > 3 or not 0 <= int(raw) <= maximum:
                        raise ValueError('Invalid processing hour or load limit.')
                    change[key] = int(raw)
            else:
                raise ValueError('Unknown task action.')
            # Read and write under one write lock, so a concurrent pause is never overwritten.
            with app.db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                s = _policy(setting(conn, 'policy', '{}'))
                s.update(change)
                put(conn, 'policy', json.dumps(s))
            WAKE.set()
            msg = 'Background policy saved.'
    except (ValueError, OSError, UnicodeError, sqlite3.Error):
        msg = 'Task setting was not saved. Check the form or database.'
    return redirect(handler, '/system?' + urllib.parse.urlencode({'tab': 'tasks', 'notice': msg}))
