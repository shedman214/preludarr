# SPDX-License-Identifier: GPL-3.0-or-later
"""Automatic backups, Sonarr style (Themearr V1.9.13).

System → Backup & Restore: on/off, interval (days) and retention (days). Every automatic backup is a normal in-app
backup made by system.create_backup (same verified archive, same folder, same restore path). Only backups
this module created are ever deleted by retention; manual backups are never removed automatically.

The folder is fixed: the container only sees /config, and the host restore watcher reads /config/system_backups.
"""
import html
import json
import os
import threading
import time
from pathlib import Path

FILE = 'themearr_backup_schedule.json'
DEFAULTS = {'enabled': True, 'interval_days': 7, 'retention_days': 28}
FIRST_DELAY = 600       # seconds after start-up before the first check (let the app settle)
CHECK_EVERY = 900       # seconds between checks
_LOCK = threading.RLock()
_STARTED = {'on': False, 'at': 0.0}


def _e(value):
    return html.escape(str(value), quote=True)


def _path(config):
    return Path(config) / FILE


def load(config):
    """Settings + state: enabled, interval_days, retention_days, auto (names this module created), last_auto, last_error."""
    data = dict(DEFAULTS, auto=[], last_auto=0, last_error='')
    try:
        raw = json.loads(_path(config).read_text())
        if isinstance(raw, dict):
            data.update({k: raw[k] for k in data if k in raw})
    except (OSError, ValueError):
        pass
    data['auto'] = [n for n in data.get('auto') or [] if isinstance(n, str)][-200:]
    return data


def _save(config, data):
    p = _path(config)
    tmp = p.with_name('.' + p.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out, indent=1, sort_keys=True)
    os.replace(tmp, p)


def validate(enabled, interval, retention):
    try:
        interval, retention = int(interval), int(retention)
    except (TypeError, ValueError):
        raise ValueError('Interval and retention must be whole numbers of days.') from None
    if not 1 <= interval <= 30:
        raise ValueError('Interval must be 1–30 days.')
    if not 1 <= retention <= 365:
        raise ValueError('Retention must be 1–365 days.')
    if retention < interval:
        raise ValueError('Retention must be at least the interval, or there would never be an automatic backup to keep.')
    return bool(enabled), interval, retention


def save_settings(config, enabled, interval, retention):
    enabled, interval, retention = validate(enabled, interval, retention)
    with _LOCK:
        data = load(config)
        data.update(enabled=enabled, interval_days=interval, retention_days=retention)
        _save(config, data)
    return data


def cleanup(config, sysv, now=None):
    """Delete automatic backups older than the retention period. Manual backups are never touched. Returns names removed."""
    now = now or time.time()
    removed = []
    with _LOCK:
        data = load(config)
        keep_secs = data['retention_days'] * 86400
        try:
            root = sysv._backup_dir(config)
        except ValueError:
            return removed
        present = {name: mtime for name, _, mtime in sysv._files(config)}
        still = []
        for name in data['auto']:
            if name not in present:
                continue  # already gone (downloaded and deleted by hand, or a restore replaced /config)
            if now - present[name] > keep_secs and sysv.FILENAME.fullmatch(name):
                p = root / name
                if p.is_file() and not p.is_symlink():
                    p.unlink()
                    removed.append(name)
                    continue
            still.append(name)
        data['auto'] = still
        _save(config, data)
    return removed


def due(data, now=None):
    now = now or time.time()
    return data['enabled'] and now - float(data.get('last_auto') or 0) >= data['interval_days'] * 86400


def run_once(app, now=None, force=False):
    """One scheduled check: clean up, then back up if due. Returns the new backup name, or ''."""
    import system as sysv
    config = app.CONFIG
    data = load(config)
    if not (force or due(data, now)):
        return ''
    removed = cleanup(config, sysv, now)
    try:
        name = sysv.create_backup(config)
    except Exception as error:  # never let a backup problem stop the app
        with _LOCK:
            data = load(config)
            data['last_error'] = (type(error).__name__ + ': ' + str(error))[:300]
            data['last_auto'] = (now or time.time()) - data['interval_days'] * 86400 + 6 * 3600  # retry in ~6 h, not every check
            _save(config, data)
        try:
            app.event('Automatic backup', 'error', 'Automatic backup failed: ' + str(error)[:200])
        except Exception:
            pass
        return ''
    with _LOCK:
        data = load(config)
        data['auto'] = (data['auto'] + [name])[-200:]
        data['last_auto'] = now or time.time()
        data['last_error'] = ''
        _save(config, data)
    try:
        app.event('Automatic backup', 'backup', 'Automatic backup created' + (f'; removed {len(removed)} older automatic backup' + ('' if len(removed) == 1 else 's') if removed else ''))
    except Exception:
        pass
    return name


def start(app):
    if _STARTED['on']:
        return
    _STARTED.update(on=True, at=time.time())

    def loop():
        time.sleep(FIRST_DELAY)
        while True:
            try:
                run_once(app)
            except Exception as error:
                print('Automatic backup error: ' + type(error).__name__, flush=True)
            time.sleep(CHECK_EVERY)
    threading.Thread(target=loop, name='themearr-auto-backup', daemon=True).start()


def next_run(data):
    if not data['enabled']:
        return None
    last = float(data.get('last_auto') or 0)
    return (last + data['interval_days'] * 86400) if last else (_STARTED['at'] or time.time()) + FIRST_DELAY


def panel_html(config, token, ui):
    """Settings card for System → Backup & Restore."""
    data = load(config)
    nxt = next_run(data)
    last = float(data.get('last_auto') or 0)
    status = []
    if data['last_error']:
        status.append('<p class="ab-err" role="alert"><strong>Last automatic backup failed:</strong> ' + _e(data['last_error']) + ' — Preludarr retries in about 6 hours.</p>')
    status.append('<dl class="ab-dl"><dt>Folder</dt><dd><code>/config/system_backups</code> <small class="muted">(inside the folder you mapped to <code>/config</code>; '
                  'fixed so Restore can always find them)</small></dd>'
                  '<dt>Last automatic backup</dt><dd>' + (_e(ui.when(last)) if last and not data['last_error'] else 'None yet') + '</dd>'
                  '<dt>Next</dt><dd>' + (_e(ui.when(nxt)) if nxt else 'Off') + '</dd>'
                  '<dt>Automatic backups kept</dt><dd>' + str(len(data['auto'])) + '</dd></dl>')
    return ('<section class="panel" id="auto-backups"><h2>Automatic backups</h2>'
            '<p>Like Sonarr: Preludarr makes a backup every few days and deletes <strong>automatic</strong> backups older than the retention period. '
            'Manual backups are never deleted automatically.</p>'
            '<form method="post" action="/system/backup/schedule" class="ab-form">'
            '<input type="hidden" name="csrf" value="' + _e(token) + '">'
            '<label class="check ab-on"><input type="checkbox" name="enabled" value="1"' + (' checked' if data['enabled'] else '') + '> Make backups automatically</label>'
            '<label>Interval (days)<input type="number" name="interval_days" min="1" max="30" required value="' + str(data['interval_days']) + '"></label>'
            '<label>Retention (days)<input type="number" name="retention_days" min="1" max="365" required value="' + str(data['retention_days']) + '"></label>'
            '<button type="submit">Save automatic backups</button></form>'
            '<p class="help muted">Interval: how often a backup is made (Sonarr default 7). Retention: automatic backups older than this are deleted (Sonarr default 28). '
            'The limit of 20 backups includes automatic ones; if it is reached, the automatic backup is skipped and shown here.</p>'
            + ''.join(status) +
            '<style>#auto-backups .ab-form{display:flex;flex-wrap:wrap;gap:12px 18px;align-items:flex-end;margin:10px 0}'
            '#auto-backups .ab-form label:not(.ab-on){display:flex;flex-direction:column;gap:6px;font-weight:600}'
            '#auto-backups .ab-form input[type=number]{width:120px;padding:9px 10px;background:#0d1626;color:#eef4ff;border:1px solid #3e5174;border-radius:8px;font:inherit}'
            '#auto-backups .ab-on{display:flex;align-items:center;gap:8px;flex-basis:100%;font-weight:600}'
            '#auto-backups .ab-dl{display:grid;grid-template-columns:200px minmax(0,1fr);gap:6px 14px;margin:12px 0 0}#auto-backups .ab-dl dt{color:#9fb0cc}#auto-backups .ab-dl dd{margin:0}'
            '#auto-backups .ab-err{padding:10px;border-radius:8px;border:1px solid #e07a86;background:#4a2430}'
            '@media(max-width:700px){#auto-backups .ab-dl{grid-template-columns:1fr}}</style></section>')
