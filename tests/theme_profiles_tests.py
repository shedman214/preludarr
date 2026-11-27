# SPDX-License-Identifier: GPL-3.0-or-later
"""Requested theme preferences, inheritance, queue re-review, and no-overwrite behavior."""
import contextlib
import io
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path
from unittest.mock import patch

work = Path(tempfile.mkdtemp())
anime, movies, shows = work / 'anime', work / 'movies', work / 'shows'
folders = [anime / 'A (2022)', anime / 'B (2022)', anime / 'C (2022)', movies / 'D (2022)', shows / 'E (2022)']
for folder in folders:
    folder.mkdir(parents=True)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(anime), MOVIE_ROOT=str(movies),
                  AUTO_DOWNLOAD='false', POSTER_BACKGROUND_ENABLED='false')
sys.path.insert(0, '/src/preludarr')
import app
import theme_profiles as profiles
import theme_queue as queue

results = []


def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)


app.ROOTS.append(('Shows', shows))
app.discover()
app.auto_themes.init(app)
app.auto_themes._save(dict(app.auto_themes.settings(), mode='all', per_scan=1))
a, b, c, d, e = (str(p) for p in folders)
set_profile = lambda scope, path, value: profiles.set_profile(app, scope, path, value)
active = lambda path: profiles.for_title(app, path)
check('Existing installs have the original first-opening global default', active(a) == ('first_op', 'Global default'))
set_profile('global', '', 'first_ed')
set_profile('root', str(anime), 'latest_op')
set_profile('title', a, 'random_op')
check('Title overrides root; root overrides global', active(a) == ('random_op', 'Title')
      and active(b) == ('latest_op', 'Root folder') and active(d) == ('first_ed', 'Global'))
set_profile('title', a, 'inherit')
check('Clearing a title profile restores inheritance', active(a) == ('latest_op', 'Root folder'))
try:
    set_profile('title', e, 'first_ed')
except ValueError:
    check('Regular Shows are not inadvertently changed by anime profiles', True)
else:
    check('Regular Shows are not inadvertently changed by anime profiles', False)
try:
    set_profile('root', str(work / 'other'), 'skip')
except ValueError:
    check('Unknown root cannot be saved', True)
else:
    check('Unknown root cannot be saved', False)

tracks = [
    {'type': 'OP', 'slug': 'OP1', 'sequence': 1, 'title': 'First', 'video_id': 1, 'audio_mimetype': 'audio/ogg'},
    {'type': 'OP', 'slug': 'OP2', 'sequence': 2, 'title': 'Second', 'video_id': 2, 'audio_mimetype': 'audio/flac'},
    {'type': 'OP', 'slug': 'OP10', 'sequence': 10, 'title': 'Tenth', 'video_id': 10, 'audio_mimetype': 'audio/ogg'},
    {'type': 'ED', 'slug': 'ED2', 'sequence': 2, 'title': 'Ending 2', 'video_id': 12},
    {'type': 'ED', 'slug': 'ED1', 'sequence': 1, 'title': 'Ending 1', 'video_id': 11},
]
check('Default retains original order; latest uses numeric sequence; first ending uses numeric order',
      profiles.select(tracks, 'first_op')['video_id'] == 1 and profiles.select(tracks, 'latest_op')['video_id'] == 10
      and profiles.select(tracks, 'first_ed')['video_id'] == 11)
check('Lossless preference uses actual declared audio MIME type, not video resolution',
      profiles.select(tracks, 'lossless_op')['video_id'] == 2
      and profiles.select([tracks[0], tracks[2]], 'lossless_op')['video_id'] == 1)
check('Random opening is stable across provider order changes',
      profiles.select(tracks, 'random_op', a)['slug'] == profiles.select(list(reversed(tracks)), 'random_op', a)['slug'])
check('Moving a root folder keeps the stable random opening',
      profiles.select(tracks, 'random_op', a)['slug'] == profiles.select(tracks, 'random_op', str(work / 'new-root' / folders[0].name))['slug'])
check('Explicit ending preference does not silently install an opening',
      profiles.select(tracks[:3], 'first_ed') is None)
check('Review only permits an explicit queue preview but no automatic choice; skip permits neither',
      profiles.select(tracks, 'review_only') is None and profiles.select(tracks, 'review_only', review=True)['video_id'] == 1
      and profiles.select(tracks, 'skip', review=True) is None)

set_profile('title', a, 'skip')
set_profile('title', b, 'review_only')
check('Skipped/review-only titles do not consume the limited automatic slot',
      app.auto_themes.to_install(app) == [c])
with patch.object(app.smart_matching, 'resolve', side_effect=AssertionError('Excluded profiles must not query providers')):
    check('Direct Download / Retry respects review-only and excluded profiles',
          'Skipped:' in app.process(a) and 'Skipped:' in app.process(b))
queue.wake = lambda app: None
added, skipped = queue.add(app, [a, b, c])
check('Queue skips excluded titles but allows a review-only proposal', (added, skipped) == (2, 1))

show = {'slug': 'fixture', 'name': 'Fixture', 'year': 2022}
for item in tracks:
    item['audio_url'] = f'https://a.animethemes.moe/audio{item["video_id"]}.ogg'
    item['artist'] = ''
with patch.object(app.smart_matching, 'resolve', return_value=(show, 'id', 'AniDB 1')), \
     patch.object(app, 'list_available_themes', return_value=tracks):
    queue.step(app)
    queue.step(app)
with app.db() as conn:
    by_path = {row['path']: row for row in conn.execute('SELECT * FROM theme_queue')}
    check('Queue profile choices are included in reviewed proposals',
          json.loads(by_path[b]['proposal'])['profile'] == 'review_only'
          and json.loads(by_path[c]['proposal'])['video_id'] == 10)
    conn.execute("UPDATE theme_queue SET state='approved' WHERE path=?", (c,))
set_profile('title', c, 'first_ed')
with patch.object(app, 'resolve_animethemes_track', side_effect=AssertionError('Outdated approval must not start a download')) as verify:
    queue.step(app)
with app.db() as conn:
    changed = conn.execute('SELECT state,proposal,note FROM theme_queue WHERE path=?', (c,)).fetchone()
check('Changed profile invalidates prior queue approval before resolving audio',
      not verify.called and changed['state'] == 'queued' and not changed['proposal'] and 'profile changed' in changed['note'].lower())
with patch.object(app.smart_matching, 'resolve', return_value=(show, 'id', 'AniDB 1')), \
     patch.object(app, 'list_available_themes', return_value=tracks):
    queue.step(app)
with app.db() as conn:
    new = conn.execute('SELECT proposal,state FROM theme_queue WHERE path=?', (c,)).fetchone()
    check('A changed preference produces a fresh, unapproved ending proposal',
          new['state'] == 'proposed' and json.loads(new['proposal'])['video_id'] == 11)
    conn.execute("UPDATE theme_queue SET state='approved' WHERE path=?", (c,))
set_profile('title', c, 'skip')
with patch.object(queue, 'install', side_effect=AssertionError('Excluded title must not install')) as install:
    queue.step(app)
    check('A newly excluded approved title is skipped without installing', not install.called)

set_profile('title', d, 'first_ed')
@contextlib.contextmanager
def change_during_stage(*args):
    set_profile('title', d, 'skip')
    yield work / 'fixture.mp3'
with patch.object(app.smart_matching, 'resolve', return_value=(show, 'id', 'AniDB 1')), \
     patch.object(app, 'list_available_themes', return_value=tracks), \
     patch.object(app.library_health, 'preflight'), \
     patch.object(app, 'resolve_animethemes_track', return_value={}), \
     patch.object(app, 'stage_verified_theme_audio', side_effect=change_during_stage), \
     patch.object(app, 'manual_install_mp3') as install:
    result = app.process(d)
    check('An automatic profile changed during staging cannot publish its old choice',
          'profile changed' in result.lower() and not install.called)
set_profile('title', d, 'first_ed')
with patch.object(app.smart_matching, 'resolve', return_value=(show, 'id', 'AniDB 1')), \
     patch.object(app, 'list_available_themes', return_value=tracks), \
     patch.object(app.library_health, 'preflight'), \
     patch.object(app, 'resolve_animethemes_track', return_value={}) as resolve, \
     patch.object(app, 'stage_verified_theme_audio', return_value=contextlib.nullcontext(work / 'fixture.mp3')), \
     patch.object(app, 'manual_install_mp3') as install:
    result = app.process(d)
    check('Automatic profile chooses an ending and cannot replace existing themes',
          result == 'Downloaded: Ending 1' and resolve.call_args.args == ('fixture', '11') and install.call_args.kwargs['replace'] is False)
(folders[3] / 'theme.mp3').write_bytes(b'ID3 fixture audio')
set_profile('title', d, 'first_op')
with patch.object(app, 'manual_install_mp3', side_effect=AssertionError('Do not replace existing themes')) as install:
    check('Changing a profile keeps the existing theme intact',
          app.process(d) == 'Already installed' and not install.called and (folders[3] / 'theme.mp3').read_bytes() == b'ID3 fixture audio')

payload = urllib.parse.urlencode({'scope': 'title', 'path': b, 'profile': 'latest_op'}).encode()
handler = type('Form', (), {'headers': {'Content-Length': str(len(payload))}, 'rfile': io.BytesIO(payload)})()
url = profiles.post(app, handler, lambda h, value: value)
check('Profile POST stores the preference and returns to the title page', active(b) == ('latest_op', 'Title')
      and url.startswith('/themes?') and '#title-theme-profile' in url)
check('Global/root and per-title controls expose the active rule', 'Anime Theme Profiles' in profiles.settings_html(app)
      and 'Global default' in profiles.settings_html(app) and 'Theme Profile' in profiles.title_html(app, b)
      and 'Latest opening' in profiles.title_html(app, b))

if os.name == 'posix':
    # Prefix remap changes profile records along with the saved root/title data.
    with app.db() as conn:
        conn.execute("INSERT INTO theme_profiles VALUES('/old','review_only'),('/old/Title','first_ed')")
    import path_moves
    path_moves.remap(app, '/old', '/new')
    check('Root moves keep inherited and title-specific profile records',
          active('/new/Title') == ('first_ed', 'Title') and active('/new/Another') == ('review_only', 'Root folder'))
print(f'{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
