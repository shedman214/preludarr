# SPDX-License-Identifier: GPL-3.0-or-later
"""Poster retry rejects stale paths clearly without weakening destination checks."""
import io
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
work = Path(tempfile.mkdtemp())
root = work/'library'; root.mkdir()
os.environ.update(CONFIG_DIR=str(work/'config'), TV_ROOT=str(root), MOVIE_ROOT=str(work/'absent'),
                  POSTER_BACKGROUND_ENABLED='false', AUTO_DOWNLOAD='false')
import anidb
import app_logs
with patch.object(anidb,'enabled',return_value=False), patch.object(app_logs,'install',return_value=False):
    import app
app.db().close()
results=[]
def check(name, ok):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ')+name)
class H:
    def __init__(self, fields):
        body=urllib.parse.urlencode(fields,doseq=True).encode()
        self.headers={'Content-Length':str(len(body))}
        self.rfile=io.BytesIO(body)
        self.sent=None
    def send(self, code, body, ctype='text/html'):
        self.sent=(code,body,ctype)

missing=root/'Assassination Classroom'
with patch.object(app,'queue_poster') as queue:
    h=H({'path':str(missing)})
    app.poster_v1_retry_single(h)
    check('Stale path returns actionable missing-folder response', h.sent[0]==400 and h.sent[1]==app.POSTER_FOLDER_UNAVAILABLE)
    check('Missing folder neither queues nor consumes retry quota', not queue.called and not app.POSTER_V1_MANUAL_ATTEMPTS)
    outside=work/'outside'; outside.mkdir()
    h=H({'path':str(outside)}); app.poster_v1_retry_single(h)
    check('Existing directory outside registered root still rejected', h.sent[:2]==(400,'Invalid anime destination') and not queue.called)
    for fields in ({'path':[str(missing),str(missing)]},{'path':str(missing),'extra':'value'}):
        h=H(fields); app.poster_v1_retry_single(h)
        check('Malformed form rejected before queue', h.sent[0]==400 and not queue.called)
    linked=root/'Linked Title'
    try:
        linked.symlink_to(outside,target_is_directory=True)
    except OSError:
        print('SKIP Symlink fixture requires local symlink privileges')
    else:
        h=H({'path':str(linked)}); app.poster_v1_retry_single(h)
        check('Symlink destination remains rejected with clear message',h.sent[:2]==(400,app.POSTER_LINKED_FOLDER) and not queue.called)

current=root/'Assassination Classroom (2015)'; current.mkdir()
app.save(current,'Anime TV',current.name,'missing','')
def redirect(h,url):h.sent=(303,url,'redirect')
with patch.object(app,'queue_poster',return_value='') as queue, patch.object(app.auth,'redirect',side_effect=redirect):
    h=H({'path':str(current)}); app.poster_v1_retry_single(h)
    check('Current dated folder retry succeeds', h.sent[0]==303 and queue.call_args.args==(str(current),'Anime TV',current.name))
with app.db() as conn:
    row=conn.execute('SELECT status,checked FROM posters WHERE path=?',(str(current),)).fetchone()
check('Valid retry enters existing priority queue', row['status']=='queued' and row['checked']==-1)
check('No media folders created for stale entry', not missing.exists() and not any(p.is_file() for p in root.rglob('*')))
check('Frontend includes safe missing-folder explanation', app.POSTER_FOLDER_UNAVAILABLE in app.POSTER_RETRY_SCRIPT and '__POSTER_RETRY_ERRORS__' not in app.POSTER_RETRY_SCRIPT)
print('%s/%s passed' % (sum(results),len(results)))
sys.exit(0 if all(results) else 1)
