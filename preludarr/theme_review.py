# SPDX-License-Identifier: GPL-3.0-or-later
"""Review extra theme files + Recycle bin (Themearr V1.9.4; called "theme trash" in code, "Recycle bin" in the UI since V1.9.5).

User request 2026-10-26: review BOTH theme files of a folder (play them), pick the one to keep, remove the rest.
Decisions: the kept file always ends up as the folder's theme.mp3 (renamed or converted); removed files go to a
recoverable Themearr trash (/config/theme-trash) with Undo; Season-folder theme.mp3 files are included; the trash
keeps files for N days (user setting) and can also be emptied on a cron schedule.

Safety: only registered title folders that are direct children of a root; symlinks are never offered or followed;
every removed file is copied into the trash and verified (SHA-256) BEFORE it is unlinked from the media folder;
publishing theme.mp3 is an atomic rename/replace inside the folder; MP3 conversion of a non-MP3 keeper goes through
the existing validated manual_install_mp3 pipeline. One theme.mp3 per folder is the end state.
"""
import datetime
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path, PurePosixPath

AUDIO_EXT = ('.mp3', '.ogg', '.flac', '.m4a', '.wav', '.opus', '.aac', '.wma', '.webm')
TYPES = {'.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.opus': 'audio/ogg', '.flac': 'audio/flac', '.m4a': 'audio/mp4',
         '.aac': 'audio/aac', '.wav': 'audio/wav', '.wma': 'audio/x-ms-wma', '.webm': 'audio/webm'}
TRASH = 'theme-trash'
SETTINGS = 'themearr_theme_trash.json'
DEFAULTS = {'days': 30, 'cron': ''}
MAX_FILE = 60 * 1024 * 1024
_LOCK = threading.Lock()
_PROBE = {}
_SCHED = {'started': False, 'last_cron_minute': None, 'last_purge': 0}


def _e(value):
    return html.escape(str(value), quote=True)


# ------------------------------------------------------------------------------ settings ----
def settings(config):
    data = dict(DEFAULTS)
    try:
        saved = json.loads((Path(config) / SETTINGS).read_text())
        if isinstance(saved.get('days'), int) and 1 <= saved['days'] <= 3650:
            data['days'] = saved['days']
        if isinstance(saved.get('cron'), str):
            parse_cron(saved['cron'])  # stored values were validated; re-check anyway
            data['cron'] = saved['cron'].strip()
    except (OSError, ValueError, TypeError):
        pass
    return data


def _write_json(path, data, mode=0o600):
    tmp = path.with_name('.' + path.name + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------- cron ----
_NAMES = {'sun': 0, 'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4, 'fri': 5, 'sat': 6,
          'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}
_FIELDS = (('minute', 0, 59), ('hour', 0, 23), ('day of month', 1, 31), ('month', 1, 12), ('day of week', 0, 7))


def _field(text, name, lo, hi):
    values = set()
    for part in text.split(','):
        step, stepped = 1, '/' in part
        if stepped:
            part, s = part.split('/', 1)
            if not s.isdigit() or int(s) < 1:
                raise ValueError(f'Cron: bad step in the {name} field.')
            step = int(s)
        if part == '*':
            a, b = lo, hi
        else:
            bits = part.split('-')
            if len(bits) > 2 or not all(bits):
                raise ValueError(f'Cron: “{part}” is not valid in the {name} field.')
            nums = []
            for bit in bits:
                bit = bit.lower()
                if bit.isdigit():
                    nums.append(int(bit))
                elif bit in _NAMES and name in ('month', 'day of week') and (bit[:3] in list(_NAMES)[:7]) == (name == 'day of week'):
                    nums.append(_NAMES[bit])
                else:
                    raise ValueError(f'Cron: “{bit}” is not valid in the {name} field.')
            a, b = nums[0], nums[-1]
            if len(bits) == 1 and stepped:  # "5/15" = from 5 to the end, every 15
                b = hi
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f'Cron: the {name} field must be between {lo} and {hi}.')
        values.update(range(a, b + 1, step))
    return values


def parse_cron(text):
    """Standard 5-field cron (minute hour day-of-month month day-of-week). '' = off. ValueError with a plain message."""
    text = (text or '').strip()
    if not text:
        return None
    parts = text.split()
    if len(parts) != 5:
        raise ValueError('Cron needs 5 fields: minute hour day-of-month month day-of-week (e.g. “0 3 * * 0” = Sundays 03:00).')
    sets = [_field(p, *f) for p, f in zip(parts, _FIELDS)]
    if 7 in sets[4]:
        sets[4].add(0); sets[4].discard(7)
    return {'min': sets[0], 'hour': sets[1], 'dom': sets[2], 'mon': sets[3], 'dow': sets[4],
            'dom_any': parts[2] == '*', 'dow_any': parts[4] == '*'}


def cron_matches(spec, dt):
    if not (dt.minute in spec['min'] and dt.hour in spec['hour'] and dt.month in spec['mon']):
        return False
    dom_ok, dow_ok = dt.day in spec['dom'], (dt.weekday() + 1) % 7 in spec['dow']
    if spec['dom_any'] or spec['dow_any']:
        return dom_ok and dow_ok
    return dom_ok or dow_ok  # classic cron: either day field may match when both are restricted


def next_run(spec, after=None):
    t = (after or datetime.datetime.now()).replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if t.month in spec['mon'] and t.hour in spec['hour'] and cron_matches(spec, t):
            return t
        t += datetime.timedelta(minutes=1)
    return None


# ------------------------------------------------------------------------- candidates ----
def _folder(app, raw):
    """Registered title folder that is a direct child of a root and not a symlink, or ValueError."""
    folder = Path(str(raw or ''))
    if not folder.is_absolute() or not any(folder.parent == root for _, root in app.ROOTS):
        raise ValueError('Unknown title folder.')
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('The title folder is missing or is a symlink.')
    with app.db() as c:
        if not c.execute('SELECT 1 FROM shows WHERE path=?', (str(folder),)).fetchone():
            raise ValueError('Unknown title folder.')
    return folder


def _is_theme_name(name):
    low = name.lower()
    return low.startswith('theme.') and low.endswith(AUDIO_EXT)


def _probe(path, st):
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _PROBE:
        return _PROBE[key]
    info = {'duration': None, 'title': '', 'artist': ''}
    try:
        out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:format_tags=title,artist', '-of', 'json', str(path)],
                             capture_output=True, timeout=10, check=False).stdout
        fmt = json.loads(out or b'{}').get('format') or {}
        tags = {k.lower(): v for k, v in (fmt.get('tags') or {}).items()}
        info = {'duration': float(fmt['duration']) if fmt.get('duration') else None, 'title': str(tags.get('title', ''))[:120],
                'artist': str(tags.get('artist', ''))[:120]}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    if len(_PROBE) > 500:
        _PROBE.clear()
    _PROBE[key] = info
    return info


def candidates(app, folder, probe=True):
    """Theme files in the title folder and in its direct sub-folders. Regular files only."""
    folder = _folder(app, folder)
    out = []

    def add(rel, path):
        try:
            st = os.lstat(path)
        except OSError:
            return
        if not stat.S_ISREG(st.st_mode) or st.st_size == 0 or st.st_size > MAX_FILE:
            return
        item = {'rel': rel, 'size': st.st_size, 'mtime': st.st_mtime, 'current': rel == 'theme.mp3', 'nested': '/' in rel,
                'ext': PurePosixPath(rel).suffix.lower()}
        if probe:
            item.update(_probe(path, st))
        out.append(item)

    with os.scandir(folder) as it:
        entries = sorted(it, key=lambda e: e.name)
    for e in entries:
        if e.name.startswith('.'):
            continue
        if _is_theme_name(e.name):
            add(e.name, e.path)
        elif e.is_dir(follow_symlinks=False):
            try:
                with os.scandir(e.path) as sub:
                    for s in sorted(sub, key=lambda x: x.name):
                        if s.name.lower() == 'theme.mp3':
                            add(e.name + '/' + s.name, s.path)
            except OSError:
                continue
    out.sort(key=lambda i: (not i['current'], i['nested'], i['rel'].lower()))
    return folder, out


def default_keep(items):
    cur = next((i for i in items if i['current'] and i['size'] >= 10 * 1024), None)
    return cur['rel'] if cur else (items[0]['rel'] if items else '')


# ----------------------------------------------------------------------------- trash ----
def trash_dir(app):
    d = Path(app.CONFIG) / TRASH
    d.mkdir(mode=0o700, exist_ok=True)
    if d.is_symlink():
        raise ValueError('Unsafe trash folder.')
    return d


def _sha(fd):
    h = hashlib.sha256()
    os.lseek(fd, 0, 0)
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def _copy_to_trash(folder_fd, rel, batch):
    """Copy one file (opened without following symlinks, relative to the folder) into the batch; verify by SHA-256."""
    parts = rel.split('/')
    dir_fd = folder_fd
    opened = []
    try:
        for p in parts[:-1]:
            dir_fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            opened.append(dir_fd)
        src = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            st = os.fstat(src)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError('Not a regular file: ' + rel)
            digest = _sha(src)
            stored = secrets.token_hex(8) + PurePosixPath(rel).suffix.lower()
            dst = os.open(batch / stored, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)  # read back to verify
            try:
                os.lseek(src, 0, 0)
                while True:
                    chunk = os.read(src, 1 << 20)
                    if not chunk:
                        break
                    os.write(dst, chunk)
                os.fsync(dst)
                if _sha(dst) != digest:
                    raise ValueError('Trash copy did not verify: ' + rel)
            finally:
                os.close(dst)
            return {'rel': rel, 'stored': stored, 'size': st.st_size, 'sha256': digest, 'mode': stat.S_IMODE(st.st_mode),
                    'uid': st.st_uid, 'gid': st.st_gid, 'mtime': st.st_mtime, 'ino': [st.st_dev, st.st_ino]}
        finally:
            os.close(src)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _unlink_if_same(folder_fd, item):
    """Remove the original only if it is still exactly the file that was copied to the trash."""
    parts = item['rel'].split('/')
    dir_fd, opened = folder_fd, []
    try:
        for p in parts[:-1]:
            dir_fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            opened.append(dir_fd)
        try:
            st = os.stat(parts[-1], dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if [st.st_dev, st.st_ino] != item['ino'] or st.st_size != item['size']:
            return False
        os.unlink(parts[-1], dir_fd=dir_fd)
        return True
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _publish(folder_fd, rel):
    """Make `rel` (an MP3 in the folder or a sub-folder) the folder's theme.mp3 — atomic rename, copy fallback across disks."""
    parts = rel.split('/')
    src_dir, opened = folder_fd, []
    try:
        for p in parts[:-1]:
            src_dir = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src_dir)
            opened.append(src_dir)
        try:
            os.replace(parts[-1], 'theme.mp3', src_dir_fd=src_dir, dst_dir_fd=folder_fd)
            return
        except OSError as error:
            if error.errno != 18:  # EXDEV: different disks in a pooled share → copy then replace
                raise
        staged = '.themearr-keep-' + secrets.token_hex(8) + '.mp3'
        src = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_dir)
        try:
            dst = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o664, dir_fd=folder_fd)
            try:
                while True:
                    chunk = os.read(src, 1 << 20)
                    if not chunk:
                        break
                    os.write(dst, chunk)
                os.fsync(dst)
            finally:
                os.close(dst)
        finally:
            os.close(src)
        os.replace(staged, 'theme.mp3', src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        os.unlink(parts[-1], dir_fd=src_dir)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def keep(app, raw_folder, keep_rel):
    """Keep one theme file as the folder's theme.mp3; every other theme file goes to the trash. Returns a message."""
    import permissions
    folder, items = candidates(app, raw_folder, probe=False)
    rels = [i['rel'] for i in items]
    if keep_rel not in rels:
        raise ValueError('That file is no longer in the folder — open the review again.')
    others = [r for r in rels if r != keep_rel]
    keeper = next(i for i in items if i['rel'] == keep_rel)
    convert = keeper['ext'] != '.mp3'
    if not others and keep_rel == 'theme.mp3':
        return 'Nothing to do — this folder already has only theme.mp3.'
    if not app._manual_install_lock.acquire(blocking=False):
        raise ValueError('Another install is running — try again in a moment.')
    released = False
    try:
        batch = trash_dir(app) / (time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
        batch.mkdir(mode=0o700)
        manifest = {'folder': str(folder), 'name': folder.name, 'at': int(time.time()), 'at_ns': time.time_ns(), 'kept': keep_rel, 'converted': convert,
                    'items': [], 'state': 'copying'}
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # 1. copy everything that will leave the folder (incl. a theme.mp3 that gets replaced, and a non-MP3 keeper)
            leaving = list(others) + ([keep_rel] if convert else [])
            for rel in leaving:
                manifest['items'].append(_copy_to_trash(fd, rel, batch))
            kst = os.stat(keep_rel, dir_fd=fd, follow_symlinks=False)
            manifest['kept_meta'] = {'mode': stat.S_IMODE(kst.st_mode), 'uid': kst.st_uid, 'gid': kst.st_gid}  # undo puts these back
            _write_json(batch / 'manifest.json', manifest)
            # 2. publish the keeper as theme.mp3
            if convert:
                with tempfile.TemporaryDirectory(prefix='themearr-keep-') as tmp:
                    src = Path(tmp) / ('source' + keeper['ext'])
                    shutil.copyfile(folder / keep_rel, src)
                    app._manual_install_lock.release(); released = True
                    app.manual_install_mp3(folder, src, replace='theme.mp3' in rels, backup=False,  # this review keeps its own copy
                                           activity_label='Library Health · kept ' + keep_rel + ' (converted to MP3)')
            elif keep_rel != 'theme.mp3':
                _publish(fd, keep_rel)
                permissions.apply_file(app.CONFIG, folder / 'theme.mp3')
            # 3. remove the originals that are now safely in the trash (theme.mp3 itself was replaced above)
            for item in manifest['items']:
                if item['rel'] != 'theme.mp3':
                    _unlink_if_same(fd, item)
            final = os.open('theme.mp3', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                manifest['final_sha256'] = _sha(final)
            finally:
                os.close(final)
        finally:
            os.close(fd)
        manifest['state'] = 'done'
        _write_json(batch / 'manifest.json', manifest)
    finally:
        if not released:
            app._manual_install_lock.release()
    with app.db() as c:
        c.execute("UPDATE shows SET state='installed', detail=? WHERE path=?", ('Kept ' + keep_rel + ' (Library Health review)', str(folder)))
    moved = [i['rel'] for i in manifest['items'] if not (convert and i['rel'] == keep_rel)]
    app.event(folder.name, 'theme kept', 'Library Health · kept ' + keep_rel + ' as theme.mp3; moved to trash: ' + (', '.join(moved) or 'nothing'))
    try:
        __import__('library_health').refresh_title(app, str(folder))
        if not convert:
            __import__('media_servers').after_install(app.CONFIG, str(folder))
    except Exception:
        pass
    return ('Kept ' + keep_rel + ' as theme.mp3' + (' (converted to MP3)' if convert else '') + '. Moved to the recycle bin: '
            + (', '.join(moved) or 'nothing') + '. You can undo this from the recycle bin.')


def save_replaced(app, folder, folder_fd):
    """1.12: before a theme.mp3 is replaced (Theme Browser, custom upload, YouTube), keep the old one in the recycle bin.
    Undo (recycle bin) puts it back as long as the new theme.mp3 is unchanged. Raises if the copy cannot be made and verified."""
    batch = trash_dir(app) / (time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
    batch.mkdir(mode=0o700)
    manifest = {'folder': str(folder), 'name': Path(folder).name, 'at': int(time.time()), 'at_ns': time.time_ns(), 'kept': 'theme.mp3',
                'converted': False, 'kind': 'replaced', 'items': [_copy_to_trash(folder_fd, 'theme.mp3', batch)], 'state': 'copying'}
    _write_json(batch / 'manifest.json', manifest)
    return batch, manifest


def finish_replaced(saved, folder_fd):
    batch, manifest = saved
    final = os.open('theme.mp3', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=folder_fd)
    try:
        manifest['final_sha256'] = _sha(final)
    finally:
        os.close(final)
    manifest['state'] = 'done'
    _write_json(batch / 'manifest.json', manifest)


def batches(app):
    out = []
    root = Path(app.CONFIG) / TRASH
    if not root.is_dir() or root.is_symlink():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir() or d.is_symlink():
            continue
        try:
            m = json.loads((d / 'manifest.json').read_text())
        except (OSError, ValueError):
            continue
        m['id'] = d.name
        m['bytes'] = sum(i['size'] for i in m.get('items', []))
        out.append(m)
    out.sort(key=lambda m: m.get('at_ns') or m.get('at', 0) * 10**9, reverse=True)  # newest first, exact order
    return out


def _batch(app, bid):
    if not re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{8}', bid or ''):
        raise ValueError('Unknown trash entry.')
    d = Path(app.CONFIG) / TRASH / bid
    if not d.is_dir() or d.is_symlink():
        raise ValueError('That trash entry no longer exists.')
    return d, json.loads((d / 'manifest.json').read_text())


def undo(app, bid):
    """Put the folder back exactly as it was before the review. Refused if theme.mp3 changed since."""
    d, m = _batch(app, bid)
    if m.get('state') != 'done':
        raise ValueError('This entry was not completed — nothing to undo; files are kept in the trash.')
    folder = _folder(app, m['folder'])
    if not app._manual_install_lock.acquire(blocking=False):
        raise ValueError('Another install is running — try again in a moment.')
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                cur = os.open('theme.mp3', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                raise ValueError('theme.mp3 is gone from the folder — undo is not safe; files stay in the recycle bin.') from None
            try:
                if _sha(cur) != m.get('final_sha256'):
                    raise ValueError('theme.mp3 was changed after the review — undo is not safe; files stay in the recycle bin.')
            finally:
                os.close(cur)
            items = {i['rel']: i for i in m['items']}
            for rel in items:
                if rel == 'theme.mp3':
                    continue
                parts = rel.split('/')
                try:
                    os.stat(rel, dir_fd=fd, follow_symlinks=False)
                    raise ValueError(f'“{rel}” exists again in the folder — undo would overwrite it; nothing changed.')
                except FileNotFoundError:
                    pass
            # kept MP3 goes back to its old name, unless it was converted (then the converted theme.mp3 is replaced below)
            if not m['converted'] and m['kept'] != 'theme.mp3':
                parts = m['kept'].split('/')
                dst_dir = fd if len(parts) == 1 else os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    try:
                        os.rename('theme.mp3', parts[-1], src_dir_fd=fd, dst_dir_fd=dst_dir)
                    except OSError as error:
                        if error.errno != 18:
                            raise
                        _restore_file(d / '.none', None, dst_dir, parts[-1], src_fd_name=('theme.mp3', fd))
                        os.unlink('theme.mp3', dir_fd=fd)
                    meta = m.get('kept_meta')
                    if meta:  # the kept file gets its original mode/owner back
                        kfd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dst_dir)
                        try:
                            os.fchmod(kfd, meta['mode'])
                            try:
                                os.fchown(kfd, meta['uid'], meta['gid'])
                            except OSError:
                                pass
                        finally:
                            os.close(kfd)
                finally:
                    if dst_dir != fd:
                        os.close(dst_dir)
            if m['converted'] and 'theme.mp3' not in items:
                os.unlink('theme.mp3', dir_fd=fd)  # the folder had no theme.mp3 before; the converted one goes away
            for rel, item in items.items():
                parts = rel.split('/')
                dst_dir = fd if len(parts) == 1 else os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    _restore_file(d / item['stored'], item, dst_dir, parts[-1])
                finally:
                    if dst_dir != fd:
                        os.close(dst_dir)
        finally:
            os.close(fd)
    finally:
        app._manual_install_lock.release()
    shutil.rmtree(d)
    app.event(folder.name, 'theme review undone', 'Library Health · restored ' + ', '.join(sorted(items)))
    try:
        __import__('library_health').refresh_title(app, str(folder))
    except Exception:
        pass
    return 'Undone: ' + folder.name + ' is back the way it was (' + ', '.join(sorted(items)) + ' restored).'


def _restore_file(src_path, item, dst_dir, name, src_fd_name=None):
    staged = '.themearr-restore-' + secrets.token_hex(8)
    if src_fd_name:
        src = os.open(src_fd_name[0], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_fd_name[1])
    else:
        src = os.open(src_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        dst = os.open(staged, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dst_dir)  # read back to verify
        try:
            while True:
                chunk = os.read(src, 1 << 20)
                if not chunk:
                    break
                os.write(dst, chunk)
            os.fsync(dst)
            if item:
                if _sha(dst) != item['sha256']:
                    raise ValueError('Trash file is damaged — not restored.')
                os.fchmod(dst, item['mode'])
                try:
                    os.fchown(dst, item['uid'], item['gid'])
                except OSError:
                    pass
        finally:
            os.close(dst)
        os.replace(staged, name, src_dir_fd=dst_dir, dst_dir_fd=dst_dir)
        if item:
            os.utime(name, (item['mtime'], item['mtime']), dir_fd=dst_dir, follow_symlinks=False)
    except Exception:
        try:
            os.unlink(staged, dir_fd=dst_dir)
        except OSError:
            pass
        raise
    finally:
        os.close(src)


def delete_batch(app, bid):
    d, m = _batch(app, bid)
    shutil.rmtree(d)
    return 'Deleted permanently from the recycle bin: ' + ', '.join(i['rel'] for i in m.get('items', [])) + ' (' + m.get('name', '') + ').'


def purge(app, older_than_days=None, everything=False):
    """Delete trash entries older than the setting (or everything). Returns the number removed."""
    days = settings(app.CONFIG)['days'] if older_than_days is None else older_than_days
    limit = time.time() - days * 86400
    n = 0
    for m in batches(app):
        if everything or m.get('at', 0) < limit:
            try:
                shutil.rmtree(Path(app.CONFIG) / TRASH / m['id'])
                n += 1
            except OSError:
                pass
    return n


def tick(app, now=None):
    """Called every minute: expire old entries (at most hourly) and run the cron schedule (once per matching minute)."""
    now = now or datetime.datetime.now()
    s = settings(app.CONFIG)
    if time.time() - _SCHED['last_purge'] >= 3600:
        _SCHED['last_purge'] = time.time()
        n = purge(app)
        if n:
            app.event('Recycle bin', 'expired', f"{n} entr{'y' if n == 1 else 'ies'} older than {s['days']} days deleted")
    spec = parse_cron(s['cron']) if s['cron'] else None
    minute = now.strftime('%Y%m%d%H%M')
    if spec and cron_matches(spec, now) and _SCHED['last_cron_minute'] != minute:
        _SCHED['last_cron_minute'] = minute
        n = purge(app, everything=True)
        app.event('Recycle bin', 'emptied', f"Cron schedule “{s['cron']}”: {n} entr{'y' if n == 1 else 'ies'} deleted")


def start(app):
    if _SCHED['started']:
        return
    _SCHED['started'] = True

    def loop():
        while True:
            try:
                tick(app)
            except Exception as error:
                print('Theme trash schedule error: ' + type(error).__name__, flush=True)
            time.sleep(60 - time.localtime().tm_sec % 60 or 60)
    threading.Thread(target=loop, name='themearr-theme-trash', daemon=True).start()


# ------------------------------------------------------------------------------- pages ----
def recycle_text(config):
    s = settings(config)
    spec = parse_cron(s['cron']) if s['cron'] else None
    nxt = next_run(spec) if spec else None
    text = f"Removed files go to the recycle bin and are kept for {s['days']} day{'s' if s['days'] != 1 else ''}, then deleted automatically."
    text += (f" It is also emptied on the schedule “{s['cron']}” (next: {nxt.strftime('%Y-%m-%d %H:%M')})." if nxt else ' No emptying schedule is set.')
    return text


def recycle_note(config):
    """V1.9.5 (user): show the recycle bin settings that apply, with a reminder they can be changed."""
    return ('<p class="tr-recycle" role="note">♻️ ' + _e(recycle_text(config)) + ' <a href="/settings?section=recycle">Change this in Settings → General → Recycle bin</a>.</p>')


def _guru_help(cron):
    """V1.9.10: point to crontab.guru for writing/checking a schedule (opens in a new tab; Themearr never contacts it)."""
    link = 'https://crontab.guru/'
    if cron and re.fullmatch(r'[0-9A-Za-z*/,\- ]{1,100}', cron):
        link += '#' + '_'.join(cron.split())  # crontab.guru reads the schedule from the address, fields joined by _
    return ('<p class="help tr-guru">Not sure how to write a schedule? <a href="' + _e(link) + '" target="_blank" rel="noopener noreferrer">crontab.guru</a> '
            '(free website, opens in a new tab) turns a cron line into plain English — e.g. <code>30 4 1 * *</code> becomes “At 04:30 on day-of-month 1” — '
            'and lists the next times it will run. ' + ('The link opens with your current schedule filled in. ' if link.count('#') else '') +
            'It only helps you write the line; Preludarr does not contact it.</p>')


def settings_panel(config, notice=''):
    """Recycle bin section for Settings → General (under Authentication), styled like the account form there."""
    s = settings(config)
    spec = parse_cron(s['cron']) if s['cron'] else None
    nxt = next_run(spec) if spec else None
    bad = notice.startswith('✗')
    return ('<section class="panel" id="settings-recycle-bin"><div class="pill">General · Recycle bin</div><h2>♻️ Recycle bin</h2>'
            '<p class="muted">Theme files you remove in a Library Health review go here first, so you can undo. '
            '<a href="/library-health/trash">Open the recycle bin</a>.</p>'
            + ('<div role="' + ('alert' if bad else 'status') + '" class="notice">' + _e(notice.lstrip('✗ ')) + '</div>' if notice else '') +
            '<form method="post" action="/library-health/trash" autocomplete="off"><input type="hidden" name="action" value="settings">'
            '<label for="tr-days">Keep removed files for (days)<input id="tr-days" name="days" type="number" min="1" max="3650" required value="' + str(s['days']) + '"></label>'
            '<p class="help">Older entries are deleted automatically (checked every hour). Default 30.</p>'
            '<label for="tr-cron">Empty on a schedule (cron, optional)<input id="tr-cron" name="cron" maxlength="100" placeholder="blank = off · e.g. 0 3 * * 0" '
            'value="' + _e(s['cron']) + '" data-bwignore="true" data-lpignore="true" data-1p-ignore="true"></label>'
            '<p class="help">Empties the <strong>whole</strong> recycle bin each time it runs. Leave blank for no schedule.</p>'
            + _guru_help(s['cron']) +
            '<details class="tm-help"><summary>Cron format &amp; examples</summary><p class="help">Five fields: <code>minute hour day-of-month month day-of-week</code> (server time). '
            'Examples: <code>0 3 * * 0</code> Sundays 03:00 · <code>30 4 1 * *</code> the 1st of every month 04:30 · <code>0 */12 * * *</code> every 12 hours. '
            'Day names (<code>sun</code>, <code>mon-fri</code>) and steps (<code>*/15</code>) work too.</p></details>'
            + ('<p class="help tr-next">Next scheduled emptying: <strong>' + nxt.strftime('%Y-%m-%d %H:%M') + '</strong> (server time).</p>' if nxt else '') +
            '<button type="submit">Save Recycle Bin</button></form></section>')



def _dur(sec):
    return '' if not sec else f'{int(sec // 60)}:{int(sec % 60):02d}'


def _size(n):
    return f'{n / 1048576:.2f} MB' if n >= 1048576 else f'{max(1, n // 1024)} KB'


def review_page(app, handler):
    import media_servers as v4
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    raw = q.get('path', [''])[0]
    notice = q.get('notice', [''])[0][:400]
    parts = [v4.settings_shell('Review theme files', 'system'),
             '</aside><main class="main"><div class="topline"><div class="crumb">System / '
             '<a href="/system?tab=health#findings">Library Health</a> / <strong>Review theme files</strong></div></div>']
    try:
        folder, items = candidates(app, raw)
    except (ValueError, OSError) as error:
        parts += ['<h1 class="heading">Review theme files</h1><p class="notice tr-bad" role="alert">', _e(error), '</p>', STYLE, '</main></div></body></html>']
        return handler.send(200, ''.join(parts), 'text/html')
    parts += ['<h1 class="heading">', _e(folder.name), '</h1>',
              '<p class="muted">Listen to each theme file, choose the <strong>one</strong> to keep and Preludarr makes it this folder’s <code>theme.mp3</code> '
              '(the file Plex and Jellyfin play). The others move to the <a href="/library-health/trash">recycle bin</a> — you can undo it from there.</p>']
    if notice:
        parts += ['<p class="notice', ' tr-bad' if notice.startswith('✗') else '', '" role="status">', _e(notice.lstrip('✗ ')), '</p>']
    if len(items) < 2 and not any(i['nested'] for i in items):
        parts += ['<p>This folder has ', ('only <code>theme.mp3</code>' if items else 'no theme files'), ' — nothing to review. ',
                  '<a href="/system?tab=health#findings">Back to Library Health</a></p>', STYLE, '</main></div></body></html>']
        return handler.send(200, ''.join(parts), 'text/html')
    chosen = default_keep(items)
    parts += ['<form method="post" action="/library-health/keep" class="tr-form"><input type="hidden" name="path" value="', _e(folder), '">',
              '<div class="tr-grid">']
    for i in items:
        audio = '/library-health/audio?' + urllib.parse.urlencode({'path': str(folder), 'file': i['rel']})
        where = ('In a sub-folder (' + i['rel'].split('/')[0] + ') — Plex ignores it' if i['nested'] else
                 'The file Plex/Jellyfin play' if i['current'] else 'Extra file — Plex ignores it')
        song = ' — '.join(x for x in (i.get('title'), i.get('artist')) if x)
        parts += ['<label class="tr-card', ' tr-current' if i['current'] else '', '"><span class="tr-pick"><input type="radio" name="keep" value="', _e(i['rel']), '"',
                  ' checked' if i['rel'] == chosen else '', '> Keep this one</span>',
                  '<code class="tr-name">', _e(i['rel']), '</code><span class="tr-where">', _e(where), '</span>',
                  '<span class="tr-meta">', _e(song or 'No song name in the file'), '</span>',
                  '<span class="tr-meta muted">', ' · '.join(x for x in (_dur(i.get('duration')), _size(i['size']), i['ext'][1:].upper(),
                                                                         time.strftime('%Y-%m-%d', time.localtime(i['mtime']))) if x), '</span>',
                  '<audio controls preload="none" src="', _e(audio), '"></audio></label>']
    parts += ['</div>', recycle_note(app.CONFIG),
              '<label class="tr-confirm"><input type="checkbox" name="confirm" value="1" required> I understand: the chosen file becomes '
              '<code>theme.mp3</code> and every other theme file listed here moves to the recycle bin.</label>',
              '<div class="tr-actions"><button type="submit">Keep the selected file</button><a class="btn" href="/system?tab=health#findings">Leave everything as it is</a></div></form>',
              STYLE, '</main></div></body></html>']
    return handler.send(200, ''.join(parts), 'text/html')


def trash_page(app, handler):
    import media_servers as v4
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    notice = q.get('notice', [''])[0][:400]
    s = settings(app.CONFIG)
    items = batches(app)
    spec = parse_cron(s['cron']) if s['cron'] else None
    nxt = next_run(spec) if spec else None
    parts = [v4.settings_shell('Recycle bin', 'system'),
             '</aside><main class="main"><div class="topline"><div class="crumb">System / '
             '<a href="/system?tab=health">Library Health</a> / <strong>Recycle bin</strong></div></div>',
             '<h1 class="heading">Recycle bin</h1>',
             '<p class="muted">Theme files removed by a Library Health review, and themes you replaced (Theme Browser, upload, YouTube), wait here, '
             'outside your media folders, so you can undo.</p>']
    if notice:
        parts += ['<p class="notice', ' tr-bad' if notice.startswith('✗') else '', '" role="', 'alert' if notice.startswith('✗') else 'status', '">', _e(notice.lstrip('✗ ')), '</p>']
    parts += [recycle_note(app.CONFIG)]
    # entries
    total = sum(m['bytes'] for m in items)
    parts += ['<section class="panel"><h2>In the recycle bin</h2>']
    if not items:
        parts += ['<p>The recycle bin is empty.</p>']
    else:
        parts += ['<p>', str(len(items)), ' entr', 'y' if len(items) == 1 else 'ies', ' · ', _size(total), '.</p><div class="tr-list"><table>',
                  '<tr><th>Title</th><th>Removed files</th><th>Kept as theme.mp3</th><th>Removed on</th><th></th></tr>']
        for m in items:
            files = [i['rel'] for i in m.get('items', []) if not (m.get('converted') and i['rel'] == m.get('kept'))]
            expires = datetime.datetime.fromtimestamp(m.get('at', 0)) + datetime.timedelta(days=s['days'])
            parts += ['<tr><td><a href="/themes?', _e(urllib.parse.urlencode({'path': m['folder']})), '">', _e(m.get('name', '')), '</a></td>',
                      '<td>', ('previous <code>theme.mp3</code> (replaced by a new theme)' if m.get('kind') == 'replaced'
                               else '<br>'.join('<code>' + _e(f) + '</code>' for f in files) or '—'), '</td><td>',
                      ('the new theme' if m.get('kind') == 'replaced' else '<code>' + _e(m.get('kept', '')) + '</code>' + (' (converted)' if m.get('converted') else '')),
                      '</td><td>', _e(__import__('ui').when(m.get('at', 0))),
                      '<br><small class="muted">expires ', expires.strftime('%Y-%m-%d'), '</small></td><td class="tr-row-actions">']
            for action, label in (('undo', 'Undo'), ('delete', 'Delete now')):
                parts += ['<form method="post" action="/library-health/trash"><input type="hidden" name="action" value="', action, '">',
                          '<input type="hidden" name="id" value="', _e(m['id']), '"><button type="submit"', ' class="tr-danger"' if action == 'delete' else '', '>', label,
                          '</button></form>']
            parts += ['</td></tr>']
        parts += ['</table></div><form method="post" action="/library-health/trash" class="tr-empty"><input type="hidden" name="action" value="empty">',
                  '<label class="tr-confirm"><input type="checkbox" name="confirm" value="1" required> Delete everything in the recycle bin for good</label>',
                  '<button type="submit" class="tr-danger">Empty recycle bin now</button></form>']
    parts += ['</section>', STYLE, '</main></div></body></html>']
    return handler.send(200, ''.join(parts), 'text/html')


def audio(app, handler):
    """Stream one reviewed theme file (only files the review lists for a registered folder). Supports Range for seeking."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    try:
        folder, items = candidates(app, q.get('path', [''])[0], probe=False)
    except (ValueError, OSError):
        return handler.send(404, 'Not found', 'text/plain')
    rel = q.get('file', [''])[0]
    item = next((i for i in items if i['rel'] == rel), None)
    if not item:
        return handler.send(404, 'Not found', 'text/plain')
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = rel.split('/')
        dir_fd = fd if len(parts) == 1 else os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            src = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        finally:
            if dir_fd != fd:
                os.close(dir_fd)
    finally:
        os.close(fd)
    with os.fdopen(src, 'rb') as f:
        size = os.fstat(f.fileno()).st_size
        start, end = 0, size - 1
        m = re.fullmatch(r'bytes=(\d*)-(\d*)', handler.headers.get('Range', '') or '')
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1)); end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                handler.send_response(416); handler.send_header('Content-Range', f'bytes */{size}'); handler.end_headers(); return
            handler.send_response(206); handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        else:
            handler.send_response(200)
        handler.send_header('Content-Type', TYPES.get(item['ext'], 'application/octet-stream'))
        handler.send_header('Accept-Ranges', 'bytes'); handler.send_header('Content-Length', str(end - start + 1))
        handler.send_header('Cache-Control', 'no-store'); handler.end_headers()
        f.seek(start)
        left = end - start + 1
        while left > 0:
            chunk = f.read(min(1 << 16, left))
            if not chunk:
                break
            handler.wfile.write(chunk)
            left -= len(chunk)


def _form(handler, limit=4096):
    size = int(handler.headers.get('Content-Length', '0') or 0)
    if not 0 < size <= limit:
        raise ValueError('Invalid form.')
    form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=8)
    return lambda k: form.get(k, [''])[0].strip()


def post(app, handler, redirect):
    route = urllib.parse.urlsplit(handler.path).path
    back = '/library-health/trash'
    settings_form = False
    try:
        f = _form(handler)
        settings_form = f('action') == 'settings'
        if route == '/library-health/keep':
            back = '/library-health/review?' + urllib.parse.urlencode({'path': f('path')})
            if f('confirm') != '1':
                raise ValueError('Tick the box to confirm.')
            msg = keep(app, f('path'), f('keep'))
            back = '/library-health/trash'
        else:
            action = f('action')
            if action == 'settings':
                raw = f('days')
                if not raw.isdigit() or not 1 <= int(raw) <= 3650:
                    raise ValueError('Keep files for 1 to 3650 days.')
                cron = ' '.join(f('cron').split())
                spec = parse_cron(cron)
                _write_json(Path(app.CONFIG) / SETTINGS, {'days': int(raw), 'cron': cron})
                nxt = next_run(spec) if spec else None
                msg = ('Recycle bin saved: keep files ' + raw + ' days' + ('; empty on schedule “' + cron + '” — next run ' + nxt.strftime('%Y-%m-%d %H:%M') if nxt else '; no schedule') + '.')
            elif action == 'undo':
                msg = undo(app, f('id'))
            elif action == 'delete':
                msg = delete_batch(app, f('id'))
            elif action == 'empty':
                if f('confirm') != '1':
                    raise ValueError('Tick the box to confirm.')
                n = purge(app, everything=True)
                msg = f"Recycle bin emptied: {n} entr{'y' if n == 1 else 'ies'} deleted for good."
                app.event('Recycle bin', 'emptied', msg)
            else:
                raise ValueError('Unknown action.')
    except (ValueError, OSError, RuntimeError, UnicodeError) as error:
        msg = '✗ ' + (str(error) if isinstance(error, (ValueError, RuntimeError)) else 'Could not complete: ' + type(error).__name__)
    if route == '/library-health/trash' and settings_form:  # V1.9.5: the settings form lives in Settings → General
        return redirect(handler, '/settings?' + urllib.parse.urlencode({'section': 'recycle', 'recycle_notice': msg[:400]}))
    sep = '&' if '?' in back else '?'
    return redirect(handler, back + sep + urllib.parse.urlencode({'notice': msg[:400]}))


STYLE = ('<style>.tr-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:14px;margin:14px 0}'
         '.tr-card{display:flex;flex-direction:column;gap:6px;padding:16px;border:1px solid var(--line,#3b4b6b);border-radius:10px;background:#1c2a44;cursor:pointer}'
         '.tr-card:has(input:checked){border-color:#3468de;box-shadow:0 0 0 2px #3468de inset}.tr-current{background:#1d3150}'
         '.tr-pick{display:flex;gap:8px;align-items:center;font-weight:700}.tr-name{font-size:1.05em;color:#9fd0ff;overflow-wrap:anywhere}'
         '.tr-where{color:#ffd36b;font-size:.9em}.tr-current .tr-where{color:#8fe3a8}.tr-meta{overflow-wrap:anywhere}.tr-card audio{width:100%;margin-top:6px}'
         '.tr-recycle{margin:12px 0;padding:10px 12px;border:1px solid #5d6b85;border-left:3px solid #8fe3a8;border-radius:8px;background:#1f2d47}'
         '.tr-confirm{display:flex;gap:8px;align-items:flex-start;margin:12px 0}.tr-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}'
         '.tr-bad{border-color:#e07a86!important;background:#4a2430!important}.tr-danger,button.tr-danger{background:#9b394a!important;color:#fff!important;border-color:#c25466!important}'
         '.tr-list{overflow-x:auto}.tr-list table{width:100%;border-collapse:collapse}.tr-list th,.tr-list td{text-align:left;padding:8px 10px 8px 0;vertical-align:top;border-bottom:1px solid #2b3a55}'
         '.tr-row-actions{display:flex;gap:6px;flex-wrap:wrap}.tr-row-actions form{margin:0}.tr-empty{margin-top:14px}'
         '#trash-settings .pm-row{display:grid;grid-template-columns:200px minmax(0,1fr);gap:24px;align-items:start;margin:14px 0}'
         '#trash-settings .pm-label{text-align:right;font-weight:700;padding-top:9px}'
         '#trash-settings input{padding:10px;background:#101c30;color:#fff;border:1px solid #647da0;border-radius:7px;box-sizing:border-box}'
         '#trash-settings #tr-days{width:110px}#trash-settings #tr-cron{width:min(420px,100%);display:block}'
         '#trash-settings .pm-help{color:var(--muted,#adbed9);margin:8px 0 0}#trash-settings .pm-ok{color:#8fe3a8;margin:8px 0 0}'
         '@media(max-width:760px){#trash-settings .pm-row{grid-template-columns:1fr;gap:6px}#trash-settings .pm-label{text-align:left;padding-top:0}}</style>')
