# SPDX-License-Identifier: GPL-3.0-or-later
"""Anime track preferences: a title overrides its root, which overrides the global default.

Profiles affect automatic installs and the reviewed bulk queue, not an explicit
manual track selection. They never remove or replace an existing theme.mp3.
"""
import hashlib
import html
import re
import urllib.parse
from pathlib import Path

import kinds

DEFAULT = 'first_op'
PROFILES = {
    'first_op': 'First opening (original default)',
    'latest_op': 'Latest opening',
    'first_ed': 'First ending',
    'random_op': 'Random opening (stable per title)',
    'lossless_op': 'Prefer lossless opening audio',
    'review_only': 'Review only',
    'skip': 'Do not auto-theme or queue',
}
AUTO_EXCLUDED = ('review_only', 'skip')

class ProfileChanged(RuntimeError):
    """The preferences changed after a queue preview was generated."""


def read(conn):
    return {r['path']: r['profile'] for r in conn.execute('SELECT path,profile FROM theme_profiles')}


def effective(path, saved):
    title_path = str(path)
    for key, scope in ((title_path, 'Title'), (str(Path(title_path).parent), 'Root folder'), ('', 'Global')):
        if key in saved:
            value = saved[key]
            # A malformed stored preference must not silently enable downloads.
            return (value if value in PROFILES else 'review_only'), scope
    return DEFAULT, 'Global default'


def for_title(app, path):
    title_path = str(path)
    with app.db() as conn:
        saved = {r['path']: r['profile'] for r in conn.execute(
            'SELECT path,profile FROM theme_profiles WHERE path IN (?,?,?)', (title_path, str(Path(title_path).parent), ''))}
        return effective(title_path, saved)


def queued_allowed(path, library, saved):
    return not kinds.is_anime(library) or effective(path, saved)[0] != 'skip'


def _sequence(track):
    raw = track.get('sequence')
    if str(raw or '').isdigit() and 0 < int(raw) < 10000:
        return int(raw)
    slug = str(track.get('slug') or '')
    match = re.match(r'(?:OP|ED)([0-9]{1,4})(?:$|[^0-9])', slug.upper())
    return int(match.group(1)) if match else 1


def select(tracks, profile, path='', *, review=False):
    """Choose a verified video ID, or None. Never rank audio by video resolution.

    The default preserves provider order. Queue review keeps the legacy ability
    to propose an ending if no opening is available; explicit ending/latest/
    lossless/random profiles apply their own preference without substitution.
    """
    if profile not in PROFILES or profile == 'skip' or (profile == 'review_only' and not review):
        return None
    valid = [t for t in tracks if re.fullmatch(r'[0-9]{1,20}', str(t.get('video_id') or ''))]
    ops = [t for t in valid if str(t.get('type') or '').startswith('OP')]
    eds = [t for t in valid if str(t.get('type') or '').startswith('ED')]
    if profile == 'first_ed':
        return min(eds, key=_sequence, default=None)
    if profile == 'latest_op':
        return max(ops, key=_sequence, default=None)
    if profile == 'random_op':
        if not ops:
            return None
        # One candidate per opening, rather than giving extra weight to songs
        # that happen to have more encodings or episode-specific versions.
        songs = {}
        for track in ops:
            songs.setdefault(str(track.get('slug') or _sequence(track)), track)
        keys = sorted(songs)
        digest = hashlib.sha256(Path(path).name.encode('utf-8')).digest()
        return songs[keys[int.from_bytes(digest[:8], 'big') % len(keys)]]
    if profile == 'lossless_op':
        lossless = [t for t in ops if str(t.get('audio_mimetype') or '').split(';')[0].strip().lower() in
                    ('audio/flac', 'audio/x-flac')]
        return next(iter(lossless or ops), None)
    return next(iter(ops or (eds if review else [])), None)


def set_profile(app, scope, path, profile):
    profile_path = str(path)
    if scope == 'global':
        if profile_path:
            raise ValueError('Invalid global profile.')
    elif scope == 'root':
        if not any(str(root) == profile_path and kinds.is_anime(kind) for kind, root in app.ROOTS):
            raise ValueError('Choose a registered anime root folder.')
    elif scope == 'title':
        roots = {str(root) for kind, root in app.ROOTS if kinds.is_anime(kind)}
        with app.db() as conn:
            row = conn.execute('SELECT library FROM shows WHERE path=?', (profile_path,)).fetchone()
        if not row or not kinds.is_anime(row['library']) or str(Path(profile_path).parent) not in roots:
            raise ValueError('Choose a registered anime title.')
    else:
        raise ValueError('Invalid profile scope.')
    if profile not in PROFILES and profile != 'inherit':
        raise ValueError('Choose a theme profile.')
    with app.db() as conn:
        if profile == 'inherit':
            conn.execute('DELETE FROM theme_profiles WHERE path=?', (profile_path,))
        else:
            conn.execute('INSERT INTO theme_profiles(path,profile) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET profile=excluded.profile',
                         (profile_path, profile))


def _e(value):
    return html.escape(str(value), quote=True)


def _form(scope, path, value, label):
    options = {'inherit': 'Use inherited default', **PROFILES}
    if scope == 'global':
        options['inherit'] = 'Use original default'
    select_html = ''.join(f'<option value="{key}"{" selected" if key == value else ""}>{_e(name)}</option>'
                          for key, name in options.items())
    return (f'<form method="post" action="/media-management/theme-profile" class="profile-form">'
            f'<input type="hidden" name="scope" value="{scope}"><input type="hidden" name="path" value="{_e(path)}">'
            f'<label class="field"><span>{_e(label)}</span><select name="profile">{select_html}</select></label>'
            '<button type="submit">Save profile</button></form>')


STYLE = ('<style>.profile-form{display:flex;align-items:end;gap:10px;flex-wrap:wrap;margin:10px 0}'
         '.profile-form select{padding:10px;background:var(--panel,#16243b);color:var(--text,#fff);border:1px solid #647da0;border-radius:7px;max-width:100%}'
         '.profile-form .field{min-width:0;flex:1 1 220px}.profile-form .field span{display:block;overflow-wrap:anywhere}</style>')


def settings_html(app, notice=''):
    from root_folders import name_of
    with app.db() as conn:
        saved = read(conn)
    parts = ['<section class="panel" id="theme-profiles"><h2>Anime Theme Profiles</h2>',
             '<p class="muted">Choose the track automatically proposed for anime. Title preferences override root folders, which override the global default. '
             'Review only waits for a manual selection or queue approval. Do not auto-theme or queue excludes the anime from both; a deliberate Theme Browser install is still possible. '
             'Prefer lossless uses source audio metadata when available, otherwise the first opening. Existing themes are never replaced.</p>']
    if notice:
        parts.append(f'<p class="notice" role="status">{_e(notice)}</p>')
    parts.append(_form('global', '', saved.get('', 'inherit'), 'Global default'))
    for kind, root in list(app.ROOTS):
        if kinds.is_anime(kind):
            parts.append(_form('root', str(root), saved.get(str(root), 'inherit'), f'{name_of(root)} ({root})'))
    parts.extend([STYLE, '</section>'])
    return ''.join(parts)


def title_html(app, path, notice=''):
    with app.db() as conn:
        saved = read(conn)
    profile, scope = effective(path, saved)
    message = f'<p class="notice" role="status">{_e(notice)}</p>' if notice else ''
    return (f'<section class="panel" id="title-theme-profile"><h3>Theme Profile</h3>{message}'
            f'<p class="muted">Active: <strong>{_e(PROFILES[profile])}</strong> ({_e(scope)}). Does not replace your current theme.</p>'
            f'{_form("title", path, saved.get(path, "inherit"), "Preference for this title")}{STYLE}</section>')


def post(app, handler, redirect):
    scope = path = ''
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 8192:
            raise ValueError('Invalid profile form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'),
                                    keep_blank_values=True, max_num_fields=3)
        if set(form) != {'scope', 'path', 'profile'} or any(len(v) != 1 for v in form.values()):
            raise ValueError('Invalid profile form.')
        scope, path, profile = (form[k][0] for k in ('scope', 'path', 'profile'))
        set_profile(app, scope, path, profile)
        message = 'Theme profile saved. Future matching uses the new preference.'
    except (ValueError, UnicodeError, OSError) as error:
        message = f'Theme profile not saved: {error}'
    if scope == 'title':
        params = urllib.parse.urlencode({'path': path, 'profile_msg': message[:220]})
        return redirect(handler, f'/themes?{params}#title-theme-profile')
    params = urllib.parse.urlencode({'notice': message[:200]})
    return redirect(handler, f'/media-management?{params}#theme-profiles')
