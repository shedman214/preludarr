# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → Media Management → Unmapped Folders + Match / Import (Themearr V1.7.3, Feature 16 part 2).

A folder in a root folder is *mapped* when Themearr knows its anime for sure: it already has a theme.mp3, or
the user confirmed its AnimeThemes entry (Match / Import here, or "Remember this anime" in the Theme Browser —
same storage, themearr_v4_matches.json). Everything else is *unmapped*: not scanned yet, or no confirmed identity.

Suggestions per folder: Original Matching first (unchanged engine, same year checks as automatic installs), then
AniDB identification. Nothing is accepted automatically. Match / Import only registers the folder in Themearr's
library (if it isn't yet) and remembers the chosen AnimeThemes entry; it never installs audio and never creates,
renames, moves or deletes anything on disk.
"""
import html
import json
import os
import time
import urllib.parse
from pathlib import Path

PAGE_SIZE = 20


def _e(value):
    return html.escape(str(value), quote=True)


def _matches(app):
    import media_servers as v4
    with v4._LOCK:
        return v4._read(app.CONFIG, 'matches')


def folders(app, root):
    """Direct sub-folders of `root` (same rule as the library scan) with their mapping state."""
    root = Path(root)
    on_disk = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith(('.', '!')):
                    continue
                try:
                    if entry.is_dir():
                        on_disk.append(entry.path)
                except OSError:
                    continue
    except OSError:
        return []
    with app.db() as c:
        rows = {r['path']: r['state'] for r in c.execute('SELECT path,state FROM shows WHERE path IN (SELECT value FROM json_each(?))',
                                                         (json.dumps(on_disk),))}
    saved = _matches(app)
    out = []
    for p in sorted(on_disk, key=lambda x: Path(x).name.casefold()):
        state = rows.get(p)
        mapped = state == 'installed' or bool(saved.get(p))
        out.append({'path': p, 'name': Path(p).name, 'registered': state is not None, 'state': state or '', 'mapped': mapped})
    return out


def unmapped(app, root):
    return [f for f in folders(app, root) if not f['mapped']]


def _root_of(app, path):
    p = Path(path)
    for label, root in app.ROOTS:
        if p.parent == root and root.is_dir():
            return label, root
    return None, None


# ------------------------------------------------------------------------------ suggestions ----
def suggest(app, path):
    """Possible AnimeThemes entries for one folder: Original Matching, then AniDB. Read-only."""
    label, root = _root_of(app, path)
    if not label or not Path(path).is_dir() or Path(path).is_symlink():
        raise ValueError('Not a folder inside a root folder.')
    name = Path(path).name
    yr = app.year(name)
    cands, notes, seen = [], [], set()
    try:
        media = app.anilist(name)
        if media:
            ay = media.get('seasonYear')
            if yr and ay and yr != ay:
                notes.append(f'Original Matching: AniList year {ay} differs from your folder ({yr}).')
            else:
                show = app.anime_theme(name, media)
                if show and not (yr and show.get('year') and int(show['year']) != yr):
                    seen.add(show.get('slug'))
                    cands.append({'slug': show.get('slug'), 'name': show.get('name') or show.get('slug'), 'year': show.get('year'),
                                  'format': show.get('media_format') or '', 'confidence': 'High', 'source': 'Original Matching',
                                  'why': 'Original Matching: exact title match' + (f' · same year {yr}' if yr else '')})
                else:
                    notes.append('Original Matching found no exact AnimeThemes entry.')
        else:
            notes.append('AniList did not identify this title.')
    except Exception as error:
        notes.append('Original Matching could not finish (' + type(error).__name__ + ').')
    try:
        import anidb
        if anidb.enabled(app.CONFIG) and anidb.status(app.CONFIG)['available']:
            results, more = anidb.identify(name, yr, label == 'Anime Movies', app.request_json, app.clean)
            notes += more
            for r in results:
                if r['slug'] not in seen:
                    seen.add(r['slug'])
                    cands.append({'slug': r['slug'], 'name': r['name'], 'year': r['year'], 'format': r['format'],
                                  'confidence': r['confidence'], 'source': 'AniDB', 'why': r['why']})
    except Exception as error:
        notes.append('AniDB identification could not finish (' + type(error).__name__ + ').')
    for c in cands:
        c['art'] = '/anime-art?' + urllib.parse.urlencode({'slug': c['slug']})
        c['type'] = 'Anime Movie' if str(c['format']).upper() == 'MOVIE' else ('Anime TV' if c['format'] else '')
    return {'path': path, 'candidates': cands[:4], 'notes': notes[:6]}


# ---------------------------------------------------------------------------- Match / Import ----
def match(app, path, slug):
    """Register the folder (if needed) and remember its AnimeThemes entry. Never installs, never touches disk."""
    import media_servers as v4
    label, root = _root_of(app, path)
    folder = Path(path)
    if not label or not folder.is_dir() or folder.is_symlink():
        raise ValueError('Not a folder inside a root folder.')
    if not v4._SLUG.fullmatch(slug or ''):
        raise ValueError('Invalid AnimeThemes entry.')
    url = 'https://api.animethemes.moe/anime/' + urllib.parse.quote(slug, safe='') + '?' + urllib.parse.urlencode({'include': v4._DETAIL})
    item = app.request_json(url, fresh=True).get('anime')
    if not isinstance(item, dict) or item.get('slug') != slug:
        raise ValueError('AnimeThemes could not confirm that entry; nothing was saved.')
    with app.db() as c:
        known = c.execute('SELECT 1 FROM shows WHERE path=?', (str(folder),)).fetchone()
    imported = False
    if not known:  # Import = register the existing folder in Themearr's library (records only)
        theme = folder / 'theme.mp3'
        state = 'installed' if theme.is_file() and theme.stat().st_size > 0 else 'missing'
        app.save(folder, label, folder.name, state, 'Existing theme' if state == 'installed' else '')
        imported = True
    with v4._LOCK:
        data = v4._read(app.CONFIG, 'matches')
        data[str(folder)] = {'slug': slug, 'name': str(item.get('name') or slug)[:200], 'year': str(item.get('year') or '')[:10]}
        v4._write(app.CONFIG, 'matches', data)
    app.event(folder.name, 'matched', ('Imported and matched' if imported else 'Matched') + ' to AnimeThemes ' + str(item.get('name') or slug)
              + (f" ({item.get('year')})" if item.get('year') else ''))
    return str(item.get('name') or slug), imported


# ---------------------------------------------------------------------------------------- UI ----
def page(app, handler):
    import media_servers
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    raw_root = q.get('root', [''])[0]
    notice = q.get('notice', [''])[0][:200]
    roots = [(label, root) for label, root in app.ROOTS]
    root = next((r for _, r in roots if str(r) == raw_root), roots[0][1] if roots else None)
    try:
        page_no = max(1, int(q.get('page', ['1'])[0]))
    except ValueError:
        page_no = 1
    items = unmapped(app, root) if root else []
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page_no = min(page_no, pages)
    shown = items[(page_no - 1) * PAGE_SIZE: page_no * PAGE_SIZE]
    import root_folders as rf
    root_name = rf._NAMES.get(str(root), '') if root else ''
    parts = [media_servers.settings_shell('Unmatched Folders', 'media'),
             '</aside><main class="main"><div class="topline"><div class="crumb">Settings / <a href="/media-management">Media Management</a> / <strong>Unmatched Folders</strong></div></div>',
             '<div class="heading-row"><div><h1 class="heading">Unmatched Folders</h1>',
             '<p class="muted">Folders whose anime Preludarr hasn’t confirmed yet (no theme.mp3 and no confirmed match). '
             'Pick the right anime and click <strong>Match / Import</strong>: Preludarr remembers it for all future scans and automatic installs. '
             'Nothing is installed, and nothing on disk is moved, renamed or deleted.</p></div></div>']
    if notice:
        parts += ['<p class="notice" role="status">', _e(notice), '</p>']
    parts += ['<form method="get" action="/media-management/unmapped" class="um-filter"><label>Root folder <select name="root">'] + \
             ['<option value="' + _e(r) + '"' + (' selected' if r == root else '') + '>' + _e(rf._NAMES.get(str(r), l)) + ' — ' + _e(r) + '</option>' for l, r in roots] + \
             ['</select></label><button type="submit">Show</button></form>',
              '<p class="muted">', str(len(items)), ' unmatched folder', '' if len(items) == 1 else 's', ' in ', _e(root_name or root),
              ' · page ', str(page_no), ' of ', str(pages), '</p><div class="um-list">']
    back = urllib.parse.urlencode({'root': str(root), 'page': page_no})
    for f in shown:
        status = 'Not scanned yet' if not f['registered'] else {'review': 'Needs review', 'missing': 'No theme yet'}.get(f['state'], f['state'] or 'No theme yet')
        parts += ['<section class="um-row" data-path="', _e(f['path']), '"><div class="um-head"><div><h3>', _e(f['name']), '</h3>',
                  '<code>', _e(f['path']), '</code> <span class="um-status">', _e(status), '</span></div>',
                  '<a class="btn ghost" href="', _e('/themes?' + urllib.parse.urlencode({'path': f['path']})) if f['registered'] else '#', '"',
                  '' if f['registered'] else ' aria-disabled="true" title="Match / Import it first, or scan the root folder"', '>Search manually</a></div>',
                  '<div class="um-cands" data-back="', _e(back), '"><p class="muted">Waiting to look up suggestions…</p></div></section>']
    if not shown:
        parts.append('<p>Every folder in this root folder is mapped. 🎉</p>')
    parts.append('</div>')
    if pages > 1:
        nav = ['<a class="btn ghost" href="' + _e('/media-management/unmapped?' + urllib.parse.urlencode({'root': str(root), 'page': n})) + '">' + label + '</a>'
               for label, n in (('« First', 1), ('‹ Previous', page_no - 1), ('Next ›', page_no + 1), ('Last »', pages)) if 1 <= n <= pages and n != page_no]
        parts += ['<p class="um-pager">', ' '.join(nav), '</p>']
    parts += [app.poster_manual.PICKER_ASSETS, '<style>',
              '.um-filter{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 6px}.um-filter select{padding:9px;background:#101c30;color:#fff;border:1px solid #647da0;border-radius:7px;max-width:100%}',
              '.um-list{display:flex;flex-direction:column;gap:12px}.um-row{border:1px solid var(--line,#3b4b6b);border-radius:11px;padding:14px;background:var(--card,#202b44)}',
              '.um-head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start}.um-head h3{margin:0 0 4px;overflow-wrap:anywhere}',
              '.um-head code{color:#9fd0ff;font-size:.82rem;overflow-wrap:anywhere}.um-status{display:inline-block;margin-left:6px;font-size:.78rem;padding:2px 8px;border-radius:10px;background:#33415e}',
              '.um-cands{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px;margin-top:12px}.um-cands>p{grid-column:1/-1;margin:0}',
              '.um-card{display:flex;gap:12px;align-items:flex-start;border:1px solid #3b4b6b;border-radius:9px;padding:10px;background:#18233a}',
              '.um-card .sm-art,.um-card .sm-art img,.um-card .sm-art .tp-noart{width:96px;height:144px}',
              '.um-card h4{margin:0 0 4px;font-size:.95rem;overflow-wrap:anywhere}.um-card p{margin:3px 0;font-size:.82rem}.um-card form{margin:8px 0 0}',
              '.um-pager{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}</style>',
              '<script>(function(){"use strict";', r'''
const rows=[...document.querySelectorAll(".um-row")];
const el=(t,a,k)=>{const n=document.createElement(t);for(const[x,y]of Object.entries(a||{}))if(x==="text")n.textContent=y;else n.setAttribute(x,y);for(const c of[].concat(k||[]))if(c)n.append(c);return n};
const colors={High:"#3f8f5f",Medium:"#b08a2e",Low:"#7a5252"};
async function load(row){const box=row.querySelector(".um-cands");box.replaceChildren(el("p",{class:"muted",text:"Looking up suggestions…"}));
 try{const r=await fetch("/media-management/suggest?"+new URLSearchParams({path:row.dataset.path}),{credentials:"same-origin",cache:"no-store"});
  const d=await r.json();if(!r.ok)throw new Error(d.error||("HTTP "+r.status));box.replaceChildren();
  if(!d.candidates.length)box.append(el("p",{class:"muted",text:"No suggestion found. Use Search manually to look it up by another title."}));
  for(const c of d.candidates){
   const preview=JSON.stringify({img:c.art,title:c.name,alts:[],year:String(c.year||""),type:c.type||"not stated",match:c.confidence+" confidence · "+c.why});
   const art=el("button",{type:"button",class:"sm-art","data-art-preview":preview,"aria-label":"Preview "+c.name},el("img",{loading:"lazy",alt:"",src:c.art,"data-noart":"No artwork"}));
   const form=el("form",{method:"post",action:"/media-management/match"},[el("input",{type:"hidden",name:"path",value:row.dataset.path}),
     el("input",{type:"hidden",name:"slug",value:c.slug}),el("input",{type:"hidden",name:"back",value:box.dataset.back}),el("button",{type:"submit",text:"Match / Import"})]);
   const conf=el("strong",{text:c.confidence+" confidence"});conf.style.color=colors[c.confidence]||"#ddd";
   box.append(el("div",{class:"um-card"},[art,el("div",{},[el("h4",{text:c.name}),el("p",{text:[c.year,c.type].filter(Boolean).join(" · ")||"year unknown"}),
     el("p",{},[conf,document.createTextNode(" · "+c.source)]),el("p",{class:"muted",text:c.why}),
     el("button",{type:"button",class:"btn ghost","data-art-preview":preview,text:"Preview"}),form])]));}
  if(d.notes.length){const det=el("details",{},[el("summary",{class:"muted",text:"Details ("+d.notes.length+")"}),el("ul",{},d.notes.map(n=>el("li",{class:"muted",text:n})))]);det.style.gridColumn="1/-1";box.append(det);}
 }catch(e){box.replaceChildren(el("p",{class:"muted",text:"Suggestions could not load ("+e.message+")."}))}}
(async()=>{for(const row of rows)await load(row)})();  // one folder at a time: gentle on AniList / AnimeThemes
''', '})();</script></main></div></body></html>']
    return handler.send(200, ''.join(parts), 'text/html')


def post_match(app, handler, redirect):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 4096:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), max_num_fields=5)
        if any(len(v) != 1 for v in form.values()):
            raise ValueError('Invalid form.')
        f = lambda k: form.get(k, [''])[0]  # noqa: E731
        back = {k: v[0] for k, v in urllib.parse.parse_qs(f('back')).items() if k in ('root', 'page') and v}
        name, imported = match(app, f('path'), f('slug'))
        msg = ('Imported and matched ' if imported else 'Matched ') + Path(f('path')).name + ' → ' + name + '. It will be used for future scans and installs.'
    except (ValueError, OSError, UnicodeError) as error:
        back = {}
        msg = 'Not matched: ' + (str(error) if isinstance(error, ValueError) else 'AnimeThemes could not be reached; try again.')
    return redirect(handler, '/media-management/unmapped?' + urllib.parse.urlencode({**back, 'notice': msg[:190]}))
