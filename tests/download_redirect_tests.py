# SPDX-License-Identifier: GPL-3.0-or-later
"""1.12 review finding (#14): poster and AnimeThemes audio downloads follow redirects only to approved HTTPS hosts.
No network: the opener is replaced by fakes. Run in a Preludarr image, --network none."""
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

work = Path(tempfile.mkdtemp())
for d in ('anime', 'movies'):
    (work / d).mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


AUDIO = app.ANIMETHEMES_AUDIO_HOSTS
IMAGES = __import__('poster_manual').IMAGE_HOSTS
h = app._ApprovedRedirect(AUDIO)
req = urllib.request.Request('https://a.animethemes.moe/X-OP1.ogg')


def follows(newurl):
    try:
        return h.redirect_request(req, None, 302, 'Found', {}, newurl) is not None
    except urllib.error.HTTPError:
        return False


check('A redirect to another approved AnimeThemes host is followed', follows('https://v.animethemes.moe/X-OP1.webm'))
check('Redirects to other hosts, http://, ports, user:pass@ or internal addresses are refused before anything is requested',
      not any(follows(u) for u in ('https://evil.example/x.ogg', 'http://a.animethemes.moe/x.ogg', 'https://a.animethemes.moe:8443/x', 'https://u:p@a.animethemes.moe/x',
                                   'http://192.168.1.1/admin', 'https://a.animethemes.moe.evil.example/x', 'file:///etc/passwd')))


class FakeResponse:
    def __init__(self, url):
        self.url, self.closed = url, False

    def geturl(self):
        return self.url

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, final):
        self.final, self.handlers = final, []

    def open(self, req, timeout=None):
        self.opened = req.full_url
        return FakeResponse(self.final)


real = urllib.request.build_opener
seen = {}


def fake_build(*handlers):
    seen['handlers'] = handlers
    return FakeOpener(seen['final'])


urllib.request.build_opener = fake_build
seen['final'] = 'https://a.animethemes.moe/X-OP1.ogg'
r = app.open_approved(req, AUDIO, 5)
check('An approved download opens normally, with the host-checking redirect handler', r.geturl() == seen['final'] and isinstance(seen['handlers'][0], app._ApprovedRedirect))
seen['final'] = 'https://elsewhere.example/X.ogg'
try:
    app.open_approved(req, AUDIO, 5); refused = False
except ValueError:
    refused = True
check('If the final address is somewhere else anyway, the download is refused', refused)
try:
    app.open_approved(urllib.request.Request('https://evil.example/poster.jpg'), IMAGES, 5); refused = False
except ValueError:
    refused = True
check('A poster address that is not TMDB/TheTVDB is refused before any request', refused)
urllib.request.build_opener = real

src = Path('/src/preludarr/app.py').read_text()
check('Both download paths use the check (theme audio and automatic posters)',
      "open_approved(req,ANIMETHEMES_AUDIO_HOSTS,35)" in src and "open_approved(req,__import__('poster_manual').IMAGE_HOSTS,25)" in src)
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
