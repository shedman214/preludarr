# SPDX-License-Identifier: GPL-3.0-or-later

"""Themearr v0.9: read-only theme management browser."""

import html
import urllib.parse
from pathlib import Path

from login import APP_VERSION, brand_html, full_sidebar

PER_PAGE = 40

def page(handler, roots, db, css):
    esc = html.escape
    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(handler.path).query
    )
    term = query.get("q", [""])[0].strip()[:100]
    library = query.get("library", ["all"])[0]
    view = query.get("view", ["installed"])[0]

    if library != "all" and not __import__("kinds").valid(library):
        library = "all"
    if view not in ("all", "installed", "missing"):
        view = "installed"

    try:
        number = max(1, int(query.get("page", ["1"])[0]))
    except (TypeError, ValueError):
        number = 1

    conditions = []
    params = []

    if term:
        conditions.append("instr(lower(name),lower(?))>0")
        params.append(term)
    if library != "all":
        conditions.append("library=?")
        params.append(library)
    if view == "installed":
        conditions.append("state='installed'")
    elif view == "missing":
        conditions.append("state!='installed'")

    where = " WHERE " + " AND ".join(conditions) if conditions else ""

    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM shows" + where, params
        ).fetchone()[0]
        pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        number = min(number, pages)
        rows = conn.execute(
            "SELECT path,name,library,state FROM shows" + where
            + " ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
            params + [PER_PAGE, (number - 1) * PER_PAGE]
        ).fetchall()

    def url(page_number):
        return "/manage-themes?" + urllib.parse.urlencode({
            "q": term,
            "library": library,
            "view": view,
            "page": page_number
        })

    output = [
        '<!doctype html><html><head>',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>Theme Management - Preludarr</title>',
        '<style>', css,
        # V1.9.9 denser cards: title + file info | player | actions on one aligned row (stacks on narrow screens)
        '.management-item{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(240px,1.25fr) minmax(170px,auto);',
        'grid-template-areas:"info player actions";gap:6px 20px;align-items:center;margin:8px 0;padding:12px 16px;',
        'border:1px solid #334967;border-radius:10px;background:#16243b}',
        '.management-item .mi-info{grid-area:info;min-width:0}',
        '.management-item h3{margin:0;font-size:15.5px;line-height:1.35;overflow-wrap:anywhere}',
        '.management-item .mi-meta{margin:2px 0 0;font-size:13px}',
        '.management-item .mi-player{grid-area:player;min-width:0}',
        '.management-item audio{display:block;width:100%;margin:0}',
        '.management-item .mi-none{margin:0;font-size:13px;color:#9fb0cc}',
        '.management-item .mi-actions{grid-area:actions;display:flex;flex-direction:column;align-items:flex-start;gap:4px;font-size:14px}',
        '.management-item .mi-actions p{margin:0}',
        '@media(max-width:1180px){.management-item{grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"info actions" "player player"}}',
        '@media(max-width:600px){.management-item{grid-template-columns:1fr;grid-template-areas:"info" "player" "actions"}}',
        '.management-item a,.management-pagination a{color:#a8c8ff}',
        '.management-filters{display:flex;gap:10px;',
        'flex-wrap:wrap;align-items:center}',
        '.management-filters input{min-width:200px;flex:1}',
        '.management-pagination{display:flex;gap:20px}',
        '</style></head><body>',
        full_sidebar('manage', fixed=True),  # V1.7.3 shared sidebar, Theme Management highlighted
        '<div class="themearr-content"><main>',
        '<div class="panel"><h2>🎧 Theme Management</h2>',
        '<p class="muted">Browse installed themes, preview audio,',
        ' and select replacement OP/ED tracks.</p>',
        '<form method="get" action="/manage-themes" ',
        'class="management-filters">',
        '<input type="search" name="q" maxlength="100" ',
        'placeholder="Search titles..." value="',esc(term,quote=True),'">',
        '<select name="library">'
    ]

    for value, label in (
        ("all", "All libraries"),
        *[(k, __import__("kinds").label(k)) for k in __import__("kinds").TYPES]
    ):
        output.extend([
            '<option value="', esc(value, quote=True), '"',
            ' selected' if library == value else '',
            '>', esc(label), '</option>'
        ])

    output.append('</select><select name="view">')

    for value, label in (
        ("installed", "Indexed installed"),
        ("missing", "Indexed missing / other"),
        ("all", "All indexed")
    ):
        output.extend([
            '<option value="', value, '"',
            ' selected' if view == value else '',
            '>', esc(label), '</option>'
        ])

    output.extend([
        '</select><button type="submit">Filter</button></form>',
        '<p class="muted">', str(total),
        ' matching titles · Page ', str(number),
        ' of ', str(pages), '</p>',
        '<p class="muted">Status filters use the library index.',
        ' Actual MP3 availability is checked separately.</p>',
        '</div>'
    ])

    registered = {(label, root) for label, root in roots}  # V1.7.2: several root folders per library type

    for row in rows:
        folder = Path(row["path"])
        allowed_root = folder.parent if (row["library"], folder.parent) in registered else None
        size = 0

        if (allowed_root is not None
            and folder.parent == allowed_root
            and allowed_root.is_dir()
            and not allowed_root.is_symlink()
            and folder.is_dir()
            and not folder.is_symlink()):
            target = folder / "theme.mp3"
            if not target.is_symlink() and target.is_file():
                try:
                    size = max(0, target.stat().st_size)
                except OSError:
                    pass

        theme_url = "/themes?" + urllib.parse.urlencode({
            "path": row["path"]
        })
        preview_url = "/current-theme?" + urllib.parse.urlencode({
            "path": row["path"]
        })

        output.extend([
            '<div class="management-item">',
            '<div class="mi-info"><h3>', esc(row["name"]), '</h3>',
            '<p class="muted mi-meta">', esc(row["library"]), ' · ',
            ("MP3 available · %.2f MB" % (size / 1048576))
            if size else "No playable theme.mp3 found",
            '</p></div><div class="mi-player">'
        ])

        if size:
            output.extend([
                '<audio controls preload="none" src="',
                esc(preview_url, quote=True), '" aria-label="Play installed theme for ',
                esc(row["name"], quote=True), '"></audio>'
            ])
        else:
            output.append('<p class="mi-none">Nothing to play</p>')

        output.extend(['</div><div class="mi-actions"><a href="', esc(theme_url, quote=True),
                       '">🎵 Browse / Replace OP-ED</a>'])

        if size:
            remove_url = "/review-remove-theme?" + urllib.parse.urlencode({"path": row["path"]})
            output.extend(['<p><a style="color:#ffb6ad" href="',
                           esc(remove_url, quote=True), '">🗑 Remove (recoverable)</a></p>'])
        # Archived themes stay visible even when the live MP3 is absent.
        if allowed_root is not None and folder.parent == allowed_root:
            for archived_name in _tm_archives(folder):
                restore_url = "/review-restore-theme?" + urllib.parse.urlencode(
                    {"path": row["path"], "archive": archived_name})
                output.extend(['<p><a style="color:#a7e5c1" href="',
                               esc(restore_url, quote=True),
                               '">↩ Restore archived theme: ', esc(archived_name), '</a></p>'])
        output.append('</div></div>')

    if not rows:
        output.append(
            '<div class="panel">No matching anime found.</div>'
        )

    output.append('<div class="panel management-pagination">')

    if number > 1:
        output.extend([
            '<a href="', esc(url(number - 1), quote=True),
            '">← Previous</a>'
        ])

    output.extend([
        '<span>Page ', str(number), ' / ', str(pages), '</span>'
    ])

    if number < pages:
        output.extend([
            '<a href="', esc(url(number + 1), quote=True),
            '">Next →</a>'
        ])

    output.extend(['</div></main></div></body></html>'])

    return handler.send(200, "".join(output))


# ===== THEMEARR V0.9 RECOVERABLE REMOVAL =====
# Recovery archives are stored under each registered anime directory.
# Each confirmation is tied to the logged-in browser session and file state.
import os as _tm_os
import re as _tm_re
import stat as _tm_stat
import secrets as _tm_secrets
import hashlib as _tm_hashlib
import threading as _tm_threading
import time as _tm_time
import contextlib as _tm_contextlib
from datetime import datetime as _tm_datetime, timezone as _tm_timezone

_TM_RECOVERY_DIR = '.themearr-recovery'
_TM_ARCHIVE_PATTERN = _tm_re.compile(r'theme-\d{8}T\d{6}Z-[a-f0-9]{32}\.mp3\Z')
_TM_CONFIRMATIONS = {}
_TM_CONFIRMATION_LOCK = _tm_threading.Lock()
_TM_CONFIRMATION_TTL = 600


def _tm_fingerprint(item):
    return (item.st_dev, item.st_ino, item.st_size,
            item.st_mtime_ns, item.st_ctime_ns)


def _tm_regular(dir_fd, name):
    item = _tm_os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not _tm_stat.S_ISREG(item.st_mode) or item.st_size <= 0:
        raise ValueError('Theme must be a nonempty regular file')
    return item


def _tm_registered(folder_text, roots, db):
    if not isinstance(folder_text, str) or len(folder_text) > 2048:
        raise ValueError('Invalid anime destination')
    folder = Path(folder_text)
    root_matches = [root for _, root in roots if folder.parent == root]
    if len(root_matches) != 1:
        raise ValueError('Anime path is not within a configured library')
    with db() as connection:
        row = connection.execute(
            'SELECT name, library FROM shows WHERE path=?', (folder_text,)
        ).fetchone()
    if not row:
        raise ValueError('Anime is not registered in Preludarr')
    if not any(label == row['library'] and root == folder.parent for label, root in roots):
        raise ValueError('Registered library does not match destination')
    return folder, str(row['name'])


@_tm_contextlib.contextmanager
def _tm_recovery_fd(folder_fd, create=False):
    if create:
        try:
            _tm_os.mkdir(_TM_RECOVERY_DIR, 0o700, dir_fd=folder_fd)
        except FileExistsError:
            pass
    fd = _tm_os.open(
        _TM_RECOVERY_DIR,
        _tm_os.O_RDONLY | _tm_os.O_DIRECTORY | _tm_os.O_NOFOLLOW,
        dir_fd=folder_fd
    )
    try:
        stat_data = _tm_os.fstat(fd)
        if not _tm_stat.S_ISDIR(stat_data.st_mode):
            raise ValueError('Recovery location is not a directory')
        if _tm_stat.S_IMODE(stat_data.st_mode) != 0o700:
            raise ValueError('Recovery location must have mode 700')
        yield fd
    finally:
        _tm_os.close(fd)


def _tm_archives(folder):
    """List recovery names safely, without changing any files."""
    try:
        fd = _tm_os.open(folder, _tm_os.O_RDONLY | _tm_os.O_DIRECTORY | _tm_os.O_NOFOLLOW)
    except OSError:
        return []
    try:
        try:
            with _tm_recovery_fd(fd) as archive_fd:
                with _tm_os.scandir(archive_fd) as entries:
                    names = [entry.name for entry in entries
                             if _TM_ARCHIVE_PATTERN.fullmatch(entry.name)
                             and entry.is_file(follow_symlinks=False)]
            return sorted(names, reverse=True)[:30]
        except (OSError, ValueError):
            return []
    finally:
        _tm_os.close(fd)


def _tm_session_id(session):
    if not isinstance(session, str) or not session:
        raise ValueError('Login session required')
    return _tm_hashlib.sha256(session.encode()).hexdigest()


def _tm_issue(operation, folder, archive, fingerprint, session):
    value = _tm_secrets.token_urlsafe(32)
    now = _tm_time.monotonic()
    record = (now + _TM_CONFIRMATION_TTL,
              operation, str(folder), archive, fingerprint,
              _tm_session_id(session))
    with _TM_CONFIRMATION_LOCK:
        for key, current in list(_TM_CONFIRMATIONS.items()):
            if current[0] <= now:
                _TM_CONFIRMATIONS.pop(key, None)
        if len(_TM_CONFIRMATIONS) >= 1024:
            raise ValueError('Too many pending confirmations; try again later')
        _TM_CONFIRMATIONS[value] = record
    return value


def _tm_consume(token, operation, folder, archive, session):
    with _TM_CONFIRMATION_LOCK:
        record = _TM_CONFIRMATIONS.pop(token, None)
    if not record:
        raise ValueError('Confirmation expired or already used')
    expires, saved_operation, saved_path, saved_archive, fingerprint, saved_session = record
    if (expires <= _tm_time.monotonic()
        or saved_operation != operation
        or saved_path != str(folder)
        or saved_archive != archive
        or not _tm_secrets.compare_digest(saved_session, _tm_session_id(session))):
        raise ValueError('Confirmation does not match your session or selection')
    return fingerprint


def _tm_confirm_html(css, title, anime, destination, token, path, archive):
    esc = html.escape
    fields = (
        '<input type="hidden" name="path" value="' + esc(str(path), quote=True) + '">'
        '<input type="hidden" name="token" value="' + esc(token, quote=True) + '">'
        '<input type="hidden" name="archive" value="' + esc(archive, quote=True) + '">'
    )
    if destination == '/confirm-remove-theme':
        explanation = 'The MP3 will move into a hidden recovery folder inside this anime. It will not be permanently deleted.'
        button = 'Move to Recovery'
    else:
        explanation = 'Restore the selected archived MP3 as theme.mp3. This will not replace an existing theme.'
        button = 'Restore Theme'
    return (
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + esc(title) + ' - Preludarr</title><style>' + css + '</style></head><body>'
        '<main style="max-width:850px;margin:48px auto"><div class="panel">'
        '<h2>' + esc(title) + '</h2><p><strong>Anime:</strong> ' + esc(anime) + '</p>'
        '<p>' + esc(explanation) + '</p>'
        '<form method="post" action="' + destination + '">' + fields +
        '<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">' +
        '<a href="/manage-themes?view=all" style="display:inline-block;padding:10px 18px;background:#30415f;border:1px solid #65799b;border-radius:7px;color:#fff;text-decoration:none;font-weight:600">← Back</a>' +
        '<button type="submit">' + button + '</button></div></form>'
        '</div></main></body></html>'
    )


def review_action(handler, roots, db, css, safe_open, verify_identity, session, operation):
    if operation not in ('remove', 'restore'):
        return handler.send(404, 'Not found', 'text/plain')
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    selected = query.get('path', [''])[0]
    archive = query.get('archive', [''])[0]
    try:
        folder, anime = _tm_registered(selected, roots, db)
        with safe_open(folder) as folder_fd:
            verify_identity(folder, folder_fd)
            if operation == 'remove':
                archive = ''
                identity = _tm_fingerprint(_tm_regular(folder_fd, 'theme.mp3'))
            else:
                if not _TM_ARCHIVE_PATTERN.fullmatch(archive):
                    raise ValueError('Invalid recovery file selection')
                with _tm_recovery_fd(folder_fd) as archive_fd:
                    identity = _tm_fingerprint(_tm_regular(archive_fd, archive))
                try:
                    _tm_os.stat('theme.mp3', dir_fd=folder_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError('Theme already exists. Restore will not overwrite it.')
            token = _tm_issue(operation, folder, archive, identity, session)
    except (OSError, ValueError) as exc:
        return handler.send(409, 'Unable to review theme action: ' + str(exc), 'text/plain')
    title = 'Confirm Remove Theme' if operation == 'remove' else 'Confirm Restore Theme'
    destination = '/confirm-remove-theme' if operation == 'remove' else '/confirm-restore-theme'
    return handler.send(200, _tm_confirm_html(css, title, anime, destination, token, folder, archive))


def perform_action(handler, roots, db, safe_open, verify_identity,
                   manual_lock, session, form, operation, redirect, event=None):
    if operation not in ('remove', 'restore'):
        return handler.send(404, 'Not found', 'text/plain')
    selected = form.get('path', [''])[0]
    token = form.get('token', [''])[0]
    archive = form.get('archive', [''])[0]
    if not _tm_re.fullmatch(r'[A-Za-z0-9_-]{30,100}', token):
        return handler.send(409, 'Missing or invalid confirmation', 'text/plain')
    if operation == 'remove':
        archive = ''
    elif not _TM_ARCHIVE_PATTERN.fullmatch(archive):
        return handler.send(409, 'Invalid archive name', 'text/plain')
    try:
        folder, anime = _tm_registered(selected, roots, db)
        expected = _tm_consume(token, operation, folder, archive, session)
    except (OSError, ValueError) as exc:
        return handler.send(409, 'Theme action rejected: ' + str(exc), 'text/plain')

    if not manual_lock.acquire(blocking=False):
        return handler.send(409, 'Another installation or theme change is running', 'text/plain')
    try:
        with safe_open(folder) as folder_fd:
            verify_identity(folder, folder_fd)
            if operation == 'remove':
                current = _tm_regular(folder_fd, 'theme.mp3')
                if _tm_fingerprint(current) != expected:
                    raise ValueError('Theme changed since confirmation')
                with _tm_recovery_fd(folder_fd, create=True) as recovery_fd:
                    # Unique 128-bit archive name. Atomic rename stays in the same anime folder.
                    timestamp = _tm_datetime.now(_tm_timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                    unique_name = 'theme-' + timestamp + '-' + _tm_secrets.token_hex(16) + '.mp3'
                    try:
                        _tm_os.stat(unique_name, dir_fd=recovery_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise ValueError('Unexpected recovery archive collision')
                    verify_identity(folder, folder_fd)
                    if _tm_fingerprint(_tm_regular(folder_fd, 'theme.mp3')) != expected:
                        raise ValueError('Theme changed during removal')
                    _tm_os.rename('theme.mp3', unique_name,
                                  src_dir_fd=folder_fd, dst_dir_fd=recovery_fd)
                    archived = _tm_regular(recovery_fd, unique_name)
                    if archived.st_dev != current.st_dev or archived.st_ino != current.st_ino:
                        raise ValueError('Recovery file identity mismatch')
                state, detail, label = ('missing', 'Theme moved to recovery', 'removed')
            else:
                with _tm_recovery_fd(folder_fd) as recovery_fd:
                    saved = _tm_regular(recovery_fd, archive)
                    if _tm_fingerprint(saved) != expected:
                        raise ValueError('Archived theme changed since confirmation')
                    try:
                        _tm_os.stat('theme.mp3', dir_fd=folder_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise ValueError('Theme already exists; nothing was overwritten')
                    # Set Plex/Windows-compatible ownership and permissions before publication.
                    permission_fd = _tm_os.open(archive, _tm_os.O_RDONLY | _tm_os.O_NOFOLLOW,
                                                dir_fd=recovery_fd)
                    try:
                        if not _tm_stat.S_ISREG(_tm_os.fstat(permission_fd).st_mode):
                            raise ValueError('Unsafe archive')
                        __import__('permissions').set_theme_owner(permission_fd)  # 1.12: PUID/PGID aware
                    finally:
                        _tm_os.close(permission_fd)
                    verify_identity(folder, folder_fd)
                    # Hard-link creates only if destination doesn't exist; never overwrite.
                    _tm_os.link(archive, 'theme.mp3', src_dir_fd=recovery_fd,
                                dst_dir_fd=folder_fd, follow_symlinks=False)
                    restored = _tm_regular(folder_fd, 'theme.mp3')
                    if restored.st_dev != fixed.st_dev or restored.st_ino != fixed.st_ino:
                        raise ValueError('Restored theme identity mismatch; original kept in recovery')
                    _tm_os.unlink(archive, dir_fd=recovery_fd)
                state, detail, label = ('installed', 'Theme restored from recovery', 'restored')

        # File operation already succeeded. Recovery remains available even if indexing fails.
        try:
            with db() as connection:
                connection.execute(
                    'UPDATE shows SET state=?, detail=?, updated=? WHERE path=?',
                    (state, detail, _tm_time.strftime('%Y-%m-%dT%H:%M:%S+00:00', _tm_time.gmtime()), selected)  # V1.9.14: UTC ISO like every other writer
                )
        except Exception:
            return handler.send(500, 'Theme file operation succeeded, but the library index could not update. '
                                'Check Theme Management before retrying.', 'text/plain')
        if event:
            try:
                event(anime, label, detail)
            except Exception:
                pass
        destination = '/manage-themes?' + urllib.parse.urlencode(
            {'view': 'all', 'q': anime, 'result': label}
        )
        return redirect(handler, destination)
    except (OSError, ValueError) as exc:
        return handler.send(409, 'Theme action cancelled: ' + str(exc), 'text/plain')
    finally:
        manual_lock.release()
# ===== END THEMEARR V0.9 RECOVERABLE REMOVAL =====
