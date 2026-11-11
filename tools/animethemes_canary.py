# SPDX-License-Identifier: GPL-3.0-or-later
"""Daily AnimeThemes canary (1.16 follow-up): does AnimeThemes still answer the way Preludarr needs?

Runs Preludarr's own AnimeThemes code (animethemes + animethemes_graphql) against the live GraphQL API:
a search, an anime with its themes and files, the AniDB look-up and cover images, and checks every field
Preludarr reads, then that one audio file actually downloads. It never touches a library or a server.

Exit code 0 = all good, 1 = something Preludarr relies on changed or failed. A Markdown report goes to
stdout and, when given, to the file named by --report (the workflow puts it into an issue).
Usage: python3 tools/animethemes_canary.py [--report FILE]
"""
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
import animethemes  # noqa: E402
import animethemes_graphql as g  # noqa: E402

UA = {'User-Agent': 'Preludarr canary (https://github.com/shedman214/preludarr)'}
API = 'https://api.animethemes.moe'
SLUG, ANIDB_ID = 'bocchi_the_rock', 16063  # a stable, well-documented entry
MEDIA_HOSTS = ('a.animethemes.moe', 'v.animethemes.moe')
checks = []


def check(name, ok, detail=''):
    checks.append((name, bool(ok), detail))


def ask(url):
    """Ask GraphQL only (no JSON:API fallback): the canary must notice when GraphQL itself breaks."""
    planned = g.plan(url)
    if planned is None:
        raise ValueError('Preludarr has no GraphQL plan for ' + url)
    return animethemes._graphql(planned, UA)


def step(name, url, verify):
    try:
        verify(ask(url))
    except Exception as exc:  # report every failure; never stop at the first
        check(name, False, type(exc).__name__ + ': ' + str(exc)[:300])


def search(data):
    found = [a for a in data.get('anime') or [] if isinstance(a, dict)]
    hit = next((a for a in found if a.get('slug') == SLUG), None)
    check('Search finds the anime', hit is not None, f'{len(found)} results')
    if hit:
        check('Search result has name, year and format', bool(hit.get('name')) and bool(hit.get('year')) and bool(hit.get('media_format')),
              str({k: hit.get(k) for k in ('name', 'year', 'media_format')}))
        check('Search result has synonyms', isinstance(hit.get('animesynonyms'), list))


audio_links = []


def detail(data):
    a = data.get('anime') or {}
    check('Anime detail answers', a.get('slug') == SLUG)
    themes = [t for t in a.get('animethemes') or [] if isinstance(t, dict)]
    check('Anime has themes', len(themes) > 0, f'{len(themes)} themes')
    for t in themes[:1]:
        check('Theme has type and slug', str(t.get('type') or '').upper().startswith(('OP', 'ED')) and bool(t.get('slug')),
              str({k: t.get(k) for k in ('type', 'slug')}))
        check('Theme has a song title', bool((t.get('song') or {}).get('title')))
        videos = [v for e in t.get('animethemeentries') or [] for v in e.get('videos') or []]
        check('Theme has videos', len(videos) > 0)
        for v in videos[:1]:
            link, audio = v.get('link') or '', (v.get('audio') or {}).get('link') or ''
            check('Video and audio links point at AnimeThemes media',
                  link.startswith('https://') and audio.startswith('https://')
                  and all(urllib.parse.urlsplit(u).hostname in MEDIA_HOSTS for u in (link, audio)), audio)
            audio_links.append(audio)


def anidb(data):
    slugs = [a.get('slug') for a in data.get('anime') or [] if isinstance(a, dict)]
    check('AniDB look-up finds the anime', SLUG in slugs, str(slugs))


def images(data):
    found = [i for i in (data.get('anime') or {}).get('images') or [] if isinstance(i, dict)]
    large = [i.get('link') or '' for i in found if i.get('facet') == 'Large Cover']
    check('Cover images (Large Cover) are listed with a link', any(link.startswith('https://') for link in large),
          str([i.get('facet') for i in found]))


def main():
    report = sys.argv[sys.argv.index('--report') + 1] if '--report' in sys.argv else None
    started = time.time()
    step('Search', API + '/anime?q=Bocchi%20the%20Rock&page%5Bsize%5D=20&include=animesynonyms', search)
    step('Anime detail', f'{API}/anime/{SLUG}?include=animethemes.animethemeentries.videos.audio%2Canimethemes.song', detail)
    step('AniDB look-up', f'{API}/anime?filter%5Bhas%5D=resources&filter%5Bsite%5D=AniDB&filter%5Bexternal_id%5D={ANIDB_ID}', anidb)
    step('Cover images', f'{API}/anime/{SLUG}?include=images', images)
    for link in audio_links[:1]:
        try:
            req = urllib.request.Request(link, headers={**UA, 'Range': 'bytes=0-1023'})
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read(1024)
                kind = response.headers.get('Content-Type', '')
            # a real Ogg audio file, not an error page served with 200
            check('An audio file downloads', response.status in (200, 206) and body.startswith(b'OggS'),
                  f'HTTP {response.status}, {kind or "no type"}')
        except Exception as exc:
            check('An audio file downloads', False, type(exc).__name__ + ': ' + str(exc)[:200])
    # the old JSON:API is informational only (deprecated; Preludarr no longer needs it)
    try:
        with urllib.request.urlopen(urllib.request.Request(API + '/anime?page%5Bsize%5D=1', headers=UA), timeout=30) as r:
            legacy = f'answers (HTTP {r.status})'
    except urllib.error.HTTPError as exc:
        legacy = f'HTTP {exc.code}'
    except Exception as exc:
        legacy = type(exc).__name__
    failed = [c for c in checks if not c[1]]
    lines = ['## AnimeThemes canary: ' + ('✅ all good' if not failed else f'❌ {len(failed)} check(s) failed'), '',
             f'GraphQL endpoint `{g.ENDPOINT}` · {len(checks)} checks · {time.time() - started:.0f} s · '
             f'old JSON:API (deprecated, informational): {legacy}', '',
             '| Check | Result | Detail |', '|---|---|---|']
    lines += [f"| {name} | {'✅' if ok else '❌'} | {detail.replace('|', '/')} |" for name, ok, detail in checks]
    if failed:
        lines += ['', 'Something Preludarr relies on changed or failed at AnimeThemes. Compare with '
                  'https://api-docs.animethemes.moe/ and `preludarr/animethemes_graphql.py`.']
    text = '\n'.join(lines)
    print(text)
    if report:
        Path(report).write_text(text + '\n', encoding='utf-8')
    return 1 if failed or not checks else 0


if __name__ == '__main__':
    sys.exit(main())
