# SPDX-License-Identifier: GPL-3.0-or-later
"""Automatic theme.mp3 per user choice + "Recently added" (Themearr V1.7.3).

Settings → Media Management → Automatic Themes:
  * Off (default unless the Docker AUTO_DOWNLOAD=true was already set),
  * New titles only — folders that first appear after automatic themes were switched on,
  * All missing — every title without a theme, newest first.
Plus: how many installs per scan and how often scans run. Installs always go through the existing
automatic path (Original Matching + year checks, atomic no-overwrite install): an existing theme.mp3 is
never replaced and every folder keeps exactly one theme.mp3.

"Recently added" uses a separate table show_added(path, added, baseline); older Themearr versions ignore
it, so rolling back is safe. Titles already in the library when V1.7.3 first starts are the baseline
(their real arrival date is unknown and is not guessed); every folder found afterwards gets its date.
"""
import html
import json
import os
import time
import urllib.parse
from pathlib import Path

FILE = 'themearr_auto.json'
MODES = {'off': 'Off', 'new': 'New titles only', 'all': 'All missing titles'}
_STATE = {'config': None, 'defaults': {}}


def _e(value):
    return html.escape(str(value), quote=True)


def init(app):
    """Startup: remember defaults from Docker variables, create the table, mark today's titles as baseline."""
    _STATE['config'] = Path(app.CONFIG)
    _STATE['defaults'] = {'mode': 'all' if app.AUTO else 'off', 'per_scan': app.MAX_PER_SCAN,
                          'interval_hours': max(1, app.INTERVAL // 3600), 'recent_days': 14, 'enabled_at': 0}
    _baseline(app)


def _baseline(app):
    """First time only (table empty): every title already in the library is 'older', not 'recently added'."""
    with app.db() as c:
        if c.execute('SELECT COUNT(*) FROM show_added').fetchone()[0] == 0:
            c.execute('INSERT OR IGNORE INTO show_added SELECT path, 0, 1 FROM shows')


def settings():
    data = dict(_STATE['defaults'])
    try:
        saved = json.loads((_STATE['config'] / FILE).read_text())
        if saved.get('mode') in MODES:
            data['mode'] = saved['mode']
        for key, lo, hi in (('per_scan', 1, 50), ('interval_hours', 1, 168), ('recent_days', 1, 365)):
            if isinstance(saved.get(key), int) and lo <= saved[key] <= hi:
                data[key] = saved[key]
        if isinstance(saved.get('enabled_at'), int):
            data['enabled_at'] = saved['enabled_at']
    except (OSError, ValueError, TypeError):
        pass
    return data


def _save(data):
    path = _STATE['config'] / FILE
    tmp = path.with_name('.' + FILE + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def record_new(app):
    """After every scan: give folders seen for the first time their 'added' date."""
    _baseline(app)
    with app.db() as c:
        c.execute("INSERT OR IGNORE INTO show_added SELECT path, CAST(strftime('%s','now') AS INTEGER), 0 FROM shows")


def recent_clause():
    """SQL fragment + params for the 'Recently added' status (used by the dashboard and the poster filters)."""
    cutoff = int(time.time()) - settings()['recent_days'] * 86400
    return 'path IN (SELECT path FROM show_added WHERE baseline=0 AND added>=?)', [cutoff]


def to_install(app):
    """Missing titles the automatic run may try now, per the user's choice (newest first)."""
    s = settings()
    if s['mode'] == 'off':
        return []
    sql = ("SELECT s.path FROM shows s LEFT JOIN show_added a ON a.path=s.path WHERE s.state='missing'"
           " AND s.library IN ('Anime TV','Anime Movies')")  # Preludarr 1.11: Shows / Movies are never auto-installed
    params = []
    if s['mode'] == 'new':
        sql += ' AND a.baseline=0 AND a.added>=?'
        params.append(s['enabled_at'])
    sql += ' ORDER BY COALESCE(a.added,0) DESC, s.name LIMIT ?'
    params.append(s['per_scan'])
    with app.db() as c:
        return [r['path'] for r in c.execute(sql, params).fetchall()]


def interval_seconds():
    return settings()['interval_hours'] * 3600


def summary():
    s = settings()
    return f"Automatic themes: {MODES[s['mode']]} · every {s['interval_hours']} h · up to {s['per_scan']} per scan"


# ---------------------------------------------------------------------------------- UI ----
def settings_html(app, notice=''):
    s = settings()
    with app.db() as c:
        missing = c.execute("SELECT COUNT(*) FROM shows WHERE state='missing'").fetchone()[0]
        sql, params = recent_clause()
        recent = c.execute('SELECT COUNT(*) FROM shows WHERE ' + sql, params).fetchone()[0]
    since = time.strftime('%Y-%m-%d %H:%M', time.localtime(s['enabled_at'])) if s['enabled_at'] else ''
    radios = ''.join(
        '<label class="check" style="display:flex;gap:8px;align-items:flex-start;margin:8px 0"><input type="radio" name="mode" value="' + k + '"'
        + (' checked' if s['mode'] == k else '') + '><span><strong>' + v + '</strong><br><small class="muted">' + d + '</small></span></label>'
        for k, v, d in (('off', MODES['off'], 'Preludarr only installs themes when you ask (Theme Browser, Download / Retry, bulk review).'),
                        ('new', MODES['new'], 'Only folders that appear after you switch this on — your existing missing titles stay manual.'),
                        ('all', MODES['all'], 'Also works through the %d titles missing a theme today, newest first, a few per scan.' % missing)))
    return ('<section class="panel" id="automatic-themes"><h2>Automatic Themes</h2>'
            + ('<p class="notice" role="status">' + _e(notice) + '</p>' if notice else '') +
            '<p class="muted">When on, each scheduled scan installs theme.mp3 for up to the set number of titles, using Preludarr’s '
            'automatic matching (exact title + year checks — the same rules as Download / Retry). An existing theme.mp3 is never replaced; '
            'titles it cannot match safely are marked “Needs review” for you.</p>'
            '<form method="post" action="/media-management/auto">' + radios +
            ('<p class="muted">New titles only: counting folders found since ' + _e(since) + '.</p>' if s['mode'] == 'new' and since else '') +
            '<div style="display:flex;gap:16px;flex-wrap:wrap">'
            '<label class="field"><span>Installs per scan</span><input type="number" name="per_scan" min="1" max="50" value="' + str(s['per_scan']) + '"></label>'
            '<label class="field"><span>Scan every (hours)</span><input type="number" name="interval_hours" min="1" max="168" value="' + str(s['interval_hours']) + '"></label>'
            '<label class="field"><span>“Recently added” covers (days)</span><input type="number" name="recent_days" min="1" max="365" value="' + str(s['recent_days']) + '"></label>'
            '</div><button type="submit">Save</button></form>'
            '<form method="post" action="/media-management/auto-run" style="margin-top:10px"><button type="submit"'
            + (' disabled title="Switch Automatic Themes on first"' if s['mode'] == 'off' or app.STATUS.get('running') else '') +
            '>Run now</button> <small class="muted">Scans, then installs up to ' + str(s['per_scan']) + ' theme(s) per the choice above.</small></form>'
            '<p class="muted">Recently added right now: <a href="/?state=recent#library-results">' + str(recent) + ' title' + ('' if recent == 1 else 's') +
            '</a> (titles that were already there when Preludarr first scanned the folder count as older).</p></section>'
            '<style>#automatic-themes .field input{padding:10px;background:#101c30;color:#fff;border:1px solid #647da0;border-radius:7px;width:9em}</style>')


def post(app, handler, redirect):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 2048:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), max_num_fields=6)
        if any(len(v) != 1 for v in form.values()):
            raise ValueError('Invalid form.')
        mode = form.get('mode', [''])[0]
        if mode not in MODES:
            raise ValueError('Choose Off, New titles only or All missing titles.')
        values = {}
        for key, lo, hi in (('per_scan', 1, 50), ('interval_hours', 1, 168), ('recent_days', 1, 365)):
            raw = form.get(key, [''])[0]
            if not raw.isdigit() or not lo <= int(raw) <= hi:
                raise ValueError(f'{key.replace("_", " ").capitalize()} must be between {lo} and {hi}.')
            values[key] = int(raw)
        old = settings()
        enabled_at = old['enabled_at']
        if mode != 'off' and (old['mode'] == 'off' or not enabled_at):
            enabled_at = int(time.time())  # "new titles" = folders found from this moment on
        _save({'mode': mode, **values, 'enabled_at': enabled_at})
        if hasattr(app, 'event'):
            app.event('Automatic themes', 'settings', summary())
        msg = 'Saved. ' + summary() + '.'
    except (ValueError, UnicodeError, OSError) as error:
        msg = 'Not saved: ' + (str(error) if isinstance(error, ValueError) else 'settings could not be written.')
    return redirect(handler, '/media-management?' + urllib.parse.urlencode({'notice': msg[:190]}) + '#automatic-themes')


def run_now(app, handler, redirect):
    """POST /media-management/auto-run: one automatic run now (same rules as the scheduled one)."""
    if settings()['mode'] == 'off':
        msg = 'Automatic Themes is Off — nothing to run.'
    elif app.job(download=True):
        msg = 'Automatic run started: scanning, then up to %d install(s). Progress shows on the dashboard.' % settings()['per_scan']
    else:
        msg = 'Another scan or download is running; try again shortly.'
    return redirect(handler, '/media-management?' + urllib.parse.urlencode({'notice': msg}) + '#automatic-themes')
