# SPDX-License-Identifier: GPL-3.0-or-later
"""1.12 review, priority 2: the API key is read-only on new installs; existing installs keep their choice. Run in a Preludarr image, --network none."""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

work = Path(tempfile.mkdtemp())
os.environ['CONFIG_DIR'] = str(work)
sys.path.insert(0, '/src/preludarr')
import general_settings as gs  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


f = work / gs.FILE


def reset(saved=None):
    if f.exists():
        f.unlink()
    if saved is not None:
        f.write_text(json.dumps(saved))


reset()
check('New install: API key is read-only by default', gs.settings()['api_scan'] is False)
check('New install: nothing is written by the migration', gs.migrate_api_scan(False) is None and not f.exists())
reset()
check('Existing install that never saved the choice keeps "may start scans"', gs.migrate_api_scan(True) is True and gs.settings()['api_scan'] is True)
check('…and the choice is now written down (shown in Settings)', json.loads(f.read_text())['api_scan'] is True)
check('The migration runs only once', gs.migrate_api_scan(True) is None)
reset({'api_scan': False, 'instance_name': 'Home'})
check('A saved "read-only" choice is kept', gs.migrate_api_scan(True) is None and gs.settings()['api_scan'] is False and gs.settings()['instance_name'] == 'Home')
reset({'api_scan': True})
check('A saved "may start scans" choice is kept', gs.migrate_api_scan(True) is None and gs.settings()['api_scan'] is True)
reset({'instance_name': 'Home', 'allowed_hosts': ['nas.lan:8788']})
gs.migrate_api_scan(True)
s = gs.settings()
check('Other saved settings survive the migration', s['instance_name'] == 'Home' and s['allowed_hosts'] == ['nas.lan:8788'] and s['api_scan'] is True)


class H:
    def __init__(self, path, method_key):
        self.path = path; self.headers = {'X-Api-Key': method_key, 'Host': '127.0.0.1:8788'}; self.client_address = ('127.0.0.1', 1); self.sent = None

    def send(self, code, body, ctype=''):
        self.sent = (code, json.loads(body))


reset()
key = gs.api_key(create=True)
h = H('/api/v1/command/scan', key)
check('Read-only key: POST /api/v1/command/scan is refused with 403 and a clear message', gs.api_auth(h, '/api/v1/command/scan', 'POST') is False
      and h.sent[0] == 403 and 'read-only' in h.sent[1]['error'])
h = H('/api/v1/status', key)
check('Read-only key: GET /api/v1/status still works', gs.api_auth(h, '/api/v1/status', 'GET') is True)
f.write_text(json.dumps({'api_scan': True}))
h = H('/api/v1/command/scan', key)
first = gs.api_auth(h, '/api/v1/command/scan', 'POST')
h2 = H('/api/v1/command/scan', key)
second = gs.api_auth(h2, '/api/v1/command/scan', 'POST')
check('Scan permission on: the first scan is allowed, a second within 5 minutes gets 429 (cooldown kept)', first is True and second is False and h2.sent[0] == 429)
h = H('/api/v1/status?apikey=' + key, '')
h.headers = {'Host': '127.0.0.1:8788'}
check('Keys in the address stay off by default (header only)', gs.api_auth(h, '/api/v1/status', 'GET') is False and h.sent[0] == 401)
src = Path('/src/preludarr/app.py').read_text()
check('The app records whether the install existed before this start, before creating the database', src.index('EXISTING_INSTALL=DB.exists()') < src.index("CREATE TABLE IF NOT EXISTS shows"))

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
