# SPDX-License-Identifier: GPL-3.0-or-later
"""Theme suggestions for Shows and Movies on the title page (Preludarr 1.11).

Sources by type (kinds): Shows -> Plex TV Themes, ThemerrDB; Movies -> ThemerrDB (TMDB, else IMDb); then, only when
those found nothing, the optional YouTube search (preview only, youtube_search).
  * Plex / ThemerrDB suggestions can be installed after an explicit confirmation, through the existing safe installer
    (sources.install -> manual_install_mp3, replace=False: an existing theme.mp3 is never replaced).
  * YouTube search results have NO install button (YouTube API policy): the user may copy a link into the title's YouTube box.
Only suggestions saved for that exact title (theme_suggestions) can be installed — the form sends an index, never a URL.
"""
import html
import json
import re
import time
import urllib.parse

_VID = re.compile(r'https://(?:www\.)?youtube\.com/watch\?v=([A-Za-z0-9_-]{11})')


def _e(value):
    return html.escape(str(value), quote=True)


def _mmss(seconds):
    if not isinstance(seconds, int):
        return ''
    return f'{seconds // 60}:{seconds % 60:02d}'


def _player(video_id, title):
    """Click-to-load embedded player (youtube-nocookie), so a page with 5 results doesn't load 5 players."""
    return ('<div class="rg-player" data-vid="' + _e(video_id) + '"><button type="button" class="rg-play" data-rg-play aria-label="Preview ' + _e(title) +
            ' on YouTube">▶ Preview</button></div>')


def panel(app, path, name, library):
    kinds = __import__('kinds')
    src = __import__('sources')
    yt = __import__('youtube_search')
    found, notes = src.cached_find(app, path, name, library)
    order = ('Plex TV Themes → ThemerrDB' if library == 'Shows' else 'ThemerrDB') + ' → the MainThemeSoundtracks YouTube channel'
    parts = ['<section class="panel" id="tp-suggest"><h3>Theme suggestions</h3>',
             '<p class="muted">', _e(kinds.label(library)), ' look for themes in this order: ', _e(order),
             ', then YouTube search (preview only) if you turned it on. Nothing is installed until you confirm.</p>']
    installable = [(i, f) for i, f in enumerate(found) if f.get('source') in ('plex', 'themerrdb')]
    import os
    writable = os.access(path, os.W_OK)  # 1.11.5: say BEFORE the click that a read-only Docker volume blocks installs
    if installable and not writable:
        parts += ['<p class="notice rg-ro" role="status">🔒 This folder is <strong>read-only</strong> in Docker, so Preludarr can preview these themes but cannot save theme.mp3 here yet. ',
                  'To install, make this library’s volume <strong>read/write</strong> in your Docker settings (Unraid: Docker → Preludarr → Edit; Compose: remove <code>:ro</code>).</p>']
    searched = [f for f in found if f.get('source') == 'youtube_search']
    for i, f in installable:
        vid = _VID.fullmatch(str(f.get('url') or ''))
        parts += ['<div class="rg-item"><div class="rg-head"><strong>', _e(f.get('track', '')), '</strong> <span class="tag on">', _e(f.get('label', '')), '</span></div>',
                  '<p class="muted rg-why">Found by exact ID: ', _e(f.get('note', '')), '</p>']
        if f['source'] == 'plex':
            parts += ['<audio controls preload="none" src="', _e(f['url']), '"></audio>']
        elif vid:
            parts.append(_player(vid.group(1), f.get('track', '')))
        parts += ['<form method="post" action="/regular/install" class="rg-install"><input type="hidden" name="path" value="', _e(path), '">',
                  '<input type="hidden" name="idx" value="', str(i), '">',
                  '<label class="check"><input type="checkbox" name="confirm" value="1" required', '' if writable else ' disabled', '> Install this as theme.mp3',
                  ' (downloads through the YouTube download client)' if f['source'] == 'themerrdb' else '', '</label>',
                  '<button type="submit"', '' if writable else ' disabled title="The folder is read-only in Docker"', '>Install</button></form></div>']
    if searched:
        parts += ['<h4 class="rg-sub">YouTube search results <span class="muted">— preview only</span></h4>',
                  '<p class="muted rg-why">YouTube’s API rules don’t let Preludarr download what it found through search. To use one, press ',
                  '<strong>Use this</strong> (it fills in <em>YouTube → Paste one video link</em> below) or <strong>Copy link</strong>, then review and install — that download is your own choice. ',
                  'Check the warnings: covers, live versions, trailers and long compilations are common.</p>']
        for f in searched:
            flags = f.get('flags') or []
            parts += ['<div class="rg-item"><div class="rg-head"><strong>', _e(f.get('track', '')), '</strong>',
                      ' <span class="tag on">⭐ ' + _e(yt.CHANNEL_NAME) + '</span>' if f.get('trusted') else '', '</div>',
                      '<p class="muted rg-why">', _e(f.get('channel', '')), (' · ' + _mmss(f.get('seconds'))) if f.get('seconds') is not None else '', '</p>',
                      ('<p class="rg-flags">' + ''.join('<span class="tag warn">⚠ ' + _e(x) + '</span>' for x in flags) + '</p>') if flags else '',
                      _player(f['video_id'], f.get('track', '')),
                      '<p class="rg-copy"><button type="button" data-rg-use="', _e(f['url']), '">Use this</button> <button type="button" class="btn ghost" data-rg-copy="', _e(f['url']), '">Copy link</button> ',
                      '<a href="', _e(f['url']), '" target="_blank" rel="noopener noreferrer">Open on YouTube ↗</a></p></div>']
    if searched and all(f.get('trusted') for f in searched) and yt.ready():
        parts += ['<form method="post" action="/regular/suggest" class="rg-again"><input type="hidden" name="path" value="', _e(path), '">',
                  '<input type="hidden" name="scope" value="all"><button type="submit" class="btn ghost">Search all of YouTube</button>',
                  '<span class="muted">Not the right version? Uses one YouTube search.</span></form>']
    if not found:
        parts.append('<p>No theme found yet for this title.</p>')
    if notes:
        parts += ['<ul class="muted rg-notes">'] + ['<li>' + _e(n) + '</li>' for n in notes] + ['</ul>']
    u, s = yt.usage(), yt.settings()
    parts += ['<form method="post" action="/regular/suggest" class="rg-again"><input type="hidden" name="path" value="', _e(path), '">',
              '<button type="submit" class="btn ghost">Look again</button><span class="muted">',
              ('Uses one YouTube search if Plex/ThemerrDB still find nothing (' + str(u['used']) + ' of ' + str(s['budget']) + ' used today).') if yt.ready() else
              'Asks Plex TV Themes / ThemerrDB again (YouTube search is off).', '</span></form>',
              '<style>#tp-suggest .rg-item{border:1px solid #334967;border-radius:10px;padding:12px 14px;margin:10px 0;background:#111b2d}',
              '#tp-suggest .rg-head{display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}#tp-suggest .rg-why{margin:4px 0 8px;font-size:.9em}',
              '#tp-suggest audio{width:min(460px,100%);height:36px}#tp-suggest .rg-install,#tp-suggest .rg-again{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0 0}',
              '#tp-suggest .rg-flags{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 8px}#tp-suggest .tag{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12.5px;font-weight:700;background:#22324f;border:1px solid #3e5174;color:#cfe0f8}',
              '#tp-suggest .tag.on{background:#173a2a;border-color:#3f8f5f;color:#c8f5d8}#tp-suggest .tag.warn{background:#3a2f12;border:1px solid #8a6d1f;color:#ffe9b0}',
              '#tp-suggest .rg-player{width:min(400px,100%);aspect-ratio:16/9;background:#0b1220;border:1px solid #334967;border-radius:8px;display:flex;align-items:center;justify-content:center}',
              '#tp-suggest .rg-player iframe{width:100%;height:100%;border:0;border-radius:8px}#tp-suggest .rg-sub{margin:18px 0 4px}#tp-suggest .rg-copy{margin:8px 0 0}',
              '#tp-suggest .rg-notes{margin:10px 0 0;padding-left:20px}',
              '#tp-suggest .rg-progress{margin:10px 0 0;padding:10px 12px;border:1px solid #3e5174;border-radius:9px;background:#0f1a2c}',
              '#tp-suggest .rg-progress.ok{border-color:#3f8f5f;background:#132f22}#tp-suggest .rg-progress.bad{border-color:#9b394a;background:#2d151b}',
              '#tp-suggest .rg-progress .rg-now{margin:0;font-weight:600}#tp-suggest .rg-progress ol{margin:6px 0 0;padding-left:22px;color:#9fb0cc;font-size:.92em}</style>',
              '<script>(function(){"use strict";const box=document.getElementById("tp-suggest");if(!box)return;',
              'box.addEventListener("click",e=>{const p=e.target.closest("[data-rg-play]");if(p){const d=p.parentElement,f=document.createElement("iframe");',
              'f.src="https://www.youtube-nocookie.com/embed/"+encodeURIComponent(d.dataset.vid)+"?autoplay=1&rel=0";f.title="YouTube preview";',
              'f.allow="autoplay; encrypted-media; picture-in-picture";f.referrerPolicy="strict-origin-when-cross-origin";f.allowFullscreen=true;d.replaceChildren(f);return}',
              '});box.addEventListener("submit",async e=>{const f=e.target.closest("form.rg-install");if(!f||!window.fetch||!window.TextDecoder)return;e.preventDefault();',
              'const btn=f.querySelector("button[type=submit]"),cb=f.querySelector("input[name=confirm]");if(cb&&!cb.checked){cb.focus();return}',
              'let st=f.nextElementSibling;if(!st||!st.classList.contains("rg-progress")){st=document.createElement("div");st.className="rg-progress";st.setAttribute("role","status");st.setAttribute("aria-live","polite");f.after(st)}',
              'box.querySelectorAll("form.rg-install button").forEach(b=>b.disabled=true);',
              'const t0=Date.now(),list=document.createElement("ol"),line=document.createElement("p");line.className="rg-now";st.replaceChildren(line,list);',
              'let cur="Starting the install…";const tick=()=>{line.textContent="⏳ "+cur+" · "+Math.round((Date.now()-t0)/1000)+" s"};tick();const timer=setInterval(tick,1000);',
              'const done=(ok,msg)=>{clearInterval(timer);line.textContent=(ok?"✅ ":"❌ ")+msg+" ("+Math.round((Date.now()-t0)/1000)+" s)";st.classList.add(ok?"ok":"bad");',
              'if(ok){setTimeout(()=>location.href=location.pathname+"?"+new URLSearchParams({path:new URLSearchParams(location.search).get("path")||"",installed:"1"}),1500)}',
              'else box.querySelectorAll("form.rg-install button").forEach(b=>b.disabled=false)};',
              'try{const r=await fetch(f.action,{method:"POST",credentials:"same-origin",headers:{"X-Preludarr-Progress":"v1","Content-Type":"application/x-www-form-urlencoded"},body:new URLSearchParams(new FormData(f))});',
              'if(!r.ok||!r.body){done(false,"The install could not start (HTTP "+r.status+").");return}',
              'const rd=r.body.getReader(),dec=new TextDecoder();let buf="",fin=false;',
              'for(;;){const {value,done:end}=await rd.read();if(end)break;buf+=dec.decode(value,{stream:true});let i;',
              'while((i=buf.indexOf("\\n"))>=0){const ln=buf.slice(0,i).trim();buf=buf.slice(i+1);if(!ln)continue;let m;try{m=JSON.parse(ln)}catch(_){continue}',
              'if(m.phase==="complete"){fin=true;done(true,m.message)}else if(m.phase==="error"){fin=true;done(false,m.message)}',
              'else{cur=m.message;const li=document.createElement("li");li.textContent=m.message;list.append(li);tick()}}}',
              'if(!fin)done(false,"The connection closed before the install finished — refresh the page to see whether theme.mp3 was installed.")}',
              'catch(err){done(false,"Network error — refresh the page to see whether theme.mp3 was installed.")}});',
              'box.addEventListener("click",e=>{const c=e.target.closest("[data-rg-copy]");if(c){const url=c.dataset.rgCopy,done=()=>{c.textContent="Copied ✓";',
              'const yt=document.querySelector("#themearr-youtube-form input[name=youtube_url]");if(yt&&!yt.value){const d=yt.closest("details");if(d)d.open=true;}};',
              'if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(url).then(done,()=>prompt("Copy this link:",url));else prompt("Copy this link:",url)}});',
              # 1.12 (user: Shows like Collections): "Use this" fills the title page's YouTube box; the review step there decides
              'const ytForm=document.querySelector("#themearr-youtube-form");if(!ytForm)box.querySelectorAll("[data-rg-use]").forEach(b=>b.hidden=true);',
              'box.addEventListener("click",e=>{const u=e.target.closest("[data-rg-use]");if(!u||!ytForm)return;const yt=ytForm.querySelector("input[name=youtube_url]");',
              'yt.value=u.dataset.rgUse;let d=yt.closest("details");while(d){d.open=true;d=d.parentElement.closest("details")}',
              'yt.scrollIntoView({block:"center"});ytForm.classList.add("rg-picked");const s=ytForm.querySelector("button[type=submit]");if(s)s.focus()});',
              '})();</script><style>#themearr-youtube-form.rg-picked{outline:2px solid #4f8cff;outline-offset:6px;border-radius:8px}</style></section>']
    return ''.join(parts)


def _mark_installed(app, path, proposal):
    with app.db() as c:
        c.execute("UPDATE shows SET state='installed',detail=?,updated=? WHERE path=?",
                  (proposal.get('label', '') + ' · ' + proposal.get('note', ''), time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime()), path))


def _install_streamed(app, handler, path, proposal):
    """Preludarr 1.11.8 (user 2026-10-29: "when install is clicked can there be a progress/status of the install"):
    one JSON line per step (application/x-ndjson), the same way the anime installer reports progress."""
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Accel-Buffering', 'no')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Connection', 'close')
    handler.end_headers()
    handler.close_connection = True

    def report(phase, message):
        try:
            handler.wfile.write((json.dumps({'phase': phase, 'message': message}, separators=(',', ':')) + '\n').encode('utf-8'))
            handler.wfile.flush()
        except OSError:
            pass
    try:
        __import__('sources').install(app, path, proposal, progress=report)
        _mark_installed(app, path, proposal)
        report('complete', 'Installed — theme.mp3 is ready.')
    except Exception as error:  # plain reasons only (read-only folder, YouTube limits, conversion problems…)
        report('error', 'Not installed: ' + ((str(error) or type(error).__name__)[:220]))
    return None


def post(app, handler, redirect):
    route = urllib.parse.urlsplit(handler.path).path
    path = ''
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 4096:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=5)
        if any(len(v) != 1 for v in form.values()):
            raise ValueError('Invalid form.')
        f = lambda k: form.get(k, [''])[0]  # noqa: E731
        path = f('path')
        with app.db() as c:
            row = c.execute('SELECT name,library FROM shows WHERE path=?', (path,)).fetchone()
        if not row or not __import__('kinds').is_regular(row['library']):
            raise ValueError('That title is not in a Shows or Movies root folder.')
        src = __import__('sources')
        if route == '/regular/suggest':
            everywhere = f('scope') == 'all'  # 1.12: after a MainThemeSoundtracks match, the user can still search all of YouTube
            found, _ = src.cached_find(app, path, row['name'], row['library'], refresh=True, everywhere=everywhere)
            msg = (f'Searched all of YouTube: {len(found)} suggestion(s).' if everywhere else f'Looked again: {len(found)} suggestion(s).')
        else:
            if f('confirm') != '1':
                raise ValueError('Tick the confirmation box to install.')
            with app.db() as c:
                saved = c.execute('SELECT found FROM theme_suggestions WHERE path=?', (path,)).fetchone()
            found = json.loads(saved['found']) if saved else []
            idx = int(f('idx')) if f('idx').isdigit() else -1
            if not 0 <= idx < len(found) or found[idx].get('source') not in ('plex', 'themerrdb'):
                raise ValueError('That suggestion changed meanwhile — look again.')
            proposal = found[idx]
            if handler.headers.get('X-Preludarr-Progress') == 'v1':  # 1.11.8: live progress for the title page
                return _install_streamed(app, handler, path, proposal)
            src.install(app, path, proposal)
            _mark_installed(app, path, proposal)
            return redirect(handler, '/themes?' + urllib.parse.urlencode({'path': path, 'installed': '1'}))
    except (ValueError, OSError, UnicodeError, RuntimeError) as error:
        msg = 'Not done: ' + (str(error) or type(error).__name__)
    return redirect(handler, '/themes?' + urllib.parse.urlencode({'path': path, 'suggest_msg': msg[:220]}) + '#tp-suggest')
