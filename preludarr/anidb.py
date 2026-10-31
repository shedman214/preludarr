# SPDX-License-Identifier: GPL-3.0-or-later
"""AniDB identification for Themearr (V1.7.0, Smart Matching 2.0 foundation — no Shoko).

Only used when Original Matching found nothing; it never changes Original Matching, never installs and
never saves a match by itself. It suggests AnimeThemes entries whose AniDB ID matches the folder:

  folder title -> AniDB's official title list (all languages, local copy) -> candidate AniDB IDs
  -> AnimeThemes entries carrying that AniDB ID (V1.6.4 client: paced + remembered)
  -> year and TV/Movie check against the folder -> confidence + plain-language explanation.

AniDB rule respected: the title list (https://anidb.net/api/anime-titles.xml.gz) is downloaded at most
once per 24 hours (attempts count too), in the background, into /config/anidb/. If AniDB is unreachable
the last copy keeps working; without any copy the feature simply stays quiet.
"""
import difflib
import gzip
import html
import json
import os
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

DUMP_URL = 'https://anidb.net/api/anime-titles.xml.gz'
USER_AGENT = 'Preludarr/1.12 (self-hosted theme manager)'
MIN_INTERVAL = 24 * 3600
MAX_DUMP = 20 * 1024 * 1024
MIN_SCORE = 0.80
MAX_VERIFY = 2
XML_LANG = '{http://www.w3.org/XML/1998/namespace}lang'
TITLE_TYPES = {'main': 'main title', 'official': 'official title', 'syn': 'alternative title', 'short': 'short title'}
LANGS = {'x-jat': 'romaji', 'ja': 'Japanese', 'en': 'English'}
_LOCK = threading.Lock()
_DATA = {'index': None, 'mtime': None, 'loading': False, 'downloading': False, 'error': ''}
_STOP = {'the', 'and', 'of', 'no', 'wa', 'ga', 'wo', 'to', 'season', 'movie', 'the movie', 'tv', 'part'}


def key(value):
    return ''.join(ch for ch in unicodedata.normalize('NFKC', str(value)).casefold() if ch.isalnum())


def words(value):
    return [w for w in re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', str(value)).casefold()) if len(w) >= 3 and w not in _STOP]


# ------------------------------------------------------------------------- title list ----
def _folder(config):
    return Path(config) / 'anidb'


def _state(config):
    try:
        return json.loads((_folder(config) / 'state.json').read_text())
    except (OSError, ValueError):
        return {}


def _save_state(config, **changes):
    folder = _folder(config)
    folder.mkdir(mode=0o755, exist_ok=True)
    state = {**_state(config), **changes}
    tmp = folder / 'state.json.part'
    tmp.write_text(json.dumps(state))
    os.replace(tmp, folder / 'state.json')


def _parse(path):
    titles, keys, tokens = {}, {}, {}
    with gzip.open(path) as stream:
        for _, element in ET.iterparse(stream):
            if element.tag != 'anime':
                continue
            aid = int(element.get('aid'))
            entries = []
            for t in element:
                kind = t.get('type')
                if t.tag != 'title' or kind not in TITLE_TYPES or not (t.text or '').strip():
                    continue
                text = t.text.strip()
                entries.append((kind, t.get(XML_LANG) or '', text))
                k = key(text)
                if k:
                    keys.setdefault(k, set()).add(aid)
                for w in set(words(text)):
                    tokens.setdefault(w, set()).add(aid)
            titles[aid] = entries
            element.clear()
    if len(titles) < 1000:
        raise ValueError('AniDB title list looks incomplete')
    return {'titles': titles, 'keys': keys, 'tokens': tokens}


def _load(config):
    file = _folder(config) / 'anime-titles.xml.gz'
    try:
        mtime = file.stat().st_mtime
    except FileNotFoundError:
        return
    if _DATA['mtime'] == mtime:
        return
    with _LOCK:
        if _DATA['loading']:
            return
        _DATA['loading'] = True
    try:
        index = _parse(file)
        with _LOCK:
            _DATA.update(index=index, mtime=mtime, error='')
    except Exception as error:
        _DATA['error'] = 'AniDB title list could not be read (' + type(error).__name__ + ')'
    finally:
        _DATA['loading'] = False


def _download(config):
    folder = _folder(config)
    _save_state(config, last_attempt=int(time.time()))  # counts even if it fails: never more than once a day
    try:
        req = urllib.request.Request(DUMP_URL, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as response:
            data = response.read(MAX_DUMP + 1)
        if len(data) > MAX_DUMP or not data.startswith(b'\x1f\x8b'):
            raise ValueError('unexpected AniDB title list')
        tmp = folder / 'anime-titles.xml.gz.part'
        tmp.write_bytes(data)
        _parse(tmp)  # only replace the working copy with a list that parses
        os.replace(tmp, folder / 'anime-titles.xml.gz')
        _save_state(config, last_success=int(time.time()), size=len(data))
        _DATA['error'] = ''
    except Exception as error:
        _DATA['error'] = 'AniDB title list download failed (' + type(error).__name__ + '); using the last copy if any'
        (folder / 'anime-titles.xml.gz.part').unlink(missing_ok=True)
    finally:
        _DATA['downloading'] = False
    _load(config)


def ensure(config):
    """Load the local title list; start a background download only if allowed (>= 24 h since last attempt)."""
    file = _folder(config) / 'anime-titles.xml.gz'
    now = time.time()
    stale = (not file.exists()) or now - file.stat().st_mtime >= MIN_INTERVAL
    allowed = now - _state(config).get('last_attempt', 0) >= MIN_INTERVAL
    if stale and allowed:
        with _LOCK:
            start = not _DATA['downloading']
            _DATA['downloading'] = True
        if start:
            threading.Thread(target=_download, args=(config,), daemon=True).start()
    if file.exists() and _DATA['mtime'] != file.stat().st_mtime and not _DATA['loading']:
        threading.Thread(target=_load, args=(config,), daemon=True).start()
    return status(config)


def status(config):
    index = _DATA['index']
    state = _state(config)
    return {'available': index is not None, 'anime': len(index['titles']) if index else 0,
            'updated': time.strftime('%Y-%m-%d', time.localtime(_DATA['mtime'])) if _DATA['mtime'] else '',
            'loading': _DATA['loading'], 'downloading': _DATA['downloading'], 'error': _DATA['error'],
            'last_attempt': state.get('last_attempt', 0)}


# ------------------------------------------------------------------------ identification ----
def candidates(title, limit=MAX_VERIFY):
    """AniDB IDs whose titles match `title`: exact (normalized) first, then close title matches."""
    index = _DATA['index']
    if index is None:
        return []
    target = key(title)
    if len(target) < 2:
        return []
    found = {}
    for aid in index['keys'].get(target, ()):
        found[aid] = (1.0, True)
    pool = set()
    toks = sorted(set(words(title)), key=lambda w: len(index['tokens'].get(w, ())))
    for w in toks[:2]:
        pool |= index['tokens'].get(w, set())
    for aid in list(pool)[:600]:
        if aid in found:
            continue
        best = max((difflib.SequenceMatcher(None, target, key(t[2])).ratio() for t in index['titles'][aid]), default=0.0)
        if best >= MIN_SCORE:
            found[aid] = (best, False)
    ranked = sorted(found.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = []
    for aid, (score, exact) in ranked[:limit]:
        matched = max(index['titles'][aid], key=lambda t: difflib.SequenceMatcher(None, target, key(t[2])).ratio())
        main = next((t[2] for t in index['titles'][aid] if t[0] == 'main'), matched[2])
        out.append({'aid': aid, 'score': round(score, 3), 'exact': exact, 'main': main, 'matched': matched})
    return out


def identify(name, folder_year, movie, request_json, clean):
    """Suggestions for one folder. Each: AnimeThemes entry + AniDB ID + confidence + explanation."""
    results, notes = [], []
    found = candidates(clean(name), limit=4)
    exact = [c for c in found if c['exact']]
    for c in (exact or found)[:MAX_VERIFY]:  # exact title hits first; at most 2 AnimeThemes checks per folder
        url = 'https://api.animethemes.moe/anime?' + urllib.parse.urlencode(
            {'filter[has]': 'resources', 'filter[site]': 'AniDB', 'filter[external_id]': str(c['aid'])})
        try:
            entries = [e for e in (request_json(url).get('anime') or []) if isinstance(e, dict) and e.get('slug')]
        except Exception:
            notes.append(f"AnimeThemes could not be checked for AniDB {c['aid']} right now.")
            continue
        if not entries:
            notes.append(f"AniDB {c['aid']} ({c['main']}) has no AnimeThemes entry yet.")
            continue
        for e in entries[:2]:
            year = e.get('year')
            fmt = str(e.get('media_format') or '')
            year_ok = None if not folder_year or not year else int(year) == int(folder_year)
            if year_ok is False:
                notes.append(f"AniDB {c['aid']} → {e.get('name')} ({year}) skipped: your folder says {folder_year}.")
                continue
            type_ok = None if not fmt else (fmt.upper() == 'MOVIE') == movie
            kind, lang, text = c['matched']
            how = ('exact ' if c['exact'] else f"{round(c['score'] * 100)}% similar ") + \
                  f"{LANGS.get(lang, lang or 'other')} {TITLE_TYPES.get(kind, kind)} “{text}”"
            parts = [f"AniDB {c['aid']}: {how}"]
            parts.append(f'same year {year}' if year_ok else ('year not checked' if year_ok is None else ''))
            parts.append(('Movie' if fmt.upper() == 'MOVIE' else fmt or 'type unknown') + ('' if type_ok is not False else ' — but your folder is in the other library'))
            if c['exact'] and year_ok and type_ok is not False:
                confidence = 'High'
            elif (c['exact'] or c['score'] >= 0.92) and year_ok is not False and type_ok is not False:
                confidence = 'Medium'
            else:
                confidence = 'Low'
            results.append({'aid': c['aid'], 'slug': e['slug'], 'name': str(e.get('name') or e['slug']), 'year': year,
                            'format': fmt, 'confidence': confidence, 'why': ' · '.join(p for p in parts if p)})
    order = {'High': 0, 'Medium': 1, 'Low': 2}
    results.sort(key=lambda r: order[r['confidence']])
    return results, notes


# ---------------------------------------------------------------------------------- UI ----
def _e(value):
    return html.escape(str(value), quote=True)


def _logo(height):
    """The AniDB logo (user-supplied image, static/anidb-logo.png)."""
    from login import static_url
    return ('<img class="anidb-logo" src="' + static_url('anidb-logo.png') + '" alt="AniDB" width="' + str(height) + '" height="'
            + str(height) + '" style="vertical-align:middle;border-radius:6px">')


def panel(app, anime_path, name, library):
    """Placeholder that loads the AniDB section after the page appears (AnimeThemes can be slow)."""
    if not enabled(app.CONFIG):
        return ''
    url = '/anidb-identify?' + urllib.parse.urlencode({'path': anime_path})
    return ('<section class="panel" id="anidb-identification" data-anidb-url="' + _e(url) + '"><h3>' + _logo(40) + ' identification</h3>'
            '<p class="muted" role="status">Looking up this folder in AniDB…</p></section>'
            '<script>(function(){const box=document.getElementById("anidb-identification");if(!box)return;'
            'fetch(box.dataset.anidbUrl,{credentials:"same-origin",cache:"no-store"}).then(r=>{if(!r.ok)throw new Error("HTTP "+r.status);return r.text()})'
            '.then(h=>{box.innerHTML=h}).catch(e=>{const p=box.querySelector("p");if(p)p.textContent="AniDB lookup could not finish ("+e.message+"). Reload to try again."})})();</script>')


def panel_body(app, anime_path, name, library):
    """Inner HTML of the AniDB section (GET /anidb-identify). All values are escaped."""
    if not enabled(app.CONFIG):
        return '<p class="muted">AniDB identification is switched off in Settings → Connect.</p>'
    st = ensure(app.CONFIG)
    box = ['<h3>', _logo(40), ' identification</h3>']
    if not st['available']:
        msg = ('The AniDB title list is being prepared; reload this page in a minute.' if st['loading'] or st['downloading']
               else (st['error'] or 'The AniDB title list is not available yet.'))
        box += ['<p class="muted">', _e(msg), '</p>']
        return ''.join(box)
    results, notes = identify(name, app.year(name), library == 'Anime Movies', app.request_json, app.clean)
    if not results:
        box.append('<p>No AniDB-linked AnimeThemes entry matched this folder.</p>')
    else:
        box.append('<p>Matched through AniDB IDs. Check the anime and year, then click Remember this anime. '
                   'Nothing is saved or installed automatically.</p>')
        for r in results:
            art = '/anime-art?' + urllib.parse.urlencode({'slug': r['slug']})
            kind = 'Anime Movie' if r['format'].upper() == 'MOVIE' else ('Anime TV' if r['format'] else '')
            preview = json.dumps({'img': art, 'title': r['name'], 'alts': [], 'year': str(r['year'] or ''),
                                  'type': kind or 'not stated', 'match': r['confidence'] + ' confidence · ' + r['why']}, ensure_ascii=False)
            color = {'High': '#3f8f5f', 'Medium': '#b08a2e', 'Low': '#7a5252'}[r['confidence']]
            box += ['<div class="sm-card">',
                    '<button type="button" class="sm-art" data-art-preview="', _e(preview), '" aria-label="View larger artwork for ', _e(r['name']), '">',
                    '<img loading="lazy" alt="" src="', _e(art), '" data-noart="No AnimeThemes artwork"></button>',
                    '<div class="sm-info"><h4>', _e(r['name']), '</h4>',
                    '<p>', _e(r['year'] or 'year unknown'), (' · ' + _e(kind)) if kind else '', '</p>',
                    '<p><strong style="color:', color, '">', _e(r['confidence']), ' confidence</strong> · ', _e(r['why']), '</p>',
                    '<div class="sm-actions"><a style="color:#b8efff;font-weight:700;text-decoration:underline" href="',
                    _e('/themes?' + urllib.parse.urlencode({'path': anime_path, 'theme_slug': r['slug']})), '">Preview OP/ED</a>',
                    '<form method="post" action="/v4-save-match"><input type="hidden" name="path" value="', _e(anime_path), '">',
                    '<input type="hidden" name="slug" value="', _e(r['slug']), '"><button type="submit">Remember this anime</button></form>',
                    '</div></div></div>']
    if notes:
        box += ['<details><summary class="muted">Details (', str(len(notes)), ')</summary><ul>'] + \
               ['<li class="muted">' + _e(n) + '</li>' for n in notes] + ['</ul></details>']
    box += ['<p class="muted">AniDB title list: ', f"{st['anime']:,}", ' anime, updated ', _e(st['updated']), '.</p>']
    return ''.join(box)


# ------------------------------------------------------------ Settings → Connect card ----
# AniDB has no "API key" for this use: the daily title list needs no account, and AniDB's live HTTP API
# identifies a registered *client* (name + version). Both are optional; stored privately (0600).
HTTP_API = 'http://api.anidb.net:9001/httpapi'
_SETTINGS = 'settings.json'
_LAST_TEST = [0.0]
_REGISTER = 'https://anidb.net/user/register'
_ADD_CLIENT = 'https://anidb.net/software/add'
_API_DOCS = 'https://wiki.anidb.net/HTTP_API_Definition'
_DUMP_DOCS = 'https://wiki.anidb.net/API:Anime_titles_dump'


def settings(config):
    path = _folder(config) / _SETTINGS
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return {'enabled': True, 'client': '', 'clientver': ''}
    with os.fdopen(fd, 'rb') as f:
        raw = f.read(4097)
    data = json.loads(raw) if len(raw) <= 4096 else {}
    return {'enabled': data.get('enabled', True) is not False, 'client': str(data.get('client', ''))[:32],
            'clientver': str(data.get('clientver', ''))[:6]}


def _save_settings(config, data):
    folder = _folder(config)
    folder.mkdir(mode=0o755, exist_ok=True)
    tmp = folder / ('.' + _SETTINGS + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, folder / _SETTINGS)


def enabled(config):
    try:
        return settings(config)['enabled']
    except (OSError, ValueError):
        return True


def _test_client(config):
    s = settings(config)
    if not s['client'] or not s['clientver']:
        return False, 'Enter and save a client name and version first.'
    if time.monotonic() - _LAST_TEST[0] < 60:
        return False, 'Please wait a minute between tests (AniDB allows very few requests).'
    _LAST_TEST[0] = time.monotonic()
    url = HTTP_API + '?' + urllib.parse.urlencode({'request': 'anime', 'client': s['client'], 'clientver': s['clientver'],
                                                    'protover': 1, 'aid': 1})
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'gzip'})
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read(2 * 1024 * 1024)
        if raw[:2] == b'\x1f\x8b':
            raw = gzip.decompress(raw)
        text = raw.decode('utf-8', 'replace')
    except Exception as error:
        return False, 'Could not reach AniDB (' + type(error).__name__ + ').'
    match = re.search(r'<error[^>]*>(.*?)</error>', text, re.S)
    if match:
        return False, 'AniDB answered: ' + html.unescape(match.group(1)).strip()[:160]
    if '<anime ' in text:
        return True, 'AniDB accepted this client.'
    return False, 'AniDB returned an unexpected answer.'


def settings_post(handler, config, redirect):
    """POST /anidb-settings (sign-in and same-origin already checked by Themearr)."""
    try:
        length = int(handler.headers.get('Content-Length', '0'))
        if not 1 <= length <= 2048:
            return handler.send(413, 'Invalid form size', 'text/plain')
        form = urllib.parse.parse_qs(handler.rfile.read(length).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=6)
        if any(len(v) != 1 for v in form.values()):
            return handler.send(400, 'Invalid form', 'text/plain')
        action = form.get('action', [''])[0]
        current = settings(config)
        if action == 'save':
            client = form.get('client', [''])[0].strip()
            ver = form.get('clientver', [''])[0].strip()
            if client and not re.fullmatch(r'[a-z0-9_-]{1,32}', client):
                return redirect(handler, '/media-servers?anidb_notice=Client+name:+lowercase+letters,+digits,+-+or+_+only#themearr-anidb')
            if ver and not re.fullmatch(r'[0-9]{1,6}', ver):
                return redirect(handler, '/media-servers?anidb_notice=Client+version+must+be+a+number#themearr-anidb')
            current.update(client=client or current['client'], clientver=ver or current['clientver'],
                           enabled=form.get('enabled', [''])[0] == '1')
            _save_settings(config, current)
            msg = 'AniDB settings saved'
        elif action == 'clear':
            current.update(client='', clientver='')
            _save_settings(config, current)
            msg = 'AniDB client removed'
        elif action == 'test':
            ok, msg = _test_client(config)
        elif action == 'refresh':
            age = time.time() - _state(config).get('last_attempt', 0)
            if age < MIN_INTERVAL:
                msg = 'AniDB allows one title-list download per day; next refresh possible in about %d h' % ((MIN_INTERVAL - age) // 3600 + 1)
            else:
                _save_state(config, last_attempt=0)
                file = _folder(config) / 'anime-titles.xml.gz'
                if file.exists():
                    os.utime(file, (0, 0))  # mark as due; the download replaces it only after a successful check
                    _DATA['mtime'] = 0
                ensure(config)
                msg = 'AniDB title list refresh started'
        else:
            return handler.send(400, 'Unknown action', 'text/plain')
        return redirect(handler, '/media-servers?' + urllib.parse.urlencode({'anidb_notice': msg}) + '#themearr-anidb')
    except (UnicodeDecodeError, ValueError, OSError):
        return handler.send(400, 'Could not update AniDB settings.', 'text/plain')


def settings_html(config, notice=''):
    try:
        s = settings(config)
    except (OSError, ValueError):
        s = {'enabled': True, 'client': '', 'clientver': ''}
    st = status(config)
    next_ok = st['last_attempt'] + MIN_INTERVAL - time.time()
    list_status = (f"{st['anime']:,} anime · updated {st['updated']}" if st['available'] else
                   ('loading…' if st['loading'] or st['downloading'] else 'not downloaded yet'))
    client_status = (f"{s['client']} v{s['clientver']}" if s['client'] and s['clientver'] else 'Not configured (optional)')
    return ('''<section id="themearr-anidb" aria-labelledby="anidb-title" style="margin:24px 0;padding:23px;border:1px solid #485c7b;background:#1c283e;border-radius:14px;color:#edf4ff">
<h2 id="anidb-title">''' + _logo(72) + '''<span style="position:absolute;left:-9999px">AniDB</span></h2>
<p style="color:#c5d4ea">Used by Smart Matching 2.0 to identify anime by their AniDB ID. Original Matching always runs first and is never changed; AniDB results are suggestions you confirm.</p>'''
            + ('<p role="status" style="border:1px solid #617aa2;padding:10px;background:#304565">' + _e(notice[:200]) + '</p>' if notice else '') +
            '''<div class="pc-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,290px),1fr));gap:16px">
<div class="pc-card" style="padding:18px;border:1px solid #455a7a;background:#24334e;border-radius:11px">
<h3>AniDB title list</h3>
<p>Status: <strong>''' + _e(list_status) + '''</strong></p>
<p style="color:#c5d4ea">AniDB's official list of every anime title (all languages). <strong>No account or key needed.</strong> Preludarr downloads it automatically at most once a day and keeps the last copy if AniDB is unreachable. <a style="color:#a7c9ff" href="''' + _DUMP_DOCS + '''" target="_blank" rel="noopener noreferrer">About the title list ↗</a></p>'''
            + ('<p role="alert">' + _e(st['error']) + '</p>' if st['error'] else '') +
            '''<form method="post" action="/anidb-settings"><input type="hidden" name="action" value="refresh">
<button type="submit"''' + (' disabled title="Allowed again in about %d h"' % (next_ok // 3600 + 1) if next_ok > 0 else '') + '''>Refresh now</button></form>
</div>
<div class="pc-card" style="padding:18px;border:1px solid #455a7a;background:#24334e;border-radius:11px">
<h3>AniDB HTTP API client (optional)</h3>
<p>Client: <strong>''' + _e(client_status) + '''</strong></p>
<p style="color:#c5d4ea">AniDB does not issue API keys. Its live API identifies a <em>registered client</em> instead. Not required today — only for future features that ask AniDB directly (for example exact years and episode data). How to get one:</p>
<ol style="color:#c5d4ea;line-height:1.5">
<li>Create a free AniDB account: <a style="color:#a7c9ff" href="''' + _REGISTER + '''" target="_blank" rel="noopener noreferrer">anidb.net registration ↗</a></li>
<li>Sign in, then register a client: <a style="color:#a7c9ff" href="''' + _ADD_CLIENT + '''" target="_blank" rel="noopener noreferrer">Add client ↗</a> — choose <strong>HTTP API</strong>, pick a lowercase name (e.g. <code>themearr</code>) and version <code>1</code>.</li>
<li>Enter the same name and version here, Save, then Test.</li>
</ol>
<p style="color:#c5d4ea">AniDB is strict about request rates (bans for over-use); Preludarr keeps requests to a minimum. <a style="color:#a7c9ff" href="''' + _API_DOCS + '''" target="_blank" rel="noopener noreferrer">HTTP API rules ↗</a></p>
<form method="post" action="/anidb-settings" autocomplete="off">
<label for="anidb-client" style="display:block;margin:11px 0 4px;font-weight:600">Client name</label>
<input id="anidb-client" name="client" maxlength="32" pattern="[a-z0-9_-]{1,32}" placeholder="''' + _e(s['client'] or 'e.g. themearr') + '''" style="display:block;width:100%;padding:11px;background:#101c30;color:#fff;border:1px solid #647da0;border-radius:7px">
<label for="anidb-ver" style="display:block;margin:11px 0 4px;font-weight:600">Client version</label>
<input id="anidb-ver" name="clientver" maxlength="6" inputmode="numeric" pattern="[0-9]{1,6}" placeholder="''' + _e(s['clientver'] or 'e.g. 1') + '''" style="display:block;width:100%;padding:11px;background:#101c30;color:#fff;border:1px solid #647da0;border-radius:7px">
<label style="display:flex;gap:8px;align-items:center;margin:12px 0"><input type="checkbox" name="enabled" value="1"''' + (' checked' if s['enabled'] else '') + '''> Use AniDB identification in the Theme Browser</label>
<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px">
<button name="action" value="save">Save</button>
<button name="action" value="test" formnovalidate style="background:#46546d">Test client</button>
<button name="action" value="clear" formnovalidate style="background:#9b394a">Remove client</button>
</div></form>
</div></div>
<p style="font-size:.9em;color:#c5d4ea">Blank fields keep the current value. AniDB data is used under AniDB's own terms.</p>
</section>''')
