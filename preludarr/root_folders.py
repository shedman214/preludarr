# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → Media Management → Root Folders (Themearr V1.7.2, Sonarr/Radarr style).

A root folder is a PARENT folder; every direct sub-folder of it is one series or movie. Preludarr 1.11: the root's type
(Anime / Anime Movies / Shows / Movies, see kinds) decides which theme sources are used. Themearr's
existing code already works on `app.ROOTS` = [(library type, Path)] and only ever touches direct children of
a registered root, so this module just manages that list (updated in place) plus a small JSON file.

Safety: registering, editing or removing a root never creates, renames, moves or deletes anything on disk.
Browsing and new roots are limited to the library volumes Docker mounts into Themearr (TV_ROOT/MOVIE_ROOT,
or THEMEARR_LIBRARY_MOUNTS); hidden folders and symlinks are not offered; roots cannot be nested.
"""
import html
import json
import os
import threading
import urllib.parse
from pathlib import Path

FILE = 'themearr_root_folders.json'
from kinds import TYPES, label as type_label, is_regular  # Preludarr 1.11: four types
MAX_ROOTS = 20
MAX_LIST = 300
_LOCK = threading.Lock()
_NAMES = {}  # path -> display name (kept beside app.ROOTS, which holds (type, Path))


def _e(value):
    return html.escape(str(value), quote=True)


def mounts():
    """Library volumes Preludarr may browse: THEMEARR_LIBRARY_MOUNTS (or PRELUDARR_LIBRARY_MOUNTS), else /media plus the original TV/Movie roots."""
    raw = os.getenv('THEMEARR_LIBRARY_MOUNTS', '')
    items = [x.strip() for x in raw.split(',') if x.strip()] or ['/media', os.getenv('TV_ROOT', '/anime'), os.getenv('MOVIE_ROOT', '/movies')]
    out = []
    for item in items:
        p = Path(item)
        if p.is_absolute() and p not in out:
            out.append(p)
    return out


def _inside_mounts(p):
    return any(p == m or m in p.parents for m in mounts())


def _clean(raw):
    """Absolute, normalised, symlink-free path inside a mounted library volume, or ValueError."""
    text = str(raw or '').strip()
    if not text.startswith('/') or '\x00' in text or len(text) > 1024:
        raise ValueError('Choose a folder with the folder picker.')
    p = Path(os.path.normpath(text))
    if any(part.startswith('.') for part in p.parts[1:]):
        raise ValueError('Hidden folders cannot be used.')
    if not _inside_mounts(p):
        raise ValueError('That folder is not inside a library volume mounted into Preludarr.')
    if os.path.realpath(p) != str(p):
        raise ValueError('Folders reached through a symlink cannot be used.')
    if not p.is_dir():
        raise ValueError('That folder does not exist.')
    return p


# ------------------------------------------------------------------------------ storage ----
def _file(app):
    return Path(app.CONFIG) / FILE


def _save(app, entries):
    tmp = _file(app).with_name('.' + FILE + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'version': 1, 'roots': entries}, f, indent=1)
    os.replace(tmp, _file(app))


def _entries(app):
    return [{'path': str(root), 'type': label, 'name': _NAMES.get(str(root), label)} for label, root in app.ROOTS]


def _apply(app, entries):
    app.ROOTS[:] = [(e['type'], Path(e['path'])) for e in entries]  # in place: every module sees the change
    _NAMES.clear()
    _NAMES.update({e['path']: e['name'] for e in entries})


def load(app):
    """Startup: use the saved list. A new install seeds it with the TV_ROOT/MOVIE_ROOT folders that exist, else starts empty
    (the user adds root folders in Settings); an empty saved list stays empty."""
    with _LOCK:
        try:
            data = json.loads(_file(app).read_text())
            entries = []
            for e in data.get('roots', [])[:MAX_ROOTS]:
                if e.get('type') in TYPES and str(e.get('path', '')).startswith('/'):
                    entries.append({'path': os.path.normpath(e['path']), 'type': e['type'], 'name': str(e.get('name') or e['type'])[:60]})
        except FileNotFoundError:
            entries = [{'path': str(root), 'type': label, 'name': label} for label, root in app.ROOTS
                       if root.is_dir() and not root.is_symlink()]
            _save(app, entries)
        except (OSError, ValueError):
            print('Root folders file unreadable; keeping the Docker roots', flush=True)
            return
        _apply(app, entries)


# ------------------------------------------------------------------------------- scope ----
# Preludarr 1.11.9 (user 2026-10-29: a Library tab per root folder, named by its display name). A library filter value is
# 'all', a type ('Shows' …, older links) or 'root:<registered root path>' (one root folder's titles).
ROOT_PREFIX = 'root:'


def root_value(path):
    return ROOT_PREFIX + str(path)


def root_of(value):
    """The registered root path named by a 'root:…' value, else ''."""
    if isinstance(value, str) and value.startswith(ROOT_PREFIX):
        p = os.path.normpath(value[len(ROOT_PREFIX):])
        if p in _NAMES:
            return p
    return ''


def valid_scope(value):
    return value == 'all' or value in TYPES or bool(root_of(value))


def scope_clause(value):
    """(sql, params) limiting `shows` to the scope, or ('', []) for everything."""
    root = root_of(value)
    if root:
        return "path LIKE ? ESCAPE '\\'", [root.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '/%']
    if value in TYPES:
        return 'library=?', [value]
    return '', []


def name_of(path):
    return _NAMES.get(str(path), Path(str(path)).name)


# ------------------------------------------------------------------------------- stats ----
def stats(app, root):
    root = Path(root)
    info = {'exists': root.is_dir() and not root.is_symlink(), 'readable': False, 'writable': False,
            'free': None, 'folders': 0, 'in_library': 0, 'unmapped': [], 'themes': 0}
    if not info['exists']:
        return info
    info['readable'] = os.access(root, os.R_OK | os.X_OK)
    info['writable'] = os.access(root, os.W_OK)
    try:
        st = os.statvfs(root)
        info['free'] = st.f_bavail * st.f_frsize
    except OSError:
        pass
    on_disk = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith(('.', '!')):
                    continue
                try:
                    if entry.is_dir():  # same rule as the library scan
                        on_disk.append(entry.path)
                except OSError:
                    continue
    except OSError:
        info['readable'] = False
        return info
    info['folders'] = len(on_disk)
    with app.db() as c:
        rows = c.execute("SELECT path,state FROM shows WHERE path LIKE ? ESCAPE '\\'",
                         (str(root).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '/%',)).fetchall()
    known = {r['path']: r['state'] for r in rows if Path(r['path']).parent == root}
    info['in_library'] = sum(1 for p in on_disk if p in known)
    info['themes'] = sum(1 for p in on_disk if known.get(p) == 'installed')
    info['unmapped'] = sorted((Path(p).name for p in on_disk if p not in known), key=str.casefold)
    return info


def _size(n):
    if n is None:
        return 'unknown'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        if n < 1024 or unit == 'PiB':
            return (f'{n:.1f} {unit}' if unit != 'B' else f'{n} B')
        n /= 1024


# ------------------------------------------------------------------------------ browse ----
def browse(raw):
    """Sub-folders of a mounted library folder for the picker (JSON-ready)."""
    if not raw:
        return {'path': '', 'parent': '', 'dirs': [{'name': str(m), 'path': str(m)} for m in mounts() if m.is_dir()],
                'note': 'Library volumes mounted into Preludarr'}
    p = _clean(raw)
    dirs, more = [], False
    with os.scandir(p) as it:
        for entry in sorted(it, key=lambda e: e.name.casefold()):
            if entry.name.startswith('.') or entry.is_symlink():
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if len(dirs) >= MAX_LIST:
                more = True
                break
            dirs.append({'name': entry.name, 'path': entry.path})
    parent = '' if p in mounts() else str(p.parent)
    return {'path': str(p), 'parent': parent, 'dirs': dirs, 'note': ('Showing the first %d folders' % MAX_LIST) if more else ''}


# ----------------------------------------------------------------------------- changes ----
def _same_place(new, other):
    """Preludarr 1.11.1: the same disk folder can be reachable through two volumes (e.g. /media/tv/anime and /anime).
    Returns an error template when `new` is, contains, or is inside the folder of root `other`; else ''."""
    try:
        if not other.is_dir():
            return ''
        if os.path.samefile(new, other):
            return 'That folder is already a root folder (%s, "%s"), reached through another path.'
        for parent in new.parents:
            if parent.is_dir() and os.path.samefile(parent, other):
                return 'That folder is inside the root folder %s ("%s"), reached through another path.'
        for parent in other.parents:
            if parent.is_dir() and os.path.samefile(parent, new):
                return 'That folder contains the root folder %s ("%s"), reached through another path.'
        # the other root may sit inside `new` through a different volume (e.g. /media/tv contains /anime = …/anime):
        # look two levels down, where root folders live (bounded; only run when a root folder is added)
        target = os.stat(other)
        key = (target.st_dev, target.st_ino)
        seen, level = 0, [new]
        for _ in range(2):
            nxt = []
            for folder in level:
                with os.scandir(folder) as it:
                    for entry in it:
                        seen += 1
                        if seen > 20000:
                            return ''
                        if entry.name.startswith('.') or not entry.is_dir(follow_symlinks=False):
                            continue
                        st = entry.stat(follow_symlinks=False)
                        if (st.st_dev, st.st_ino) == key:
                            return 'That folder contains the root folder %s ("%s"), reached through another path.'
                        nxt.append(entry.path)
            level = nxt
    except OSError:
        return ''
    return ''


def add(app, raw, name, kind):
    if kind not in TYPES:
        raise ValueError('Choose a type: Anime, Anime Movies, Shows or Movies.')
    p = _clean(raw)
    with _LOCK:
        entries = _entries(app)
        if len(entries) >= MAX_ROOTS:
            raise ValueError('Up to %d root folders are supported.' % MAX_ROOTS)
        for e in entries:
            other = Path(e['path'])
            if p == other:
                raise ValueError('That folder is already a root folder.')
            if other in p.parents or p in other.parents:
                raise ValueError('Root folders cannot be inside each other (conflicts with %s).' % other)
            same = _same_place(p, other)
            if same:
                raise ValueError(same % (other, e['name']))
        entries.append({'path': str(p), 'type': kind, 'name': (name or '').strip()[:60] or p.name})
        _save(app, entries)
        _apply(app, entries)
    return p


def edit(app, raw, name, kind, new_path=''):
    """Rename / retype a root; Preludarr 1.11.2: optionally move it to a new folder and carry all saved data along."""
    if kind not in TYPES:
        raise ValueError('Choose a type: Anime, Anime Movies, Shows or Movies.')
    target = os.path.normpath(str(raw or ''))
    moved = None
    with _LOCK:
        entries = _entries(app)
        match = [e for e in entries if e['path'] == target]
        if not match:
            raise ValueError('Unknown root folder.')
        old_type = match[0]['type']
        if new_path and os.path.normpath(str(new_path)) != target:
            p = _clean(new_path)
            for e in entries:
                if e is match[0]:
                    continue
                other = Path(e['path'])
                if p == other or other in p.parents or p in other.parents:
                    raise ValueError('That folder overlaps the root folder %s.' % other)
                same = _same_place(p, other)
                if same:
                    raise ValueError(same % (other, e['name']))
            match[0]['path'] = str(p)
            moved = str(p)
        match[0]['name'] = (name or '').strip()[:60] or Path(match[0]['path']).name
        match[0]['type'] = kind
        _save(app, entries)
        _apply(app, entries)  # the scan stops using the old path at once
    if moved:
        counts = __import__('path_moves').remap(app, target, moved)
        target = moved
        if old_type != kind:
            with app.db() as c:
                for (path,) in c.execute('SELECT path FROM shows WHERE library=?', (old_type,)).fetchall():
                    if str(Path(path).parent) == target:
                        c.execute('UPDATE shows SET library=? WHERE path=?', (kind, path))
        return counts
    if old_type != kind:  # keep each title's library label in step with its root (records only)
        with app.db() as c:
            for (path,) in c.execute('SELECT path FROM shows WHERE library=?', (old_type,)).fetchall():
                if str(Path(path).parent) == target:
                    c.execute('UPDATE shows SET library=? WHERE path=?', (kind, path))


def remove(app, raw):
    """Unregister a root. Its titles leave Preludarr's library list; nothing on disk is touched.
    Preludarr 1.11.2: allowed while a scan runs — the scan skips folders of roots that are no longer registered."""
    target = os.path.normpath(str(raw or ''))
    with _LOCK:
        entries = _entries(app)
        keep = [e for e in entries if e['path'] != target]
        if len(keep) == len(entries):
            raise ValueError('Unknown root folder.')
        _save(app, keep)
        _apply(app, keep)
    removed = 0
    with app.db() as c:
        for (path,) in c.execute('SELECT path FROM shows').fetchall():
            if str(Path(path).parent) == target:
                c.execute('DELETE FROM shows WHERE path=?', (path,))
                removed += 1
    return removed


# ---------------------------------------------------------------------------------- UI ----
PICKER = r'''<dialog id="rf-picker" aria-labelledby="rf-picker-title" style="width:min(720px,96vw);max-height:90vh;overflow:auto;padding:0;border:1px solid #4a6181;border-radius:12px;background:#111b2d;color:#eaf2ff">
<div style="position:sticky;top:0;display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 16px;background:#14233e;border-bottom:1px solid #324158">
<h2 id="rf-picker-title" style="margin:0;font-size:1.05rem">Add Root Folder</h2><button type="button" class="btn ghost" data-rf-close aria-label="Close">✕</button></div>
<div style="padding:16px">
<p class="muted" style="margin-top:0">Pick the <strong>parent</strong> folder that contains your title folders (one sub-folder per series or movie). Only library volumes mounted into Preludarr are shown.</p>
<p><strong>Folder:</strong> <code id="rf-current">—</code></p>
<div id="rf-list" role="listbox" aria-label="Folders" style="display:flex;flex-direction:column;gap:4px;max-height:45vh;overflow:auto;border:1px solid #324158;border-radius:8px;padding:6px"></div>
<p id="rf-note" class="muted"></p>
<form method="post" action="/media-management/add" id="rf-add">
<input type="hidden" name="path" id="rf-path">
<label class="field"><span>Display name</span><input name="name" maxlength="60" placeholder="e.g. Kids TV"></label>
<label class="field"><span>Type</span><select name="type"><option value="Anime TV">Anime</option><option value="Anime Movies">Anime Movies</option><option value="Shows">Shows</option><option value="Movies">Movies</option></select></label><p class="muted" style="margin:-4px 0 10px;font-size:.9em">The type decides where Preludarr looks for themes: Anime and Anime Movies use AnimeThemes; Shows use Plex TV Themes and ThemerrDB; Movies use ThemerrDB. Shows and Movies can also use YouTube search (preview only) if you turn it on.</p>
<div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end"><button type="button" class="btn ghost" data-rf-close>Cancel</button>
<button type="submit" class="primary" id="rf-ok" disabled>Add this folder</button></div></form></div></dialog>
<script>(function(){"use strict";
const dlg=document.getElementById("rf-picker"),list=document.getElementById("rf-list"),cur=document.getElementById("rf-current"),
 note=document.getElementById("rf-note"),hidden=document.getElementById("rf-path"),ok=document.getElementById("rf-ok");
function item(label,path,cls){const b=document.createElement("button");b.type="button";b.className="btn ghost";b.style.textAlign="left";b.textContent=label;b.addEventListener("click",()=>go(path));return b}
async function go(path){list.textContent="Loading…";note.textContent="";
 try{const r=await fetch("/media-management/browse?"+new URLSearchParams({path}),{credentials:"same-origin",cache:"no-store"});
  const d=await r.json();if(!r.ok)throw new Error(d.error||("HTTP "+r.status));
  list.replaceChildren();cur.textContent=d.path||"(choose a library volume)";hidden.value=d.path;ok.disabled=!d.path;
  if(d.path)list.append(item("⬆ Up one level",d.parent,""));
  for(const x of d.dirs)list.append(item("📁 "+x.name,x.path));
  if(!d.dirs.length)list.append(Object.assign(document.createElement("p"),{className:"muted",textContent:"No sub-folders here."}));
  note.textContent=d.note||"";}catch(e){list.textContent="Could not open this folder: "+e.message}}
document.querySelectorAll("[data-rf-open]").forEach(b=>b.addEventListener("click",()=>{dlg.showModal();go("")}));
dlg.querySelectorAll("[data-rf-close]").forEach(b=>b.addEventListener("click",()=>dlg.close()));
})();</script>'''


def page(app, handler):
    import media_servers
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    notice = q.get('notice', [''])[0][:200]
    parts = [media_servers.settings_shell('Media Management', 'media'),
             '</aside><main class="main"><div class="topline"><div class="crumb">Settings / <strong>Media Management</strong></div></div>',
             '<div class="heading-row"><div><h1 class="heading">Media Management</h1>',
             '<p class="muted">Root folders are the parent folders Preludarr scans. Every sub-folder of a root folder is one series or movie, and the folder&#8217;s <strong>type</strong> decides where Preludarr looks for its themes. '
             'Adding, editing or removing a root folder never moves, renames or deletes anything on disk.</p></div>',
             '<div class="rf-head-actions"><a class="btn" href="/system?tab=health">Library Health</a>'
             '<button type="button" class="btn primary" data-rf-open>＋ Add Root Folder</button></div></div>',
             '<style>.rf-head-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}</style>']
    if notice:
        parts += ['<p class="notice" role="status">', _e(notice), '</p>']
    if app.STATUS.get('running'):  # Preludarr 1.11.2: say what is running instead of silently refusing changes
        parts += ['<p class="notice" role="status" id="rf-job">⏳ ', _e(app.STATUS.get('message') or 'Scanning'),
                  ' — you can still add, edit or remove root folders; this page refreshes when it finishes.</p>',
                  '<script>(function(){let t=setInterval(async()=>{try{const r=await fetch("/api/status",{cache:"no-store",credentials:"same-origin"});',
                  'if(!r.ok)return;const s=await r.json();const el=document.getElementById("rf-job");if(el&&s.display)el.firstChild&&(el.childNodes[0].nodeValue="⏳ "+s.display);',
                  'if(!s.running){clearInterval(t);location.reload()}}catch(e){}},3000)})();</script>']
    try:
        offers = __import__('path_moves').orphans(app)
    except Exception:
        offers = []
    for o in offers:  # saved data left behind under a folder that no longer exists
        parts += ['<section class="panel rf-reconnect"><h2>Saved data from an old location</h2>',
                  '<p><strong>', f"{o['titles']:,}", '</strong> titles still have saved data (posters, ids, matches, dates, media-server mappings) under ',
                  '<code>', _e(o['old']), '</code>, which no longer exists. ', f"{o['matches']:,}", ' of those folder names are in <strong>', _e(o['name']),
                  '</strong> (<code>', _e(o['root']), '</code>). It looks like that folder moved.</p>',
                  '<form method="post" action="/media-management/reconnect"><input type="hidden" name="old" value="', _e(o['old']), '">',
                  '<input type="hidden" name="root" value="', _e(o['root']), '">',
                  '<label class="check"><input type="checkbox" name="confirm" value="1" required> Move the saved data from ', _e(o['old']), ' to ', _e(o['root']), '</label> ',
                  '<button type="submit">Reconnect</button></form>',
                  '<p class="muted">Only Preludarr’s own records change — nothing on disk is touched. Titles keep their current installed / missing state.</p></section>']
    parts += ['<section class="panel"><h2>Root Folders</h2><div class="rf-table" role="table" aria-label="Root folders">',
              '<div class="rf-row rf-head" role="row"><span role="columnheader">Path</span><span role="columnheader">Type</span>'
              '<span role="columnheader">Free Space</span><span role="columnheader">Title folders</span>'
              '<span role="columnheader" title="Title folders with no theme.mp3 yet">Unmatched</span><span role="columnheader"></span></div>']
    for label, root in list(app.ROOTS):
        st = stats(app, root)
        name = _NAMES.get(str(root), label)
        health = ('OK' if st['readable'] and st['writable'] else 'read-only' if st['readable'] else 'not accessible') if st['exists'] else 'missing'
        parts += ['<div class="rf-row" role="row">',
                  '<span role="cell"><strong>', _e(name), '</strong><br><code>', _e(root), '</code>',
                  '' if health == 'OK' else '<br><small class="rf-warn">⚠ ' + _e(health) + '</small>', '</span>',
                  '<span role="cell">', _e(type_label(label)), '</span>',
                  '<span role="cell">', _e(_size(st['free'])), '</span>',
                  '<span role="cell">', str(st['folders']), '<br><small class="muted">', str(st['in_library']), ' in library · ', str(st['themes']), ' with theme</small></span>',
                  '<span role="cell">']
        if is_regular(label):  # Shows / Movies: no AnimeThemes matching — missing titles get suggestions on their title page
            with app.db() as c:
                miss = [r[0] for r in c.execute("SELECT path FROM shows WHERE library=? AND state IN ('missing','review')", (label,)) if Path(r[0]).parent == Path(root)]
            parts += [('<a href="' + _e('/?' + urllib.parse.urlencode({'view': 'library', 'library': label, 'state': 'missing'})) + '" title="No theme.mp3 yet — open a title to see suggestions"><strong>'
                       + str(len(miss)) + '</strong> · missing</a>') if miss else '0']
            if st['unmapped']:
                parts += ['<br><small class="muted">', str(len(st['unmapped'])), ' not scanned yet</small>']
            um = None
        else:
            um = __import__('unmapped').unmapped(app, root)
        if um is None:
            pass
        elif um:
            parts += ['<a href="', _e('/media-management/unmapped?' + urllib.parse.urlencode({'root': str(root)})), '" title="No theme.mp3 and no confirmed AnimeThemes entry — review and match"><strong>', str(len(um)), '</strong> · review</a>']
            if st['unmapped']:
                parts += ['<br><small class="muted">', str(len(st['unmapped'])), ' not scanned yet</small>']
        else:
            parts.append('0')
        parts += ['</span><span role="cell" class="rf-actions">',
                  '<form method="post" action="/media-management/scan"><input type="hidden" name="path" value="', _e(root), '">',
                  '<button type="submit"', ' disabled' if app.STATUS.get('running') else '', '>Scan</button></form>',
                  '<details><summary class="btn ghost">Edit</summary><form method="post" action="/media-management/edit" class="rf-edit">',
                  '<input type="hidden" name="path" value="', _e(root), '">',
                  '<label class="field"><span>Display name</span><input name="name" maxlength="60" value="', _e(name), '"></label>',
                  '<label class="field"><span>Folder</span><input name="new_path" maxlength="1024" value="', _e(root), '" spellcheck="false"></label>',
                  '<p class="muted rf-hint">Moved this folder? Enter its new location — posters, matches, ids, dates and mappings move with it.</p>',
                  '<label class="field"><span>Type</span><select name="type">'] + \
                 ['<option value="' + t + '"' + (' selected' if t == label else '') + '>' + type_label(t) + '</option>' for t in TYPES] + \
                 ['</select></label><button type="submit">Save</button></form></details>',
                  '<details><summary class="btn ghost" aria-label="Remove ', _e(name), '">✕</summary>',
                  '<form method="post" action="/media-management/remove" class="rf-edit"><p>', str(st['in_library']),
                  ' title(s) leave Preludarr’s library list. <strong>Folders and theme.mp3 files stay on disk.</strong></p>',
                  '<input type="hidden" name="path" value="', _e(root), '">',
                  '<label class="check"><input type="checkbox" name="confirm" value="1" required> I understand</label>',
                  '<button type="submit" style="background:#9b394a">Remove root folder</button></form></details>',
                  '</span></div>']
    if not app.ROOTS:
        parts.append('<p>No root folders yet. Use <strong>Add Root Folder</strong>.</p>')
    parts += ['</div><p class="muted rf-legend"><strong>Unmatched</strong> = title folders with no theme.mp3 yet. For Anime and Anime Movies, use <em>review</em> to match them '
              'with AnimeThemes; for Shows and Movies, open a title to see its theme suggestions. <strong>Not scanned yet</strong> = folders on disk Preludarr hasn’t added to its library — '
              'Library Health counts only these.</p>',
              '<p class="muted">Library volumes mounted into Preludarr: ', _e(', '.join(str(m) for m in mounts() if m.is_dir()) or 'none yet'),
              '. A folder elsewhere on the server first needs a Docker volume mapping (Unraid: Docker → Preludarr → Edit; Compose: <code>volumes:</code>).</p></section>',
              PICKER, __import__('auto_themes').settings_html(app, notice if notice.startswith(('Saved.', 'Not saved:', 'Automatic', 'Another scan')) and not notice.startswith('Permissions') else ''),
              __import__('permissions').settings_html(app, notice if notice.startswith('Permissions') else ''),
              '<style>.rf-table{display:flex;flex-direction:column;gap:6px}',
              '.rf-row{display:grid;grid-template-columns:minmax(0,2.4fr) minmax(0,1fr) minmax(0,1fr) minmax(0,1.2fr) minmax(0,1.2fr) 240px;gap:12px;align-items:start;padding:10px;border:1px solid var(--line,#3b4b6b);border-radius:9px}',
              '.rf-head{border:0;font-weight:700;color:var(--muted,#adbed9);padding-top:0;padding-bottom:0}',
              '.rf-row code{overflow-wrap:anywhere;color:#9fd0ff}.rf-warn{color:#ffb4b4}.rf-unmapped{max-height:220px;overflow:auto;margin:6px 0;padding-left:18px}',
              '.rf-actions details:last-of-type{margin-left:6px;padding-left:10px;border-left:1px solid #36486a}'
              '.rf-row>span{min-width:0;overflow-wrap:anywhere}.rf-actions{display:flex;gap:6px;flex-wrap:wrap;align-items:flex-start}.rf-actions form{margin:0}.rf-actions summary{list-style:none;cursor:pointer}',
              '.rf-edit{margin-top:8px;padding:10px;border:1px solid var(--line,#3b4b6b);border-radius:8px;min-width:230px}.rf-edit .rf-hint{font-size:.85em;margin:2px 0 8px}',
              '.rf-edit input{box-sizing:border-box;width:100%}.rf-reconnect{border-color:#8a6d1f!important}.rf-reconnect code{overflow-wrap:anywhere}',
              '.rf-reconnect form{display:flex;gap:10px;align-items:center;flex-wrap:wrap}',
              '#rf-picker .field{display:block;margin:10px 0}#rf-picker .field span{display:block;font-weight:600;margin-bottom:4px}',
              '#rf-picker input,#rf-picker select{width:100%;padding:10px;background:#101c30;color:#fff;border:1px solid #647da0;border-radius:7px;box-sizing:border-box}',
              '#rf-list .btn{justify-content:flex-start;text-align:left;width:100%}',
              '@media(max-width:860px){.rf-head{display:none}.rf-row{grid-template-columns:1fr}}</style>',
              '</main></div></body></html>']
    return handler.send(200, ''.join(parts), 'text/html')


def _summary(counts):
    labels = {'shows': 'titles', 'posters': 'posters', 'poster_images': 'poster images', 'media_ids': 'Sonarr/Radarr ids', 'show_added': 'added dates',
              'theme_queue': 'queue items', 'themearr_media_servers_v1.json': 'media-server mappings', 'themearr_v4_matches.json': 'remembered matches',
              'themearr_arr.json': 'Sonarr/Radarr path mappings', 'themearr_library_health.json': 'health notes', 'recycle_bin': 'recycle-bin items'}
    parts = [f'{v:,} {labels[k]}' for k, v in counts.items() if v and k in labels]
    return ', '.join(parts) or 'nothing needed moving'


def post(app, handler, redirect):
    route = urllib.parse.urlsplit(handler.path).path
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 4096:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=8)
        if any(len(v) != 1 for v in form.values()):
            raise ValueError('Invalid form.')
        f = lambda k: form.get(k, [''])[0]  # noqa: E731
        if route == '/media-management/add':
            p = add(app, f('path'), f('name'), f('type'))
            started = app.job(False, root=p)
            msg = 'Root folder added: %s. %s' % (p, 'Scanning it now.' if started else 'It will be scanned as soon as the current scan finishes.')
        elif route == '/media-management/edit':
            counts = edit(app, f('path'), f('name'), f('type'), f('new_path').strip())
            msg = ('Root folder moved; saved data carried over (' + _summary(counts) + ').') if counts else 'Root folder updated.'
        elif route == '/media-management/reconnect':
            if f('confirm') != '1':
                raise ValueError('Tick the confirmation box to reconnect the saved data.')
            old, root = os.path.normpath(f('old')), os.path.normpath(f('root'))
            offer = [o for o in __import__('path_moves').orphans(app) if o['old'] == old and o['root'] == root]
            if not offer:
                raise ValueError('That old location is no longer waiting to be reconnected.')
            counts = __import__('path_moves').remap(app, old, root)
            msg = 'Saved data from %s reconnected to %s (%s).' % (old, root, _summary(counts))
        elif route == '/media-management/remove':
            if f('confirm') != '1':
                raise ValueError('Tick “I understand” to remove the root folder.')
            n = remove(app, f('path'))
            msg = 'Root folder removed; %d title(s) left the library list. Nothing on disk was changed.' % n
        elif route == '/media-management/scan':
            target = os.path.normpath(f('path'))
            if not any(str(r) == target for _, r in app.ROOTS):
                raise ValueError('Unknown root folder.')
            msg = 'Scanning ' + target + '.' if app.job(False, root=Path(target)) else 'Queued: ' + target + ' will be scanned as soon as the current scan finishes.'
        else:
            return handler.send(404, 'Not found', 'text/plain')
    except (ValueError, OSError, UnicodeError) as error:
        msg = 'Not changed: ' + (str(error) if isinstance(error, ValueError) else 'the folder could not be read.')
    return redirect(handler, '/media-management?' + urllib.parse.urlencode({'notice': msg[:190]}))
