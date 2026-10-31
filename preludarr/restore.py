# SPDX-License-Identifier: GPL-3.0-or-later
"""Restore a Preludarr backup on the next start ("restore on restart", 1.12 pre-release task 6).

Works on any Docker host: no helper outside the container and no access to Docker itself.
  1. System → Backup & Restore: the user picks a verified backup and confirms → `schedule()` writes /config/restore-pending.json.
  2. The user restarts the container.
  3. At the very start of app.py — before the database or any setting is opened — `apply_pending()`:
     * verifies the archive again (structure, size limits, checksums, SQLite integrity) and that it is the one confirmed;
     * saves a safety copy of every file it will replace to /config/system_backups/pre-restore-<time>.tar (0600);
     * restores library.db and every settings file the backup contains (config/*.json …) through a staging folder, atomically;
     * checks the result (hashes + SQLite integrity); on ANY error puts the previous files back from the safety copy;
     * writes /config/restore-result.json, shown on the Backup page, and removes the request.
Media files are never touched. Standard library only (it runs before the rest of Preludarr is imported).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

SCHEMA = 'themearr.system-backup.v1'          # unchanged since V1.5: older backups stay valid
MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_DB_BYTES = 160 * 1024 * 1024
FILENAME = re.compile(r'\A(tsbackup-\d{8}T\d{6}Z-[a-f0-9]{8}\.zip)\Z')
EXTRA_NAME = re.compile(r'\A[a-zA-Z0-9][a-zA-Z0-9_.-]{0,85}\.(?:json|yaml|yml|toml|ini)\Z')
PENDING = 'restore-pending.json'
RESULT = 'restore-result.json'
SIDEFILES = ('library.db-wal', 'library.db-shm')


def verify_archive(path):
    """Bounded structure/integrity check; never extracts into the config folder. Returns {'created_utc','files','sha256'}."""
    p = Path(path)
    if not p.is_file() or p.is_symlink() or p.stat().st_size > MAX_ZIP_BYTES:
        raise ValueError('Invalid backup archive')
    with zipfile.ZipFile(p) as z:
        infos = z.infolist()
        if len(infos) > 30 or len({i.filename for i in infos}) != len(infos):
            raise ValueError('Unexpected archive entries')
        allowed = lambda name: name == 'library.db' or name == 'manifest.json' or (name.startswith('config/') and EXTRA_NAME.fullmatch(name[7:]))  # noqa: E731
        if any(not allowed(i.filename) or i.is_dir() or i.file_size > MAX_DB_BYTES or i.flag_bits & 1
               or (i.filename == 'manifest.json' and i.file_size > 8192)
               or (i.filename.startswith('config/') and i.file_size > 2 * 1024 * 1024) for i in infos):
            raise ValueError('Unsafe archive entry')
        if sum(i.file_size for i in infos) > MAX_DB_BYTES + 64 * 1024 * 1024:
            raise ValueError('Archive expanded size limit exceeded')
        if 'library.db' not in z.namelist() or 'manifest.json' not in z.namelist():
            raise ValueError('Required backup files missing')
        manifest = json.loads(z.read('manifest.json'))
        if not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA:
            raise ValueError('Not a Preludarr System backup')
        checks = manifest.get('files')
        if not isinstance(checks, dict) or set(checks) != (set(z.namelist()) - {'manifest.json'}):
            raise ValueError('Backup checksum manifest mismatch')
        with tempfile.TemporaryDirectory(prefix='preludarr-archive-check-') as td:
            tempdb = Path(td) / 'library.db'
            for name, digest in checks.items():
                if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                    raise ValueError('Invalid backup checksum')
                sha = hashlib.sha256()
                with z.open(name) as src:
                    if name == 'library.db':
                        with open(tempdb, 'wb') as out:
                            while data := src.read(128 * 1024):
                                sha.update(data); out.write(data)
                    else:
                        while data := src.read(128 * 1024):
                            sha.update(data)
                if not hmac.compare_digest(sha.hexdigest(), digest):
                    raise ValueError('Backup checksum validation failed')
            with closing(sqlite3.connect('file:' + quote(str(tempdb)) + '?mode=ro', uri=True)) as conn:
                if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup database integrity failed')
    return {'created_utc': manifest.get('created_utc', 'unknown'), 'files': len(checks), 'sha256': _sha(p)}


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while data := f.read(1024 * 1024):
            h.update(data)
    return h.hexdigest()


def _write_json(path, data):
    tmp = path.with_name('.' + path.name + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, sort_keys=True)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def _archive_path(config, name):
    if not FILENAME.fullmatch(str(name or '')):
        raise ValueError('Invalid backup filename')
    folder = Path(config) / 'system_backups'
    p = folder / name
    if folder.is_symlink() or p.is_symlink() or not p.is_file():
        raise ValueError('Backup unavailable')
    return p


# ------------------------------------------------------------------------------ scheduling ----
def schedule(config, name, sha256):
    """Mark a verified backup to be restored on the next start."""
    p = _archive_path(config, name)
    info = verify_archive(p)
    if not hmac.compare_digest(info['sha256'], str(sha256 or '')):
        raise ValueError('Backup changed since it was verified')
    _write_json(Path(config) / PENDING, {'archive': name, 'sha256': info['sha256'], 'scheduled_utc': datetime.now(timezone.utc).isoformat()})
    return info


def pending(config):
    p = Path(config) / PENDING
    try:
        data = json.loads(p.read_text()) if p.is_file() and not p.is_symlink() else None
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def cancel(config):
    p = Path(config) / PENDING
    if p.is_file() and not p.is_symlink():
        p.unlink()


def last_result(config):
    p = Path(config) / RESULT
    try:
        data = json.loads(p.read_text()) if p.is_file() and not p.is_symlink() else None
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# -------------------------------------------------------------------------------- applying ----
def apply_pending(config, log=print):
    """Run once at start-up, before anything opens the database. Returns the result dict, or None if nothing was pending."""
    config = Path(config)
    req = pending(config)
    if req is None:
        return None
    started = datetime.now(timezone.utc).isoformat()
    result = {'archive': str(req.get('archive')), 'started_utc': started}
    safety = None
    state = {'changed': False, 'rolled_back': None}
    try:
        if not config.is_dir() or config.is_symlink():
            raise ValueError('Config folder unavailable')
        archive = _archive_path(config, req.get('archive'))
        info = verify_archive(archive)
        if not hmac.compare_digest(info['sha256'], str(req.get('sha256') or '')):
            raise ValueError('The backup file changed after it was confirmed')
        with zipfile.ZipFile(archive) as z:
            members = [n for n in z.namelist() if n != 'manifest.json']
            targets = {n: ('library.db' if n == 'library.db' else n[len('config/'):]) for n in members}
            # 1. safety copy of everything that will be replaced (and the database journals)
            backups = config / 'system_backups'
            backups.mkdir(mode=0o700, exist_ok=True)
            target_safety = backups / ('pre-restore-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3) + '.tar')
            present = {}
            fd = os.open(target_safety, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            safety = target_safety
            with os.fdopen(fd, 'wb') as raw, tarfile.open(fileobj=raw, mode='w') as tar:
                for name in sorted(set(targets.values()) | set(SIDEFILES)):
                    path = config / name
                    if path.is_symlink() or (path.exists() and not path.is_file()):
                        raise ValueError('Unsafe file in the config folder: ' + name)
                    present[name] = path.exists()
                    if present[name]:
                        tar.add(path, arcname=name, recursive=False)
            # 2. stage, check, then swap in
            try:
                with tempfile.TemporaryDirectory(prefix='.preludarr-restore-', dir=config) as td:
                    stage = Path(td)
                    for member, target in targets.items():
                        with z.open(member) as src, open(stage / target, 'xb') as dst:
                            shutil.copyfileobj(src, dst, 128 * 1024)
                        existing = config / target
                        os.chmod(stage / target, (existing.stat().st_mode & 0o777) if existing.exists() else 0o600)
                    with closing(sqlite3.connect('file:' + quote(str(stage / 'library.db')) + '?mode=ro', uri=True)) as con:
                        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise ValueError('Staged database failed its integrity check')
                    for side in SIDEFILES:
                        if (config / side).exists():
                            state['changed'] = True
                            (config / side).unlink()
                    for target in targets.values():
                        state['changed'] = True
                        os.replace(stage / target, config / target)
                for member, target in targets.items():
                    if _sha(config / target) != hashlib.sha256(z.read(member)).hexdigest():
                        raise ValueError('Restored file differs from the backup: ' + target)
                with closing(sqlite3.connect('file:' + quote(str(config / 'library.db')) + '?mode=ro', uri=True)) as con:
                    if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Restored database failed its integrity check')
                for side in SIDEFILES:
                    if (config / side).exists():
                        (config / side).unlink()
            except BaseException:
                if state['changed']:
                    try:
                        _rollback(config, safety, present)
                        state['rolled_back'] = True
                    except Exception:
                        state['rolled_back'] = False
                raise
        result.update(ok=True, files=len(targets), created_utc=info['created_utc'], safety=safety.name,
                      message=f'Restored {len(targets)} file(s) from the backup made {_utc(info["created_utc"])}. Your previous data is saved as {safety.name}.')
        log('Preludarr restore: ' + result['message'], flush=True)
    except Exception as error:
        if not state['changed']:
            message = 'The restore did not run and nothing was changed: ' + str(error)
        elif state['rolled_back']:
            message = 'The restore failed and your previous data was put back (' + str(error) + '). Safety copy: ' + safety.name
        else:
            message = ('The restore failed and putting your previous data back also failed (' + str(error) + '). Stop Preludarr and restore '
                       'system_backups/' + safety.name + ' into the config folder by hand.')
        result.update(ok=False, safety=safety.name if safety and safety.exists() else '', message=message)
        log('Preludarr restore FAILED: ' + result['message'], flush=True)
    result['finished_utc'] = datetime.now(timezone.utc).isoformat()
    try:
        cancel(config)
        _write_json(config / RESULT, result)
    except OSError:
        pass
    return result


def _utc(text):
    try:
        return datetime.fromisoformat(str(text)).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    except ValueError:
        return str(text)


def _rollback(config, safety, present):
    with tarfile.open(safety, 'r') as tar:
        for name, existed in present.items():
            dest = config / name
            if existed:
                member = tar.getmember(name)
                if not member.isfile():
                    raise ValueError('Bad safety copy member: ' + name)
                src = tar.extractfile(member)
                tmp = config / ('.rollback-' + name)
                with src, open(tmp, 'wb') as out:
                    shutil.copyfileobj(src, out)
                os.chmod(tmp, member.mode & 0o777)
                os.replace(tmp, dest)
            elif dest.exists() and dest.is_file() and not dest.is_symlink():
                dest.unlink()
