# SPDX-License-Identifier: GPL-3.0-or-later
"""Successful theme installations and their saved metadata, without extra audio copies."""
import html
import json
import sqlite3
import stat
import time
from pathlib import Path

MAX_PER_TITLE = 200


def schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS theme_install_history (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, '
                 'at INTEGER NOT NULL, source TEXT NOT NULL, song TEXT NOT NULL, artist TEXT NOT NULL, track TEXT NOT NULL, '
                 'version TEXT NOT NULL, source_id TEXT NOT NULL, duration REAL, details TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS theme_history_path ON theme_install_history(path,id DESC)')


def clean(value, limit=200):
    return ''.join(c for c in str(value or '') if 32 <= ord(c) != 127)[:limit]


def track_metadata(track, anime_slug=''):
    return {'source': 'AnimeThemes', 'song': track.get('title'), 'artist': track.get('artist'),
            'track': track.get('slug') or track.get('type'), 'version': str(track.get('version') or ''),
            'source_id': f"{anime_slug}:{track.get('video_id') or ''}"}


def record(app, folder, *, metadata=None, slug=None, video_id=None, label=None, duration=None):
    """Run only after publication. A history failure never changes install success."""
    try:
        file = Path(folder) / 'theme.mp3'
        info = file.lstat()
        if not stat.S_ISREG(info.st_mode):
            return
        source = {'custom_upload': 'Custom upload', 'youtube_manual': 'YouTube', 'source_plex': 'Plex TV Themes',
                  'source_themerrdb': 'ThemerrDB', 'studio': 'Theme Studio', 'restore': 'Recycle bin'}.get(slug, 'AnimeThemes' if slug else 'Manual')
        meta = {'source': source, 'song': label, 'source_id': video_id if slug not in ('studio', 'custom_upload', 'restore') else ''}
        meta.update(metadata or {})
        if duration is None:
            try:
                duration, _ = __import__('custom_upload')._probe(file)
            except (ValueError, OSError):
                duration = None
        details = meta.get('details', '')
        if isinstance(details, dict):
            details = json.dumps(details, sort_keys=True, separators=(',', ':'))
        with app.db() as c:
            c.execute('INSERT INTO theme_install_history(path,at,source,song,artist,track,version,source_id,duration,details,size,mtime_ns) '
                      'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (str(folder), int(time.time()), clean(meta.get('source'), 100), clean(meta.get('song')),
                       clean(meta.get('artist')), clean(meta.get('track'), 40), clean(meta.get('version'), 40),
                       clean(meta.get('source_id'), 200), duration, clean(details, 600), info.st_size, info.st_mtime_ns))
            c.execute('DELETE FROM theme_install_history WHERE path=? AND id NOT IN '
                      '(SELECT id FROM theme_install_history WHERE path=? ORDER BY id DESC LIMIT ?)',
                      (str(folder), str(folder), MAX_PER_TITLE))
    except Exception as error:
        print(f'Preludarr installation history could not be saved: {type(error).__name__}', flush=True)


def rows(app, path, limit=30):
    with app.db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM theme_install_history WHERE path=? ORDER BY id DESC LIMIT ?',
                                          (str(path), min(MAX_PER_TITLE, max(1, limit))))]


def current(app, path):
    try:
        history = rows(app, path, 1)
    except sqlite3.Error:
        return None
    if not history:
        return None
    try:
        info = (Path(path) / 'theme.mp3').lstat()
        r = history[0]
        if stat.S_ISREG(info.st_mode) and (r['size'], r['mtime_ns']) == (info.st_size, info.st_mtime_ns):
            return r
    except OSError:
        return None
    return None


def _e(value):
    return html.escape(str(value or ''), quote=True)


def summary(meta):
    if not meta:
        return ''
    return ' · '.join(str(meta.get(k)) for k in ('source', 'track', 'song', 'artist') if meta.get(k))


def panel(app, path):
    try:
        history = rows(app, path)
    except sqlite3.Error:
        return '<details class="panel"><summary>Theme installation history</summary><p>History is temporarily unavailable.</p></details>'
    if not history:
        return '<details class="panel"><summary>Theme installation history</summary><p class="muted">No installations recorded yet. Existing themes are retained without guessed metadata.</p></details>'
    parts = [f'<details class="panel" id="theme-install-history"><summary>Theme installation history · {len(history)} recent entries</summary>',
             '<p class="muted">History begins with this update. It records successful installs, not extra audio copies. '
             'Previous audio kept on replacement can be restored from the <a href="/library-health/trash">recycle bin</a> during its retention period.</p>']
    if not current(app, path):
        parts.append('<p class="muted">The current file no longer matches the latest installation record; the history below is historical.</p>')
    parts.append('<div style="overflow-x:auto"><table><tr><th>When</th><th>Source / track</th><th>Song / artist</th><th>Duration</th></tr>')
    for r in history:
        track = ' · '.join(x for x in (r['source'], r['track'], f"Version {r['version']}" if r['version'] else '') if x)
        song = ' — '.join(x for x in (r['song'], r['artist']) if x) or 'Not recorded'
        duration = f"{r['duration']:.1f}s" if r['duration'] is not None else 'Unknown'
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(r['at']))
        parts.append(f'<tr><td>{_e(when)}</td><td>{_e(track)}</td><td>{_e(song)}</td><td>{_e(duration)}</td></tr>')
    parts.append('</table></div></details>')
    return ''.join(parts)
