# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in Sonarr/Radarr import webhooks. Nothing in a webhook can name a local file.

Only the numeric item ID is queued. Before doing work we re-read that item
from the configured instance, use its registered path mapping, validate the
real title folder, and use the ordinary indexing and automatic theme rules.
"""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path

import arr
import kinds
import tasks
import theme_profiles

ROUTE = '/api/v1/webhook/arr/'
MAX_BODY = 128 * 1024
MAX_EVENTS_PER_MINUTE = 120
_DECISION_LOCK = threading.Lock()
_HITS = {}


def _digest(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def revision(instance):
    # Reject work queued for a previous instance URL/credential or mapping.
    return _digest(json.dumps([instance.get(k) for k in ('kind', 'url', 'api_key', 'mappings')],
                              sort_keys=True, separators=(',', ':')))


def rotate(app, iid):
    inst = arr.instance(app.CONFIG, iid)
    if not inst:
        raise ValueError('Unknown instance.')
    token = secrets.token_urlsafe(32)
    with app.db() as conn:
        conn.execute('INSERT INTO arr_webhooks(instance,token_hash) VALUES(?,?) ON CONFLICT(instance) DO UPDATE SET token_hash=excluded.token_hash',
                     (iid, _digest(token)))
    return token


def disable(app, iid):
    with app.db() as conn:
        conn.execute('DELETE FROM arr_webhooks WHERE instance=?', (iid,))


def enabled(app, iid):
    with app.db() as conn:
        return bool(conn.execute('SELECT 1 FROM arr_webhooks WHERE instance=?', (iid,)).fetchone())


def _authorized(app, iid, token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        return False
    with app.db() as conn:
        row = conn.execute('SELECT token_hash FROM arr_webhooks WHERE instance=?', (iid,)).fetchone()
    return bool(row and hmac.compare_digest(row[0], _digest(token)))


def _rate_ok(iid):
    now = time.monotonic()
    with _DECISION_LOCK:
        # Inactive instance IDs cannot fill this table: only authenticated
        # events get here. Expire inactive entries as connections are replaced.
        for key in list(_HITS):
            if not _HITS[key] or now - _HITS[key][-1] >= 60:
                del _HITS[key]
        hits = _HITS.setdefault(iid, deque())
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= MAX_EVENTS_PER_MINUTE:
            return False
        hits.append(now)
    return True


def _item_id(payload, kind):
    if not isinstance(payload, dict):
        raise ValueError('Expected a JSON object.')
    event = payload.get('eventType')
    if not isinstance(event, str):
        raise ValueError('Missing event type.')
    if event == 'Test':
        return None, 'test'
    # Sonarr/Radarr call successful imports "Download", including upgrades.
    # Do not use Grab, Delete, Rename, or any unrecognized event to start work.
    if event != 'Download':
        return None, 'ignored'
    subject = payload.get('series' if kind == 'sonarr' else 'movie')
    item_id = subject.get('id') if isinstance(subject, dict) else None
    if type(item_id) is not int or not 0 < item_id <= 2147483647:
        raise ValueError('Missing or invalid imported item ID.')
    return item_id, 'import'


def receive(app, handler):
    def send(status, **body):
        return handler.send(status, json.dumps(body), 'application/json')
    parsed = urllib.parse.urlsplit(handler.path)
    iid = parsed.path.removeprefix(ROUTE)
    if (not re.fullmatch(r'[0-9a-f]{12}', iid) or parsed.query or parsed.fragment
            or not __import__('login').host_ok(handler)):
        return send(400, error='Invalid webhook address.')
    # A cookie, the general read-only API key, and URL keys cannot authorize a webhook.
    given = (handler.headers.get('X-Api-Key') or '').strip()
    if not _authorized(app, iid, given):
        return send(401, error='Webhook key required.')
    inst = arr.instance(app.CONFIG, iid)
    if not inst or not inst.get('enabled', True):
        return send(403, error='This instance is disabled.')
    if not _rate_ok(iid):
        return send(429, error='Too many events; try again later.')
    try:
        if (handler.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower() != 'application/json':
            return send(415, error='Send an application/json body.')
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= MAX_BODY:
            return send(413, error='Webhook body too large or empty.')
        body = handler.rfile.read(size)
        if len(body) != size:
            return send(400, error='Incomplete webhook request.')
        item, action = _item_id(json.loads(body.decode('utf-8', 'strict')), inst['kind'])
        if action == 'test':
            return send(200, status='connected')
        if action == 'ignored':
            return send(200, status='ignored')
        rev = revision(inst)
        task, created = tasks.enqueue(app, 'arr', f'arr:{iid}:{item}:{rev[:16]}',
                                      {'instance': iid, 'item': item, 'revision': rev}, delay=10)
        return send(202, status='queued' if created else 'already queued', task=task)
    except (ValueError, UnicodeError, RecursionError):
        return send(400, error='Invalid import event or task queue full.')


def run(app, payload, check):
    iid, item_id = payload['instance'], payload['item']
    inst = arr.instance(app.CONFIG, iid)
    if not inst or not inst.get('enabled', True) or not enabled(app, iid) or revision(inst) != payload['revision']:
        return 'Instance settings changed or events disabled; ignored.'
    if type(item_id) is not int or not 0 < item_id <= 2147483647:
        raise ValueError('Invalid import task ID.')
    check()
    endpoint = arr.KINDS[inst['kind']][1] + '/' + str(item_id)
    raw = arr._get(inst, endpoint)
    if not isinstance(raw, dict) or raw.get('id') != item_id or not isinstance(raw.get('path'), str):
        raise ValueError('Unexpected media item.')
    # Read the live connection again before trusting the previously fetched item.
    current = arr.instance(app.CONFIG, iid)
    if not current or not current.get('enabled', True) or not enabled(app, iid) or revision(current) != payload['revision']:
        return 'Instance settings changed while looking up the item; ignored.'
    local = arr.to_local(current, raw['path'])
    label, root = arr._root_for(app, local) if local else (None, None)
    if not label:
        return 'No registered path mapping covers the imported item. Use Connect to check its mappings.'
    expected = ('Anime TV', 'Shows') if current['kind'] == 'sonarr' else ('Anime Movies', 'Movies')
    folder = Path(local)
    # A webhook never creates folders, follows a link, or acts on a library of
    # the wrong media type. The ordinary scans remain the folder-creation fallback.
    if (label not in expected or folder.name.startswith(('.', '!')) or folder.is_symlink()
            or not folder.is_dir() or os.path.realpath(root) != str(root)
            or os.path.realpath(folder) != str(folder)):
        return 'Imported title folder not available in a registered library; a scheduled scan can find it later.'
    check()
    with app.db() as conn:
        was_known = bool(conn.execute('SELECT 1 FROM shows WHERE path=?', (local,)).fetchone())
    if not app.index_title(label, root, folder):
        return 'Title folder not available; waiting for the next scan.'
    it = arr.normalized_item(current['kind'], raw)
    arr.record_media_ids(app, local, it, current['kind'])
    with app.db() as conn:
        if it['added']:
            conn.execute('INSERT INTO show_added(path,added,baseline) VALUES(?,?,0) ON CONFLICT(path) DO UPDATE SET added=excluded.added,baseline=0',
                         (local, it['added']))
        elif not was_known:
            conn.execute('INSERT OR IGNORE INTO show_added(path,added,baseline) VALUES(?,?,0)', (local, int(time.time())))
        else:
            conn.execute('INSERT OR IGNORE INTO show_added(path,added,baseline) VALUES(?,0,1)', (local,))
    if app.POSTER_BACKGROUND_ENABLED:
        __import__('poster_background').wake()
    if not kinds.is_anime(label):
        return 'Registered ' + folder.name + ' · Shows and Movies stay manual.'
    if not app.auto_themes.eligible(app, local, include_review=True):
        return 'Registered ' + folder.name + ' · no automatic theme requested.'
    profile, _ = theme_profiles.for_title(app, local)
    if profile == 'review_only':
        app.save(folder, label, folder.name, 'review', 'Theme Profile: Review only. Choose a track in the Theme Browser.')
        return 'Registered ' + folder.name + ' · ready for manual review.'
    check()
    if not tasks.take_auto_attempt(app):
        return 'Registered ' + folder.name + ' · automatic limit reached; scheduled scans will retry.'
    # process() retains preflight, no-overwrite, exact identity, profile and
    # provider rules and runs the same targeted server refresh on success.
    app.STATUS['active_path'] = local
    result = app.process(local)
    return folder.name + ' · ' + result


def _e(value):
    return html.escape(str(value), quote=True)


def settings_html(app):
    parts = ['<section class="panel" id="arr-import-events"><h2>Sonarr &amp; Radarr import events</h2>',
             '<p class="muted">Optional, off until enabled for each connected instance. In Sonarr / Radarr, open Settings → Connect → Webhook. '
             'Enter your Preludarr LAN/HTTPS address plus the path below, add the generated <code>X-Api-Key</code> custom header, '
             'and enable On Import and, if desired, On Upgrade. Test checks the connection without scheduling work. '
             'The existing Automatic Themes settings and Theme Profiles still decide whether to install anything.</p>']
    instances = arr.load(app.CONFIG)['instances']
    for inst in instances:
        iid = inst['id']
        on = enabled(app, iid)
        parts += ['<p><strong>' + _e(inst.get('name', inst['kind'])) + '</strong> · ' + ('Webhook enabled' if on else 'Off') + '</p>',
                  '<code>' + _e(ROUTE + iid) + '</code>',
                  '<form method="post" action="/system/arr-webhook" style="margin:10px 0">',
                  '<input type="hidden" name="id" value="' + _e(iid) + '">',
                  '<button type="submit" name="action" value="rotate">' + ('Rotate webhook key' if on else 'Enable and generate key') + '</button>',
                  (' <button type="submit" name="action" value="disable">Disable events</button>' if on else ''), '</form>']
    if not instances:
        parts.append('<p>Connect an instance in <a href="/media-servers?tab=arr">Sonarr &amp; Radarr</a> first.</p>')
    parts.append('<p class="muted">The webhook key is separate from the read-only application API key and the Arr server login key. '
                 'It is shown once; rotating it invalidates the previous one. Keys are never accepted in an address.</p></section>')
    return ''.join(parts)


def settings_post(app, handler, redirect):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 1024:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), max_num_fields=2)
        if set(form) != {'id', 'action'} or any(len(v) != 1 for v in form.values()):
            raise ValueError('Invalid form.')
        iid, action = form['id'][0], form['action'][0]
        if not arr.instance(app.CONFIG, iid):
            raise ValueError('Unknown instance.')
        if action == 'disable':
            disable(app, iid)
            return redirect(handler, '/system?tab=tasks#arr-import-events')
        if action != 'rotate':
            raise ValueError('Unknown action.')
        token = rotate(app, iid)
        # Deliver the secret exactly once through an authenticated form POST,
        # never in the URL, task payload, database or event log.
        return handler.send(200, '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
                            '<title>Preludarr webhook key</title></head><body>' + app.full_sidebar('system') +
                            '<main><section class="panel"><h2>Webhook enabled</h2><p>Copy this key now; it cannot be shown again.</p>'
                            '<p>Webhook address path: <code>' + _e(ROUTE + iid) + '</code></p>'
                            '<label>X-Api-Key <input readonly autocomplete="off" value="' + _e(token) +
                            '" style="width:30em;max-width:100%"></label><p><a href="/system?tab=tasks#arr-import-events">Return to Tasks</a></p>'
                            '</section></main></body></html>')
    except (ValueError, OSError, UnicodeError):
        return handler.send(400, 'Webhook setting could not be saved.', 'text/plain')
