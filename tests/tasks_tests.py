# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline coverage for opt-in import handling, durable tasks and shared limits."""
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

work = Path(tempfile.mkdtemp())
anime, movies = work / 'anime', work / 'movies'
anime.mkdir()
movies.mkdir()
existing = anime / 'Existing'
existing.mkdir()
(existing / 'theme.mp3').write_bytes(b'ID3' + b'x' * 2000)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(anime), MOVIE_ROOT=str(movies),
                  AUTO_DOWNLOAD='false', POSTER_BACKGROUND_ENABLED='false')
sys.path.insert(0, '/src/preludarr')
import app
import arr
import arr_events as events
import tasks
import theme_profiles

results = []


def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)


def policy(**kwargs):
    old = tasks.settings(app)
    old.update(kwargs)
    with app.db() as c:
        tasks.put(c, 'policy', json.dumps(old))


def row(tid):
    with app.db() as c:
        return c.execute('SELECT * FROM background_tasks WHERE id=?', (tid,)).fetchone()


app.discover()
app.auto_themes.init(app)
check('Upgrade defaults preserve existing background behavior', tasks.settings(app) == tasks.DEFAULTS)
policy(start=22, end=6)
check('Overnight processing window works', not tasks.policy_reason(app, hour=23) and not tasks.policy_reason(app, hour=5)
      and tasks.policy_reason(app, hour=6) == 'Outside processing hours')
policy(start=0, end=0, busy_percent=80)
check('Host busy threshold pauses only when configured', tasks.policy_reason(app, hour=12, load_percent=90)
      and not tasks.policy_reason(app, hour=12, load_percent=20))
policy(busy_percent=0, profile='eco', paused=True)
check('Eco limits attempts and slows background work', tasks.automatic_limit(app, 20) == 1
      and tasks.poster_delay(app, 10) == 30 and tasks.queue_delay(app) > 2)
t, is_new = tasks.schedule_scan(app)
same, other_new = tasks.schedule_scan(app)
check('Scheduled scans coalesce durably', is_new and not other_new and t == same)
check('Global pause prevents queued task execution', not tasks.step(app) and row(t)['state'] == 'queued')
policy(paused=False)
with app.db() as c:
    c.execute("UPDATE background_tasks SET state='running' WHERE id=?", (t,))
tasks.recover(app)
check('Interrupted work is recovered after restart', row(t)['state'] == 'queued')
with patch.object(app, 'scan_library', return_value='Recorded scan') as scan:
    check('Recovered scan executes once after resume', tasks.step(app) and scan.call_count == 1 and row(t)['state'] == 'done')
t, _ = tasks.schedule_scan(app)
check('Queued cancellation does not execute a scan', tasks.cancel_task(app, t) and not tasks.step(app) and row(t)['state'] == 'cancelled')
t, _ = tasks.schedule_scan(app)
with app.db() as c:
    c.execute("UPDATE background_tasks SET state='running' WHERE id=?", (t,))
tasks.cancel_task(app, t)
tasks.recover(app)
check('Restart respects pending cancellation', row(t)['state'] == 'cancelled')
t, _ = tasks.schedule_scan(app)
with app.db() as c:
    c.execute("UPDATE background_tasks SET state='running' WHERE id=?", (t,))
again, again_new = tasks.schedule_scan(app)
tasks._finish(app, t, 'done', 'Finished with the older settings')
check('A request during a running task queues exactly one follow-up run', again == t and not again_new
      and row(t)['state'] == 'queued' and row(t)['note'] == 'Settings changed; running again' and not row(t)['rerun'])
tasks._finish(app, t, 'done', 'unused')  # not running: no change
with app.db() as c:
    c.execute("UPDATE background_tasks SET state='running' WHERE id=?", (t,))
tasks._finish(app, t, 'done', 'Complete')
check('Without a new request the task simply finishes', row(t)['state'] == 'done')


class Form:
    def __init__(self, text):
        data = text.encode()
        self.headers, self.rfile = {'Content-Length': str(len(data))}, io.BytesIO(data)


def submit(text):
    return tasks.post(app, Form(text), lambda handler, url: url)


submit('action=pause')
# A policy form opened before the pause must not undo it.
saved = submit('action=policy&profile=eco&priority_root=&start=1&end=5&busy_percent=0')
check('Saving the policy keeps a pause made meanwhile', 'saved' in saved and tasks.settings(app)['paused']
      and tasks.settings(app)['profile'] == 'eco' and tasks.settings(app)['end'] == 5)
submit('action=resume')
submit('action=policy&profile=eco&priority_root=&start=0&end=0&busy_percent=0')  # the later checks expect Eco
check('Resume and policy changes are saved', tasks.settings(app) == {**tasks.DEFAULTS, 'profile': 'eco'})
outside = work / 'outside'
(outside / 'Linked').mkdir(parents=True)
(outside / 'Linked' / 'theme.mp3').write_bytes(b'ID3' + b'x' * 2000)
(anime / 'Linked').symlink_to(outside / 'Linked', target_is_directory=True)
indexed = app.index_title('Anime TV', anime, anime / 'Linked')
with app.db() as c:
    linked = c.execute('SELECT 1 FROM shows WHERE path=?', (str(anime / 'Linked'),)).fetchone()
check('A linked title folder pointing outside the root is not indexed', not indexed and not linked
      and app.index_title('Anime TV', anime, existing))

# A generated, dedicated webhook key; no production credentials.
iid = 'aabbccddeeff'
inst = {'id': iid, 'kind': 'sonarr', 'name': 'Fixture Sonarr', 'url': 'http://192.168.1.50:8989',
        'api_key': 'fixture-' + 'key', 'enabled': True, 'mappings': [{'remote': '/srv/anime', 'local': str(anime)}]}
arr._save(app.CONFIG, {'instances': [inst], 'auto_create': False, 'auto_create_since': 0})
check('Import events are disabled by default', not events.enabled(app, iid))
key = events.rotate(app, iid)
with app.db() as c:
    stored = c.execute('SELECT token_hash FROM arr_webhooks WHERE instance=?', (iid,)).fetchone()[0]
check('Only a digest of the dedicated key is persisted', stored != key and len(stored) == 64 and events.enabled(app, iid))


class H:
    def __init__(self, doc, token='', query=''):
        body = json.dumps(doc).encode()
        self.headers = {'Content-Length': str(len(body)), 'Content-Type': 'application/json', 'X-Api-Key': token}
        self.rfile = io.BytesIO(body)
        self.path = events.ROUTE + iid + query

    def send(self, status, body, *args):
        return status, json.loads(body)


def receive(doc, token='', query=''):
    with patch('login.host_ok', return_value=True):
        return events.receive(app, H(doc, token, query))


check('Webhook requires a dedicated header even for Test', receive({'eventType': 'Test'})[0] == 401)
check('The key cannot be sent in a query string', receive({'eventType': 'Test'}, '', '?apikey=' + key)[0] == 400)
check('Connection Test is read-only and accepted', receive({'eventType': 'Test'}, key) == (200, {'status': 'connected'}))
check('Grab does not queue imports', receive({'eventType': 'Grab'}, key) == (200, {'status': 'ignored'}))
check('Malformed item ID rejected', receive({'eventType': 'Download', 'series': {'id': 'not-an-id'}}, key)[0] == 400)
event = {'eventType': 'Download', 'series': {'id': 41, 'path': '/srv/anime/Wrong title'}}
a, first = receive(event, key)
b, second = receive(event, key)
check('Repeat imports collapse to the same pending job', a == b == 202 and first['task'] == second['task']
      and first['status'] == 'queued' and second['status'] == 'already queued')
tid = first['task']
payload = json.loads(row(tid)['payload'])
check('Only the verified lookup ID and connection revision are stored', set(payload) == {'item', 'instance', 'revision'}
      and payload['item'] == 41 and 'Wrong title' not in row(tid)['payload'] and key not in row(tid)['payload'])
folder = anime / 'New title'
folder.mkdir()
raw = {'id': 41, 'title': 'New title', 'path': '/srv/anime/New title', 'tvdbId': 456,
       'added': '2026-11-27T12:00:00Z'}
with patch.object(arr, '_get', return_value=raw) as get, patch.object(app, 'process') as install:
    result = events.run(app, payload, lambda: None)
    check('Targeted lookup trusts live Arr path not webhook text', get.call_args.args[1] == 'series/41'
          and 'New title' in result and not (anime / 'Wrong title').exists())
    check('Automatic Themes Off still registers without installing', install.call_count == 0
          and app.media_ids_for(str(folder)).get('tvdb') == 456)
    with app.db() as c:
        check('Targeted indexing adds only the imported title', bool(c.execute('SELECT 1 FROM shows WHERE path=?', (str(folder),)).fetchone()))
tasks.cancel_task(app, tid)

app.auto_themes._save(dict(app.auto_themes.settings(), mode='all', per_scan=2, interval_hours=1))
theme_profiles.set_profile(app, 'title', str(folder), 'review_only')
with patch.object(arr, '_get', return_value=raw), patch.object(app, 'process') as install:
    check('Review-only import does not download', 'manual review' in events.run(app, payload, lambda: None)
          and not install.called)
theme_profiles.set_profile(app, 'title', str(folder), 'skip')
app.save(folder, 'Anime TV', folder.name, 'missing')
with patch.object(arr, '_get', return_value=raw), patch.object(app, 'process') as install:
    check('Excluded title is not auto-installed', 'no automatic theme' in events.run(app, payload, lambda: None) and not install.called)
theme_profiles.set_profile(app, 'title', str(folder), 'first_op')
with patch.object(arr, '_get', return_value=raw), patch.object(app, 'process', return_value='Recorded install') as install:
    check('Eligible import uses original automatic installer', 'Recorded install' in events.run(app, payload, lambda: None)
          and install.call_args.args == (str(folder),))
    check('Eco caps subsequent event attempts in the scan interval', 'limit reached' in events.run(app, payload, lambda: None)
          and install.call_count == 1)
check('Shared persisted budget blocks another automatic scan attempt', not tasks.take_auto_attempt(app))
with patch.object(arr, '_get', return_value={**raw, 'path': '/srv/anime/Existing'}), patch.object(app, 'process') as install:
    check('Existing theme never replaced by import', 'no automatic theme' in events.run(app, payload, lambda: None)
          and not install.called and (existing / 'theme.mp3').read_bytes() == b'ID3' + b'x' * 2000)
new_key = events.rotate(app, iid)
check('Rotation invalidates previous key', receive({'eventType': 'Test'}, key)[0] == 401
      and receive({'eventType': 'Test'}, new_key)[0] == 200)
events.disable(app, iid)
with patch.object(arr, '_get') as get:
    check('Disabled instance ignores pending tasks without requests', 'disabled' in events.run(app, payload, lambda: None) and not get.called)
policy(profile='balanced', priority_root=str(movies))
mf = movies / 'Movie'; mf.mkdir()
app.index_title('Anime Movies', movies, mf)
check('Root priority can prefer an anime movie over other missing titles', app.auto_themes.to_install(app)[0] == str(mf))
import path_moves
old_root, new_root = str(work / 'empty-old'), str(work / 'empty-new')
policy(priority_root=old_root, paused=True)
real_move = path_moves._move_task_priority
transactions = []
def observe_move(conn, *args):
    transactions.append(conn.in_transaction)
    return real_move(conn, *args)
with patch.object(path_moves, '_move_task_priority', side_effect=observe_move):
    path_moves.remap(app, old_root, new_root, apply=True)
check('Moving an empty root reads the resource policy under its write transaction',
      transactions == [True] and tasks.settings(app)['priority_root'] == new_root and tasks.settings(app)['paused'])
print(f'{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
