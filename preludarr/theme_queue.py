# SPDX-License-Identifier: GPL-3.0-or-later
"""Bulk Theme Queue (Themearr V1.9.0).

A  Choose   — add titles from the library filters (missing / needs review / recently added / library / search).
B  Dry run  — in the background, one title at a time, find the theme Themearr WOULD install (nothing is installed):
              a remembered match or Original Matching on AnimeThemes (unchanged rules: exact title + year checks),
              then — only if AnimeThemes has nothing — enabled secondary sources (always flagged for review).
C  Approve  — the user approves all or selected proposals; installs run one at a time (Pause / Resume / Cancel)
              through the existing safe installer with replace=False: an existing theme.mp3 is never replaced.
D  Results  — installed / skipped / no match / failed, with Retry; kept across restarts (table theme_queue).

Resource limits: one worker thread, a pause between items, AnimeThemes requests paced by the V1.6.4 client,
at most MAX_ADD titles per add, installs never run while another install or a library scan is running.
"""
import html
import json
import threading
import time
import urllib.parse
from pathlib import Path

MAX_ADD = 500
STEP_PAUSE = 2.0
FINAL = ('installed', 'skipped', 'no_match', 'failed', 'cancelled')
LABELS = {'queued': 'Waiting for dry run', 'searching': 'Looking up…', 'proposed': 'Ready for your review', 'approved': 'Approved — waiting to install',
          'installing': 'Installing…', 'installed': 'Installed', 'skipped': 'Skipped', 'no_match': 'No theme found', 'failed': 'Failed',
          'cancelled': 'Cancelled'}
_STATE = {'thread': None, 'paused': False, 'wake': threading.Event()}
_START_LOCK = threading.Lock()  # guarantees a single worker thread


def _e(value):
    return html.escape(str(value), quote=True)


def _now():
    return int(time.time())


# ------------------------------------------------------------------------------ queue data ----
def add(app, paths):
    """Add titles (skips titles already waiting in the queue and titles that already have a theme)."""
    added = skipped = 0
    with app.db() as c:
        active = {r[0] for r in c.execute("SELECT path FROM theme_queue WHERE state NOT IN ('installed','skipped','no_match','failed','cancelled')")}
        for path in list(dict.fromkeys(paths))[:MAX_ADD]:
            row = c.execute('SELECT name,library,state FROM shows WHERE path=?', (path,)).fetchone()
            if not row or path in active or row['state'] == 'installed' or (Path(path) / 'theme.mp3').exists():
                skipped += 1
                continue
            c.execute('INSERT INTO theme_queue(path,name,library,state,source,proposal,note,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                      (path, row['name'], row['library'], 'queued', '', '', '', _now(), _now()))
            added += 1
    wake(app)
    return added, skipped


def _set(app, qid, state, **fields):
    cols = ', '.join(['state=?', 'updated=?'] + [k + '=?' for k in fields])
    with app.db() as c:
        c.execute(f'UPDATE theme_queue SET {cols} WHERE id=?', [state, _now(), *fields.values(), qid])


def counts(app):
    with app.db() as c:
        return {r[0]: r[1] for r in c.execute('SELECT state,COUNT(*) FROM theme_queue GROUP BY state')}


# ------------------------------------------------------------------------------- dry run ----
def propose(app, row):
    """Phase B for one title: what WOULD be installed. Never installs."""
    import sources
    import media_servers as v4
    path, name = row['path'], row['name']
    if (Path(path) / 'theme.mp3').exists():
        return 'skipped', '', None, 'Already has a theme.mp3 — left untouched.'
    if __import__('kinds').is_regular(row['library']):  # Preludarr 1.11: Shows / Movies skip AnimeThemes
        found, notes = sources.cached_find(app, path, name, row['library'], interactive=False)
        ready = [f for f in found if f.get('source') in ('plex', 'themerrdb')]
        if ready:
            return 'proposed', ready[0]['source'], {'kind': 'fallback', **ready[0], 'how': 'Found by exact ID — check before approving'}, ' '.join(notes)
        if found:
            return 'no_match', '', None, f'{len(found)} YouTube search result(s) to preview on the title page (YouTube results are never installed by the queue).'
        return 'no_match', '', None, ' '.join(notes) + ' Use the title page for a manual upload or YouTube link.'
    show, how = None, ''
    try:
        if v4.has_override(app.CONFIG, path):
            show = v4.resolve(app.CONFIG, path, name, {}, app.anime_theme, app.request_json)
            how = 'Your remembered match'
            if not show:
                return 'failed', '', None, 'Your remembered match could not be loaded from AnimeThemes right now — Retry later.'
        else:
            try:
                media = app.anilist(name)
            except Exception as error:
                if getattr(error, 'code', None) != 404:  # AniList answers "not found" with HTTP 404
                    raise
                media = None
            yr = app.year(name)
            if media and not (yr and media.get('seasonYear') and yr != media.get('seasonYear')):
                show = app.anime_theme(name, media)
                if show and yr and show.get('year') and int(show['year']) != yr:
                    show = None
            how = 'Original Matching (exact title + year)'
    except Exception as error:
        return 'failed', '', None, 'AnimeThemes / AniList could not be reached (' + type(error).__name__ + ') — Retry later.'
    if show:
        tracks = [t for t in app.list_available_themes(show) if t['type'].startswith('OP')] or app.list_available_themes(show)
        if tracks:
            t = tracks[0]
            proposal = {'kind': 'animethemes', 'anime_slug': show.get('slug'), 'anime': show.get('name'), 'year': show.get('year'),
                        'video_id': t['video_id'], 'track': f"{t['slug']} — {t['title']}" + (f" ({t['artist']})" if t.get('artist') else ''),
                        'audio_url': t['audio_url'], 'how': how}
            return 'proposed', 'animethemes', proposal, ''
        notes = ['AnimeThemes has this anime but no audio for it.']
    else:
        notes = ['AnimeThemes: no exact match.']
    try:
        found, more = sources.find(app, path, name, row['library'])
    except Exception as error:
        found, more = [], ['Secondary sources could not be checked (' + type(error).__name__ + ').']
    if found:
        f = found[0]
        proposal = {'kind': 'fallback', **f, 'how': 'Secondary source — check before approving'}
        return 'proposed', f['source'], proposal, ' '.join(notes + more)
    return 'no_match', '', None, ' '.join(notes + more) + ' Use the Theme Browser for a manual search, upload or YouTube link.'


# ------------------------------------------------------------------------------- install ----
def install(app, row, proposal):
    """Phase C for one approved title, through the existing safe installer (replace=False)."""
    folder = Path(row['path'])
    if proposal['kind'] == 'animethemes':
        track = app.resolve_animethemes_track(proposal['anime_slug'], str(proposal['video_id']))
        with app.stage_verified_theme_audio(track, app.CONFIG) as audio:
            app.manual_install_mp3(folder, audio, replace=False, anime_slug=proposal['anime_slug'], video_id=str(proposal['video_id']),
                                   activity_label='Bulk queue · ' + proposal['track'])
    else:
        import sources
        sources.install(app, row['path'], proposal)
    with app.db() as c:
        c.execute("UPDATE shows SET state='installed',detail=?,updated=? WHERE path=?",
                  ('Bulk queue · ' + proposal.get('track', ''), time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime()), row['path']))


# -------------------------------------------------------------------------------- worker ----
def wake(app):
    _STATE['wake'].set()
    with _START_LOCK:
        t = _STATE['thread']
        if t is None or not t.is_alive():
            _STATE['thread'] = threading.Thread(target=_worker, args=(app,), daemon=True, name='themearr-queue')
            _STATE['thread'].start()


def recover(app):
    """After a restart: interrupted steps go back one step."""
    with app.db() as c:
        c.execute("UPDATE theme_queue SET state='queued' WHERE state='searching'")
        c.execute("UPDATE theme_queue SET state='approved' WHERE state='installing'")


def step(app):
    """Do one unit of work (an approved install first, else one dry run). Returns True if something was done."""
    if _STATE['paused'] or app.STATUS.get('running'):
        return False
    with app.db() as c:
        row = (c.execute("SELECT * FROM theme_queue WHERE state='approved' ORDER BY updated, id LIMIT 1").fetchone()
               or c.execute("SELECT * FROM theme_queue WHERE state='queued' ORDER BY id LIMIT 1").fetchone())
    if not row:
        return False
    if row['state'] == 'queued':
        _set(app, row['id'], 'searching')
        try:
            state, source, proposal, note = propose(app, row)
        except Exception as error:
            state, source, proposal, note = 'failed', '', None, 'Lookup error (' + type(error).__name__ + ').'
        _set(app, row['id'], state, source=source, proposal=json.dumps(proposal) if proposal else '', note=note)
        return True
    _set(app, row['id'], 'installing')
    try:
        install(app, row, json.loads(row['proposal']))
        _set(app, row['id'], 'installed', note='Installed as theme.mp3.')
    except Exception as error:
        text = str(error) or type(error).__name__
        if 'Another manual installation is running' in text:
            _set(app, row['id'], 'approved', note='Waiting for another install to finish.')
            return False
        if 'Existing theme' in text:
            _set(app, row['id'], 'skipped', note='A theme.mp3 appeared meanwhile — left untouched.')
        else:
            _set(app, row['id'], 'failed', note=text[:200])
    return True


def _worker(app):
    recover(app)
    while True:
        _STATE['wake'].clear()
        try:
            did = step(app)
        except Exception as error:
            print('Theme queue error: ' + type(error).__name__, flush=True)
            did = False
        _STATE['wake'].wait(STEP_PAUSE if did else 30)


# ------------------------------------------------------------------------------------ UI ----
def candidates(app, query):
    """Titles matching the dashboard filters that could get a theme (missing / needs review)."""
    term = query.get('q', [''])[0].strip()[:120]
    library = query.get('library', ['all'])[0]
    state = query.get('state', ['all'])[0]
    clauses, params = ["state IN ('missing','review')"], []
    if term:
        clauses.append("name LIKE ? ESCAPE '\\'")
        params.append('%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
    sql, extra = __import__('root_folders').scope_clause(library)  # Preludarr 1.11.9: type or 'root:<path>' tab
    if sql:
        clauses.append(sql); params += extra
    if state in ('missing', 'review'):
        clauses.append('state=?'); params.append(state)
    elif state == 'recent':
        sql, extra = app.auto_themes.recent_clause(); clauses.append(sql); params += extra
    with app.db() as c:
        return [r[0] for r in c.execute('SELECT path FROM shows WHERE ' + ' AND '.join(clauses) + ' ORDER BY name LIMIT ?', params + [MAX_ADD])]


def page(app, handler):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    tab = q.get('tab', ['review'])[0]
    tabs = {'review': ("state='proposed'", 'Ready for review'), 'waiting': ("state IN ('queued','searching','approved','installing')", 'In progress'),
            'done': ("state='installed'", 'Installed'), 'attention': ("state IN ('no_match','failed')", 'Needs attention'),
            'other': ("state IN ('skipped','cancelled')", 'Skipped')}
    tab = tab if tab in tabs else 'review'
    notice = q.get('notice', [''])[0][:220]
    n = counts(app)
    with app.db() as c:
        rows = c.execute('SELECT * FROM theme_queue WHERE ' + tabs[tab][0] + ' ORDER BY updated DESC, id DESC LIMIT 300').fetchall()
    tab_count = lambda k: sum(n.get(s, 0) for s in {'review': ['proposed'], 'waiting': ['queued', 'searching', 'approved', 'installing'],  # noqa: E731
                                                     'done': ['installed'], 'attention': ['no_match', 'failed'], 'other': ['skipped', 'cancelled']}[k])
    parts = ['<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Theme Queue - Preludarr</title><style>',
             app.CSS, '</style></head><body>', app.full_sidebar('queue', fixed=True), '<div class="themearr-content"><main>',
             '<section class="panel"><h1 style="margin:0 0 6px">🧾 Theme Queue</h1>',
             '<p class="muted">Install themes for many titles at once. Step 1: add titles (Dashboard → Library Search → <em>Queue themes</em>). '
             'Step 2: Preludarr looks up what it would install — nothing is installed yet. Step 3: you approve. Installs then run one at a time; '
             'an existing theme.mp3 is never replaced.</p>']
    if notice:
        parts += ['<p class="notice" role="status">', _e(notice), '</p>']
    parts += ['<div class="tq-bar"><span>Worker: <strong>', 'Paused' if _STATE['paused'] else ('Waiting for a library scan' if app.STATUS.get('running') else 'Running'),
              '</strong></span>',
              '<form method="post" action="/queue/action"><input type="hidden" name="action" value="', 'resume' if _STATE['paused'] else 'pause', '">',
              '<button type="submit">', 'Resume' if _STATE['paused'] else 'Pause', '</button></form>',
              '<form method="post" action="/queue/action"><input type="hidden" name="action" value="clear"><button type="submit" class="tq-sub">Clear finished</button></form></div>',
              '<nav class="tq-tabs">'] + ['<a href="/queue?tab=' + k + '"' + (' class="active"' if k == tab else '') + '>' + v[1] + ' (' + str(tab_count(k)) + ')</a>' for k, v in tabs.items()] + ['</nav>']
    if tab == 'review' and rows:
        parts += ['<form method="post" action="/queue/action" class="tq-approve-all"><input type="hidden" name="action" value="approve_all">',
                  '<label class="check"><input type="checkbox" name="confirm" value="1" required> Approve all ', str(len(rows)), ' proposals below</label> ',
                  '<button type="submit">Approve all</button></form>']
    parts.append('<div class="tq-list">')
    for r in rows:
        p = json.loads(r['proposal']) if r['proposal'] else {}
        fallback = p.get('kind') == 'fallback'
        parts += ['<div class="tq-row"><div class="tq-main"><a href="', _e('/themes?' + urllib.parse.urlencode({'path': r['path']})), '"><strong>', _e(r['name']), '</strong></a>',
                  '<small class="muted"> · ', _e(r['library']), '</small><br><span class="tq-state">', _e(LABELS.get(r['state'], r['state'])), '</span>']
        if p:
            parts += ['<p class="tq-prop">', '⚠ ' if fallback else '', _e(p.get('track', '')), ' · <span class="muted">',
                      _e(('AnimeThemes: ' + str(p.get('anime')) + (f" ({p['year']})" if p.get('year') else '')) if not fallback else p.get('label', '')),
                      ' · ', _e(p.get('how', '')), (' · ' + _e(p.get('note', ''))) if fallback and p.get('note') else '', '</span></p>']
            if p.get('audio_url') or (fallback and p.get('source') == 'plex'):
                parts += ['<audio controls preload="none" src="', _e(p.get('audio_url') or p.get('url')), '"></audio>']
            elif fallback:
                parts += ['<p><a href="', _e(p.get('url')), '" target="_blank" rel="noopener noreferrer">Listen on YouTube ↗</a></p>']
        if r['note']:
            parts += ['<p class="muted tq-note">', _e(r['note']), '</p>']
        parts.append('</div><div class="tq-actions">')
        for action, label, states in (('approve', 'Approve', ('proposed',)), ('skip', 'Skip', ('proposed', 'queued', 'no_match', 'failed')),
                                      ('retry', 'Retry', ('failed', 'no_match', 'skipped', 'cancelled')), ('cancel', 'Cancel', ('approved',))):
            if r['state'] in states:
                parts += ['<form method="post" action="/queue/action"><input type="hidden" name="action" value="', action, '"><input type="hidden" name="id" value="',
                          str(r['id']), '"><button type="submit"', ' class="tq-sub"' if action != 'approve' else '', '>', label, '</button></form>']
        parts.append('</div></div>')
    if not rows:
        parts.append('<p class="muted">Nothing here.</p>')
    parts += ['</div></section><style>.tq-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0}.tq-bar form{margin:0}',
              '.tq-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0;border-bottom:1px solid #39496b;padding-bottom:8px}.tq-tabs a{padding:8px 12px;border-radius:8px;color:#c5d8f6;text-decoration:none}',
              '.tq-tabs a.active{background:#294569;color:#fff;font-weight:700}.tq-list{display:flex;flex-direction:column;gap:8px}',
              '.tq-row{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;padding:12px;border:1px solid #324158;border-radius:9px;background:#111b2d}',
              '.tq-main{min-width:0;flex:1}.tq-main a{color:#f1f5ff;text-decoration:none}.tq-state{font-size:.8rem;padding:2px 8px;border-radius:10px;background:#33415e;display:inline-block;margin-top:4px}',
              '.tq-prop{margin:8px 0 4px}.tq-note{margin:4px 0;font-size:.85rem}.tq-row audio{width:min(420px,100%);height:34px}.tq-actions{display:flex;gap:6px;flex-wrap:wrap}.tq-actions form{margin:0}',
              '.tq-sub{background:#46546d!important}.tq-approve-all{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 10px}',
              '@media(max-width:700px){.tq-row{flex-direction:column}}</style>',
              '<script>setTimeout(()=>{if(!document.querySelector("audio:not([paused])")&&', 'true' if tab == 'waiting' else 'false', ')location.reload()},8000)</script>',
              '</main></div></body></html>']
    return handler.send(200, ''.join(parts), 'text/html')


def post(app, handler, redirect):
    path = urllib.parse.urlsplit(handler.path).path
    back = '/queue'
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 <= size <= 8192:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=10)
        f = lambda k: form.get(k, [''])[0]  # noqa: E731
        if path == '/queue/add-filtered':
            if f('confirm') != '1':
                raise ValueError('Tick the confirmation box to add titles.')
            added, skipped = add(app, candidates(app, {k: [f(k)] for k in ('q', 'library', 'state')}))
            msg = f'Added {added} title(s) to the queue' + (f' ({skipped} skipped: already queued or already have a theme)' if skipped else '') + '. Dry run started — nothing is installed until you approve.'
            back = '/queue?tab=waiting'
        else:
            action, qid = f('action'), f('id')
            with app.db() as c:
                if action == 'pause':
                    _STATE['paused'] = True; msg = 'Queue paused (the current step finishes first).'
                elif action == 'resume':
                    _STATE['paused'] = False; wake(app); msg = 'Queue resumed.'
                elif action == 'clear':
                    c.execute("DELETE FROM theme_queue WHERE state IN ('installed','skipped','cancelled')"); msg = 'Finished items cleared (themes stay installed).'
                elif action == 'approve_all':
                    if f('confirm') != '1':
                        raise ValueError('Tick the confirmation box.')
                    n = c.execute("UPDATE theme_queue SET state='approved', updated=? WHERE state='proposed'", (_now(),)).rowcount
                    msg = f'Approved {n} — installing one at a time.'; back = '/queue?tab=waiting'
                elif action in ('approve', 'skip', 'retry', 'cancel') and qid.isdigit():
                    allowed = {'approve': ("'proposed'", 'approved'), 'skip': ("'proposed','queued','no_match','failed'", 'skipped'),
                               'retry': ("'failed','no_match','skipped','cancelled'", 'queued'), 'cancel': ("'approved'", 'cancelled')}[action]
                    n = c.execute(f"UPDATE theme_queue SET state=?, updated=?, note=CASE WHEN ?='queued' THEN '' ELSE note END WHERE id=? AND state IN ({allowed[0]})",
                                  (allowed[1], _now(), allowed[1], int(qid))).rowcount
                    msg = {'approve': 'Approved.', 'skip': 'Skipped.', 'retry': 'Queued again.', 'cancel': 'Cancelled — nothing installed.'}[action] if n else 'That item changed meanwhile; nothing done.'
                else:
                    raise ValueError('Unknown action.')
            wake(app)
    except (ValueError, UnicodeError) as error:
        msg = 'Not done: ' + str(error)
    return redirect(handler, back + ('&' if '?' in back else '?') + urllib.parse.urlencode({'notice': msg[:210]}))
