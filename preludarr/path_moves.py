# SPDX-License-Identifier: GPL-3.0-or-later
"""Move every saved per-title record from one root folder path to another (Preludarr 1.11.2).

User 2026-10-29: after moving the anime roots from /anime and /movies to /media/tv/anime and /media/movies/anime movies,
Preludarr re-found the titles but everything stored per folder path still pointed at the old paths (posters, Sonarr/Radarr ids,
media-server item mappings, remembered matches, "recently added" dates, the queue, Sonarr/Radarr path mappings…).

remap(app, old, new) rewrites the path prefix `old` -> `new` in:
  * database tables keyed by path: shows, posters, poster_attempts, show_added, media_ids, theme_suggestions,
    youtube_search_cache, themearr_poster_batch_paths_v143, and the path column of theme_queue;
  * poster images (/config/posters/<sha256(path)>.jpg are renamed);
  * JSON settings that store folder paths: remembered AnimeThemes matches, media-server mappings, Library Health,
    Sonarr/Radarr path mappings, and recycle-bin manifests (so Undo restores into the new location).
When a title already has a record at the NEW path (it was re-scanned there) the rules are, in either direction:
  shows / suggestions / YouTube cache / poster attempts -> keep the record at the new path; show_added -> the pre-existing
  (baseline) or earliest date; posters -> the record whose image exists; media_ids -> every known id is kept (merged).
Nothing on disk outside /config is touched. Every JSON file is rewritten atomically with 0600 permissions.
"""
import hashlib
import json
import os
import time
from pathlib import Path

TABLES_NEW_WINS = ('shows', 'theme_suggestions', 'youtube_search_cache', 'poster_attempts')
TABLES_OLD_WINS = ('show_added', 'media_ids', 'posters')
JSON_FILES = ('themearr_v4_matches.json', 'themearr_media_servers_v1.json', 'themearr_library_health.json', 'themearr_arr.json')


def _norm(p):
    return os.path.normpath(str(p))


def _swap(value, old, new):
    """`value` with the prefix `old` replaced, or None if it is not inside old."""
    if value == old:
        return new
    if isinstance(value, str) and value.startswith(old + '/'):
        return new + value[len(old):]
    return None


def _walk(obj, old, new, count):
    """Recursively replace path strings (keys and values) inside old -> new. Returns the new object."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nk = _swap(k, old, new)
            if nk is not None:
                count[0] += 1
            out[nk if nk is not None else k] = _walk(v, old, new, count)
        return out
    if isinstance(obj, list):
        return [_walk(v, old, new, count) for v in obj]
    nv = _swap(obj, old, new) if isinstance(obj, str) else None
    if nv is not None:
        count[0] += 1
        return nv
    return obj


def _write_json(path, data):
    tmp = path.with_name('.' + path.name + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _like(prefix):
    return prefix.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '/%'


def _moving_wins(app, c, table, src, dst):
    """1.11.3: when both locations have a record, keep the more original / complete one — the same in either direction.
    show_added: a pre-existing title (baseline) or the earliest date; posters: the one with an image; media_ids: fields merged."""
    a = c.execute(f'SELECT * FROM {table} WHERE path=?', (src,)).fetchone()
    b = c.execute(f'SELECT * FROM {table} WHERE path=?', (dst,)).fetchone()
    if table == 'show_added':
        if (a['baseline'] or 0) != (b['baseline'] or 0):
            return bool(a['baseline'])
        return (a['added'] or 0) < (b['added'] or 0) if (a['added'] and b['added']) else bool(a['added'] and not b['added'])
    if table == 'posters':
        a_img, b_img = app.poster_file(src).is_file(), app.poster_file(dst).is_file()
        if a_img != b_img:
            return a_img
        return a['status'] == 'ok' and b['status'] != 'ok'
    if table == 'media_ids':  # take every id either record knows; the moving record's values first
        merged = {k: (a[k] if a[k] not in (None, '', 0) else b[k]) for k in ('tvdb', 'tmdb', 'imdb', 'source')}
        c.execute('UPDATE media_ids SET tvdb=?,tmdb=?,imdb=?,source=? WHERE path=?', (merged['tvdb'], merged['tmdb'], merged['imdb'], merged['source'], dst))
        return False
    return True


def remap(app, old, new, apply=True):
    """Move saved records from root `old` to root `new`. Returns {area: count}. apply=False only counts."""
    old, new = _norm(old), _norm(new)
    if old == new or not old.startswith('/') or not new.startswith('/'):
        raise ValueError('Choose two different folders.')
    counts = {}
    cfg = Path(app.CONFIG)
    with app.db() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in TABLES_NEW_WINS + TABLES_OLD_WINS:
            if table not in tables:
                continue
            rows = [r[0] for r in c.execute(f"SELECT path FROM {table} WHERE path=? OR path LIKE ? ESCAPE '\\'", (old, _like(old)))]
            counts[table] = len(rows)
            if not apply:
                continue
            for p in rows:
                np = _swap(p, old, new)
                exists = c.execute(f'SELECT 1 FROM {table} WHERE path=?', (np,)).fetchone()
                if exists:
                    if table in TABLES_NEW_WINS or not _moving_wins(app, c, table, p, np):
                        c.execute(f'DELETE FROM {table} WHERE path=?', (p,))
                        continue
                    c.execute(f'DELETE FROM {table} WHERE path=?', (np,))
                c.execute(f'UPDATE {table} SET path=? WHERE path=?', (np, p))
        if 'theme_queue' in tables:
            q = c.execute("SELECT id,path FROM theme_queue WHERE path=? OR path LIKE ? ESCAPE '\\'", (old, _like(old))).fetchall()
            counts['theme_queue'] = len(q)
            if apply:
                for r in q:
                    c.execute('UPDATE theme_queue SET path=? WHERE id=?', (_swap(r['path'], old, new), r['id']))
        if 'themearr_poster_batch_paths_v143' in tables:
            b = c.execute("SELECT job,path FROM themearr_poster_batch_paths_v143 WHERE path LIKE ? ESCAPE '\\'", (_like(old),)).fetchall()
            counts['poster_batches'] = len(b)
            if apply:
                for r in b:
                    c.execute('UPDATE OR IGNORE themearr_poster_batch_paths_v143 SET path=? WHERE job=? AND path=?', (_swap(r['path'], old, new), r['job'], r['path']))
                    c.execute('DELETE FROM themearr_poster_batch_paths_v143 WHERE job=? AND path=?', (r['job'], r['path']))
        if apply:  # poster images are named after the folder path
            moved = 0
            for (p,) in c.execute("SELECT path FROM posters WHERE path LIKE ? ESCAPE '\\'", (_like(new),)).fetchall():
                op = _swap(p, new, old)
                src, dst = app.poster_file(op), app.poster_file(p)
                if src.is_file() and not dst.exists():
                    os.replace(src, dst)
                    moved += 1
            counts['poster_images'] = moved
    for name in JSON_FILES:
        path = cfg / name
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        n = [0]
        out = _walk(data, old, new, n)
        counts[name] = n[0]
        if apply and n[0]:
            _write_json(path, out)
    trash = cfg / 'theme-trash'
    if trash.is_dir() and not trash.is_symlink():
        t = 0
        for manifest in trash.glob('*/manifest.json'):
            try:
                data = json.loads(manifest.read_text())
            except (OSError, ValueError):
                continue
            n = [0]
            out = _walk(data, old, new, n)
            if n[0]:
                t += 1
                if apply:
                    _write_json(manifest, out)
        counts['recycle_bin'] = t
    if apply:
        try:
            app.event('Root folders', 'paths moved', f'Saved data moved from {old} to {new}: ' +
                      ', '.join(f'{k} {v}' for k, v in counts.items() if v))
        except Exception:
            pass
    return counts


# ------------------------------------------------------------------- old locations ----
def orphans(app, min_titles=5, min_share=0.5):
    """Saved data under folders that are no longer root folders, matched to a current root by its title folder names.
    [{'old', 'titles', 'root', 'type', 'name', 'matches'}] — read-only."""
    roots = [(label, Path(r)) for label, r in app.ROOTS]
    root_set = {str(r) for _, r in roots}
    with app.db() as c:
        paths = set()
        for table in ('posters', 'show_added', 'media_ids'):
            try:
                paths |= {r[0] for r in c.execute(f'SELECT path FROM {table}')}
            except Exception:
                continue
    groups = {}
    for p in paths:
        parent = str(Path(p).parent)
        if parent in root_set or any(parent.startswith(r + '/') for r in root_set):
            continue
        groups.setdefault(parent, set()).add(Path(p).name)
    out = []
    names_cache = {}
    for old, names in groups.items():
        if len(names) < min_titles or Path(old).is_dir():  # an existing folder is not "old"
            continue
        best = None
        for label, root in roots:
            if str(root) not in names_cache:
                try:
                    names_cache[str(root)] = {e.name for e in os.scandir(root) if e.is_dir()}
                except OSError:
                    names_cache[str(root)] = set()
            hit = len(names & names_cache[str(root)])
            if hit >= min_titles and hit / len(names) >= min_share and (not best or hit > best[0]):
                best = (hit, label, root)
        if best:
            name = __import__('root_folders')._NAMES.get(str(best[2]), best[1])
            out.append({'old': old, 'titles': len(names), 'root': str(best[2]), 'type': best[1], 'name': name, 'matches': best[0]})
    return sorted(out, key=lambda x: -x['titles'])
