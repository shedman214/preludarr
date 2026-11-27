# SPDX-License-Identifier: GPL-3.0-or-later
"""1.16.3: provider retry, folder availability, and the shared automatic installer."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

work = Path(tempfile.mkdtemp())
root = work / 'anime'
names = ('AniList Wait (2020)', 'AnimeThemes Wait (2020)', 'No Match (2020)',
         'Legacy (2020) [tvdbid-123]', 'Legacy Outage (2020)', 'Existing (2020)',
         'Vanished (2020)', 'Install Match (2020)', 'Empty File (2020)', 'Interrupted (2020)')
for name in names:
    (root / name).mkdir(parents=True)
(work / 'movies').mkdir()
existing = b''.join((b'ID3', b'a' * 4096))
(root / 'Existing (2020)' / 'theme.mp3').write_bytes(existing)
(root / 'Empty File (2020)' / 'theme.mp3').touch()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(root),
                  MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='false',
                  POSTER_BACKGROUND_ENABLED='false')
sys.path.insert(0, '/src/preludarr')
import app
import theme_retry

results = []


def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)


def path(title):
    return str(root / title)


def row(title):
    with app.db() as c:
        return c.execute('SELECT state,detail FROM shows WHERE path=?', (path(title),)).fetchone()


def pending(title):
    with app.db() as c:
        return c.execute('SELECT * FROM theme_retry WHERE path=?', (path(title),)).fetchone()


app.discover()
app.auto_themes.init(app)
app.auto_themes._save(dict(app.auto_themes.settings(), mode='all', per_scan=50))
calls = []
slug = 'install_match'
show = {'slug': slug, 'name': 'Install Match', 'year': 2020, 'animethemes': [
    {'type': 'OP', 'slug': 'OP1', 'song': {'title': 'Verified opening'}, 'animethemeentries': [
        {'id': 1, 'videos': [{'id': 123, 'audio': {'link': 'https://a.animethemes.moe/fixture.ogg'}}]}]}]}


def provider(url, data=None, fresh=False):
    calls.append((url, fresh))
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname == 'graphql.anilist.co':
        title = data['variables']['s']
        if title == 'AniList Wait':
            raise urllib.error.HTTPError(url, 429, 'Try later', {'Retry-After': '600'}, None)
        if title == 'No Match':
            return {'data': {'Media': None}}
        return {'data': {'Media': {'seasonYear': 2020, 'title': {'english': title}, 'synonyms': []}}}
    if parsed.path == '/anime':
        title = urllib.parse.parse_qs(parsed.query).get('q', [''])[0]
        if title == 'AnimeThemes Wait':
            raise urllib.error.HTTPError(url, 503, 'Unavailable', {}, None)
        return {'anime': [show]}
    if parsed.path == '/anime/' + slug:
        return {'anime': show}
    raise AssertionError('Unexpected test request')


with patch.object(app, 'request_json', side_effect=provider):
    app.process(path('AniList Wait (2020)'))
    retry = pending('AniList Wait (2020)')
    check('AniList rate limit is a scheduled retry rather than Needs review',
          row('AniList Wait (2020)')['state'] == 'missing' and retry['attempts'] == 1
          and 590 <= retry['next_retry'] - time.time() <= 610)
    check('Future retry does not consume an automatic install slot',
          path('AniList Wait (2020)') not in app.auto_themes.to_install(app))
    app.discover()
    check('Rescan preserves pending retry information',
          'Waiting for retry:' in row('AniList Wait (2020)')['detail'])
    with app.db() as c:
        c.execute('UPDATE theme_retry SET next_retry=? WHERE path=?', (int(time.time()) - 1, path('AniList Wait (2020)')))
    check('A due retry is selected by automatic processing',
          path('AniList Wait (2020)') in app.auto_themes.to_install(app))
    calls.clear()
    app.process(path('AnimeThemes Wait (2020)'))
    check('AnimeThemes outage does not get swallowed as no matching title',
          row('AnimeThemes Wait (2020)')['state'] == 'missing'
          and 'HTTP 503' in pending('AnimeThemes Wait (2020)')['reason'])
    check('Provider outage stops the remaining title variants',
          sum('api.animethemes.moe' in url for url, _ in calls) == 1)
    app.process(path('No Match (2020)'))
    check('Genuine no-match still needs user review (no automatic retry)',
          row('No Match (2020)')['state'] == 'review' and not pending('No Match (2020)')
          and path('No Match (2020)') not in app.auto_themes.to_install(app))
    calls.clear()
    app.process(path('Existing (2020)'))
    app.process(path('Empty File (2020)'))
    check('Existing themes and zero-byte placeholders are never overwritten or downloaded again',
          not calls and (root / 'Existing (2020)' / 'theme.mp3').read_bytes() == existing
          and (root / 'Empty File (2020)' / 'theme.mp3').stat().st_size == 0
          and row('Empty File (2020)')['state'] == 'review')

app.save(path('Legacy (2020) [tvdbid-123]'), 'Anime TV', 'Legacy (2020) [tvdbid-123]', 'review', 'HTTP Error 404: Not Found')
app.save(path('Legacy Outage (2020)'), 'Anime TV', 'Legacy Outage (2020)', 'review', 'HTTP Error 503: Service Unavailable')
app.save(path('Interrupted (2020)'), 'Anime TV', 'Interrupted (2020)', 'downloading', 'Prior restart')
app.STATUS.update(running=True, active_path=None)
app.discover()
app.STATUS['running'] = False
check('Upgrade requeues known old provider errors, including the fixed tagged-title 404',
      all(row(name)['state'] == 'missing' for name in ('Legacy (2020) [tvdbid-123]', 'Legacy Outage (2020)'))
      and row('No Match (2020)')['state'] == 'review')
check('Interrupted downloads do not remain stuck after a scan', row('Interrupted (2020)')['state'] == 'missing')

app.save(path('Vanished (2020)'), 'Anime TV', 'Vanished (2020)', 'review', 'Keep match review')
shutil.move(root / 'Vanished (2020)', work / 'temporarily-offline')
app.discover()
check('Disappeared title is retained and marked unavailable',
      row('Vanished (2020)')['state'] == 'unavailable'
      and app.process(path('Vanished (2020)')).startswith('Unavailable:')
      and path('Vanished (2020)') not in app.auto_themes.to_install(app))
shutil.move(work / 'temporarily-offline', root / 'Vanished (2020)')
app.discover()
check('When a folder returns, its original review state is restored',
      row('Vanished (2020)')['state'] == 'review' and row('Vanished (2020)')['detail'] == 'Keep match review')
shutil.move(root, work / 'root-offline')
app.discover()
check('An unavailable mount does not delete the library or schedule installs',
      row('Existing (2020)')['state'] == 'unavailable' and not app.auto_themes.to_install(app))
shutil.move(work / 'root-offline', root)
app.discover()
check('Recovered mount retains the original installed audio',
      row('Existing (2020)')['state'] == 'installed' and (root / 'Existing (2020)' / 'theme.mp3').read_bytes() == existing)

# Real FFmpeg and normal installer; only the remote audio/metadata are fixtures.
audio = work / 'fixture.wav'
subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', str(audio)], check=True, timeout=30)
refreshed = []
calls.clear()
with patch.object(app, 'request_json', side_effect=provider), \
     patch.object(app, 'download_verified_theme_audio', side_effect=lambda track, out: shutil.copyfile(audio, out)), \
     patch.object(app.media_servers, 'after_install', side_effect=lambda cfg, p: refreshed.append(p)):
    outcome = app.process(path('Install Match (2020)'))
target = root / 'Install Match (2020)' / 'theme.mp3'
check('Automatic install verifies the selected video against fresh provider metadata',
      any('/anime/' + slug in url and fresh for url, fresh in calls))
check('Automatic install uses the same conversion, permissions and refresh as manual installs',
      outcome == 'Downloaded: Verified opening' and target.stat().st_size > 1024
      and stat.S_IMODE(target.stat().st_mode) == 0o664 and refreshed == [path('Install Match (2020)')]
      and row('Install Match (2020)')['state'] == 'installed')
check('Only one active theme is present and no staged files are left', sorted(p.name for p in target.parent.iterdir()) == ['theme.mp3'])

print(f'{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
