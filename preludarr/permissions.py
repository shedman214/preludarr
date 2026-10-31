# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → Media Management → Permissions (Themearr V1.9.1, Sonarr-style).

Off by default. When on, Themearr sets the chosen mode, owner and group ONLY on what it creates itself:
installed theme.mp3 files and folders it creates for new media. It never touches any other file or folder.
File mode = folder mode without execute bits (e.g. folder 775 → file 664). Errors are logged, never fatal.

V1.9.1 (user 2026-10-26): Sonarr-like layout (chmod presets + Umask/Folder/File table), chown User added
(as root it can set any owner; since 1.12 it normally runs as PUID:PGID), defaults nobody:users = 99:100 (Unraid),
and a one-time "Fix existing theme.mp3 files" tool: read-only preview first, then a confirmed fix that only
changes theme.mp3 files inside registered title folders and writes a CSV undo record to /config/logs.
"""
import csv
import grp
import html
import json
import os
import pwd
import re
import stat
import threading
import time
import urllib.parse
from pathlib import Path

FILE = 'themearr_permissions.json'
DEFAULTS = {'enabled': False, 'folder_mode': '775', 'user': '99', 'group': '100'}
PRESETS = ('755', '775', '777')
MAX_EXAMPLES = 50
_LOCK = threading.Lock()
_STATE = {'preview': None, 'fix': None}


def _e(value):
    return html.escape(str(value), quote=True)


def _id_ok(value):
    return value == '' or (re.fullmatch(r'\d{1,5}', value) is not None and int(value) <= 65535)


def settings(config):
    data = dict(DEFAULTS)
    try:
        saved = json.loads((Path(config) / FILE).read_text())
        data['enabled'] = saved.get('enabled') is True
        if re.fullmatch(r'7[0-7]{2}', str(saved.get('folder_mode', ''))):
            data['folder_mode'] = saved['folder_mode']
        for key in ('user', 'group'):
            if key in saved and _id_ok(str(saved[key])):
                data[key] = str(saved[key])
    except (OSError, ValueError, TypeError):
        pass
    return data


def file_mode(folder_mode):
    return format(int(folder_mode, 8) & 0o666, '03o')


def umask(folder_mode):
    return format(0o777 & ~int(folder_mode, 8), '04o')


def _rwx(mode, is_dir):
    out = 'd' if is_dir else '-'
    for digit in mode:
        n = int(digit)
        out += ('r' if n & 4 else '-') + ('w' if n & 2 else '-') + ('x' if n & 1 else '-')
    return out


def _ids(s):
    return (int(s['user']) if s['user'] else -1, int(s['group']) if s['group'] else -1)


def _set_fd(fd, mode, uid, gid):
    os.fchmod(fd, mode)
    if uid >= 0 or gid >= 0:
        os.fchown(fd, uid, gid)


def _apply(config, path, is_dir):
    s = settings(config)
    if not s['enabled']:
        return
    try:
        # O_NOFOLLOW: a symlink is never followed (nothing outside the folder can be changed).
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if is_dir else 0))
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode) != is_dir:
                return
            _set_fd(fd, int(s['folder_mode'] if is_dir else file_mode(s['folder_mode']), 8), *_ids(s))
        finally:
            os.close(fd)
    except (OSError, ValueError) as error:
        print('Permissions not applied (' + type(error).__name__ + ')', flush=True)


def apply_file(config, path):
    _apply(config, path, False)


def apply_dir(config, path):
    _apply(config, path, True)


def _resolve(raw, kind):
    """Number, or a name known inside this container. Returns the number as text ('' = leave unchanged)."""
    raw = raw.strip()
    if raw == '' or _id_ok(raw):
        if raw and int(raw) > 65535:
            raise ValueError('Permissions: ' + kind + ' number must be 0–65535.')
        return raw
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,31}', raw):
        raise ValueError('Permissions: enter a ' + kind + ' name or number.')
    if raw == 'nobody':
        # Inside the container "nobody" is 65534; on Unraid the media owner nobody is 99.
        raise ValueError('Permissions: for Unraid’s “nobody” enter the number 99.')
    try:
        return str(pwd.getpwnam(raw).pw_uid if kind == 'user' else grp.getgrnam(raw).gr_gid)
    except KeyError:
        raise ValueError('Permissions: “' + raw + '” is not known inside Preludarr — use the ' + kind + ' number instead.') from None


def theme_owner():
    """(uid, gid) every installed theme.mp3 gets (1.12, non-root runtime).

    PUID/PGID when set (the container runs as that user); else 99:100 (Unraid nobody:users) when running as root, as before 1.12;
    else the user Preludarr runs as, so nothing needs changing."""
    try:
        puid = int(os.environ.get('PUID', '').strip() or -1)
        if puid > 0 and os.geteuid() in (0, puid):
            return puid, int(os.environ.get('PGID', '').strip() or os.getegid())
    except ValueError:
        pass
    return (99, 100) if os.geteuid() == 0 else (os.geteuid(), os.getegid())


def set_theme_owner(fd, mode=0o664):
    """Give an installed theme its owner and mode, then check them; raises ValueError if they did not stick."""
    uid, gid = theme_owner()
    st = os.fstat(fd)
    if (st.st_uid, st.st_gid) != (uid, gid):
        try:
            os.fchown(fd, uid, gid)
        except PermissionError:
            raise ValueError('Preludarr runs as uid %d and cannot give theme.mp3 to %d:%d — set PUID/PGID to the owner of your media.' % (os.geteuid(), uid, gid)) from None
    os.fchmod(fd, mode)
    done = os.fstat(fd)
    if (done.st_uid, done.st_gid, stat.S_IMODE(done.st_mode)) != (uid, gid, mode):
        raise ValueError('Theme permissions verification failed')


def _who(config):
    try:
        return int(os.geteuid())
    except OSError:
        return -1


# --------------------------------------------------------------------------- Fix existing ----
def _wanted(s):
    uid, gid = _ids(s)
    return int(file_mode(s['folder_mode']), 8), uid, gid


def _scan(app, s, scope='all'):
    """Read-only: registered title folders that are direct children of a root, regular theme.mp3 only."""
    mode, uid, gid = _wanted(s)
    roots = [root for _, root in app.ROOTS]
    with app.db() as c:
        paths = [r['path'] for r in c.execute('SELECT path FROM shows ORDER BY path')]
    checked, wrong = 0, []
    for raw in paths:
        folder = Path(raw)
        if not any(folder.parent == root for root in roots):
            continue
        try:
            st = os.lstat(folder / 'theme.mp3')
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        checked += 1
        bad_mode = stat.S_IMODE(st.st_mode) != mode
        bad_owner = (uid >= 0 and st.st_uid != uid) or (gid >= 0 and st.st_gid != gid)
        if (bad_mode and scope == 'all') or bad_owner:
            wrong.append({'path': str(folder / 'theme.mp3'), 'mode': format(stat.S_IMODE(st.st_mode), '03o'),
                          'uid': st.st_uid, 'gid': st.st_gid, 'owner': bad_owner, 'bad_mode': bad_mode})
    return checked, wrong


def preview(app):
    s = settings(app.CONFIG)
    checked, wrong = _scan(app, s)
    wrong.sort(key=lambda w: (not w['owner'], w['path']))  # wrong owner/group first
    result = {'at': time.strftime('%Y-%m-%d %H:%M'), 'checked': checked, 'wrong': wrong, 'target': _target_text(s),
              'owner': sum(1 for w in wrong if w['owner']), 'mode_only': sum(1 for w in wrong if not w['owner'])}
    with _LOCK:
        _STATE['preview'] = result
    return result


def _target_text(s):
    fm = file_mode(s['folder_mode'])
    return fm + ' ' + _rwx(fm, False) + ' · owner ' + (s['user'] or 'unchanged') + ' · group ' + (s['group'] or 'unchanged')


def _owner_text(s):
    return 'owner ' + (s['user'] or 'unchanged') + ' · group ' + (s['group'] or 'unchanged') + ' (mode unchanged)'


def _fix_worker(app, s, scope):
    mode, uid, gid = _wanted(s)
    state = _STATE['fix']
    log_dir = Path(app.CONFIG) / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / ('permissions-fix-' + time.strftime('%Y%m%d-%H%M%S') + '.csv')
    state['log'] = log_path.name
    try:
        _, wrong = _scan(app, s, scope)
        state['total'] = len(wrong)
        fd_log = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd_log, 'w', newline='') as out:
            w = csv.writer(out)
            w.writerow(['path', 'old_mode', 'old_uid', 'old_gid', 'new_mode', 'new_uid', 'new_gid', 'result'])
            for item in wrong:
                result = 'changed'
                try:
                    fd = os.open(item['path'], os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        st = os.fstat(fd)
                        if not stat.S_ISREG(st.st_mode):
                            result = 'skipped: not a regular file'
                        else:
                            old = (format(stat.S_IMODE(st.st_mode), '03o'), st.st_uid, st.st_gid)
                            if scope == 'all':
                                _set_fd(fd, mode, uid, gid)
                            else:  # owner/group only — the file's mode is left as it is
                                os.fchown(fd, uid, gid)
                            item.update(mode=old[0], uid=old[1], gid=old[2])
                    finally:
                        os.close(fd)
                except OSError as error:
                    result = 'error: ' + type(error).__name__
                if result == 'changed':
                    state['changed'] += 1
                else:
                    state['errors'] += 1
                w.writerow([item['path'], item['mode'], item['uid'], item['gid'], format(mode, '03o') if scope == 'all' else 'unchanged',
                            uid if uid >= 0 else 'unchanged', gid if gid >= 0 else 'unchanged', result])
                state['done'] += 1
        app.event('Permissions', 'fixed', str(state['changed']) + ' theme.mp3 file(s) set to ' + state['target'] +
                  (' · ' + str(state['errors']) + ' skipped' if state['errors'] else '') + ' · undo record logs/' + log_path.name)
    except Exception as error:  # never let the worker die silently
        state['errors'] += 1
        state['message'] = 'Stopped: ' + type(error).__name__
    finally:
        state['running'] = False
        state['finished'] = time.strftime('%Y-%m-%d %H:%M')
        with _LOCK:
            _STATE['preview'] = None


def start_fix(app, scope='all'):
    s = settings(app.CONFIG)
    if not s['enabled']:
        raise ValueError('Permissions: turn on Set Permissions and save before fixing existing files.')
    with _LOCK:
        if _STATE['fix'] and _STATE['fix'].get('running'):
            raise ValueError('Permissions: a fix is already running.')
        _STATE['fix'] = {'running': True, 'done': 0, 'total': 0, 'changed': 0, 'errors': 0, 'log': '', 'message': '',
                         'target': _target_text(s) if scope == 'all' else _owner_text(s), 'started': time.strftime('%Y-%m-%d %H:%M')}
    threading.Thread(target=_fix_worker, args=(app, s, scope), name='themearr-permissions-fix', daemon=True).start()


def status():
    with _LOCK:
        fix = dict(_STATE['fix']) if _STATE['fix'] else None
    return {'fix': fix}


# ----------------------------------------------------------------------------------- UI ----
def settings_html(app, notice=''):
    s = settings(app.CONFIG)
    fm = file_mode(s['folder_mode'])
    custom = s['folder_mode'] not in PRESETS
    uid = _who(app.CONFIG)
    dim = '' if s['enabled'] else ' pm-off'
    opts = ''.join('<option value="' + m + '"' + (' selected' if m == s['folder_mode'] else '') + '>' + m + ' — ' + _rwx(m, True) + '</option>'
                   for m in PRESETS) + '<option value="custom"' + (' selected' if custom else '') + '>Custom…</option>'
    parts = ['<section class="panel" id="permissions"><h2>Permissions</h2>',
             ('<p class="notice" role="status">' + _e(notice) + '</p>' if notice else ''),
             '<form method="post" action="/media-management/permissions" autocomplete="off" class="pm-form' + dim + '" id="pm-form">',
             # Set Permissions
             '<div class="pm-row"><span class="pm-label">Set Permissions</span><div class="pm-ctl">',
             '<label class="pm-check"><input type="checkbox" name="enabled" value="1" id="pm-enabled"', ' checked' if s['enabled'] else '',
             '> Should chmod/chown be run when Preludarr installs a theme.mp3 or creates a folder?</label>',
             '<p class="pm-warn">If you’re unsure what these settings do, do not alter them.</p></div></div>',
             # chmod Folder
             '<div class="pm-row pm-dep"><label class="pm-label" for="pm-preset">chmod Folder</label><div class="pm-ctl">',
             '<select id="pm-preset">', opts, '</select>',
             '<input name="folder_mode" id="pm-mode" maxlength="3" autocomplete="off" data-bwignore="true" data-lpignore="true" data-1p-ignore="true" data-form-type="other" pattern="7[0-7]{2}" inputmode="numeric" aria-label="Custom folder mode" value="',
             _e(s['folder_mode']), '"', '' if custom else ' hidden', '>',
             '<table class="pm-table" aria-label="Resulting permissions"><tr><th>Umask</th><td id="pm-umask">', umask(s['folder_mode']), '</td><td></td></tr>',
             '<tr><th>Folder</th><td id="pm-dir">', s['folder_mode'], '</td><td><code id="pm-dir-rwx">', _rwx(s['folder_mode'], True), '</code></td></tr>',
             '<tr><th>File</th><td id="pm-file">', fm, '</td><td><code id="pm-file-rwx">', _rwx(fm, False), '</code></td></tr></table>',
             '<p class="pm-help">Octal, applied to theme.mp3 files Preludarr installs and folders it creates (files without execute bits). '
             'The owner always keeps full access (7xx).</p></div></div>',
             # chown User
             '<div class="pm-row pm-dep"><label class="pm-label" for="pm-user">chown User</label><div class="pm-ctl">',
             '<input name="user" id="pm-user" maxlength="32" autocomplete="off" data-bwignore="true" data-lpignore="true" data-1p-ignore="true" data-form-type="other" value="', _e(s['user']), '" placeholder="blank = leave the owner unchanged">',
             '<p class="pm-help">User name or uid. On Unraid, media belongs to <code>99</code> (nobody).</p>',
             ('<p class="pm-ok">Preludarr runs as root, so it can set the owner (Sonarr can’t).</p>' if uid == 0 else
              '<p class="pm-help">Preludarr runs as uid ' + _e(uid) + ' (PUID), so new themes already belong to it. Setting a different owner only works for files Preludarr owns; leave this blank or use ' + _e(uid) + '.</p>'),
             '</div></div>',
             # chown Group
             '<div class="pm-row pm-dep"><label class="pm-label" for="pm-group">chown Group</label><div class="pm-ctl">',
             '<input name="group" id="pm-group" maxlength="32" autocomplete="off" data-bwignore="true" data-lpignore="true" data-1p-ignore="true" data-form-type="other" value="', _e(s['group']), '" placeholder="blank = leave the group unchanged">',
             '<p class="pm-help">Group name or gid. Use the gid for remote file systems. On Unraid the media group is <code>100</code> (users).</p>',
             '<p class="pm-warn">Only what Preludarr creates is changed — never your videos or other files.</p></div></div>',
             '<div class="pm-row"><span class="pm-label"></span><div class="pm-ctl"><button type="submit">Save</button></div></div></form>',
             fix_html(app, s), STYLE, SCRIPT, '</section>']
    return ''.join(parts)


def fix_html(app, s):
    with _LOCK:
        pre = _STATE['preview']
        fix = dict(_STATE['fix']) if _STATE['fix'] else None
    parts = ['<div class="pm-fix" id="permissions-fix"><h3>Fix existing theme.mp3 files</h3>',
             '<p class="pm-help">Checks the theme.mp3 in every title folder Preludarr knows and lists the ones that don’t match the settings above. '
             'Fixing changes <strong>only those theme.mp3 files</strong> — never folders, videos or anything else — and keeps an undo record in '
             '<code>/config/logs</code>.</p>']
    if fix:
        if fix['running']:
            parts += ['<p class="notice" role="status" id="pm-fix-status" data-running="1">Fixing… ', str(fix['done']), ' / ', str(fix['total']), '</p>']
        else:
            parts += ['<p class="notice" role="status" id="pm-fix-status">Last fix (', _e(fix.get('finished', '')), '): ', str(fix['changed']),
                      ' changed', (', ' + str(fix['errors']) + ' skipped' if fix['errors'] else ''), ' → ', _e(fix['target']),
                      ('. Undo record: logs/' + _e(fix['log']) if fix['log'] else ''), (' ' + _e(fix['message']) if fix['message'] else ''), '</p>']
    parts += ['<form method="post" action="/media-management/permissions-preview" class="pm-inline"><button type="submit" class="btn ghost">Preview</button></form>']
    if pre:
        n = len(pre['wrong'])
        parts += ['<p role="status"><strong>', str(n), '</strong> of ', str(pre['checked']), ' theme.mp3 file(s) differ from ', _e(pre['target']),
                  ' <small class="muted">(checked ', _e(pre['at']), ')</small></p>',
                  '<ul class="pm-sum"><li><strong>', str(pre['owner']), '</strong> with the wrong owner or group</li>',
                  '<li><strong>', str(pre['mode_only']), '</strong> with the right owner and group — only the mode differs</li></ul>']
        if n:
            parts += ['<p class="muted">Files with the wrong owner or group are listed first.</p><div class="pm-list"><table><tr><th>theme.mp3</th><th>Mode</th><th>Owner</th><th>Group</th></tr>']
            for item in pre['wrong'][:MAX_EXAMPLES]:
                parts += ['<tr><td><code>', _e(item['path']), '</code></td><td>', _e(item['mode']), '</td><td>', _e(item['uid']), '</td><td>', _e(item['gid']), '</td></tr>']
            parts += ['</table></div>', ('<p class="muted">… and ' + str(n - MAX_EXAMPLES) + ' more.</p>' if n > MAX_EXAMPLES else '')]
            if not s['enabled']:
                parts += ['<p class="pm-warn">Turn on Set Permissions and save first.</p>']
            else:
                own = pre['owner']
                parts += ['<form method="post" action="/media-management/permissions-fix" class="pm-confirm">',
                          '<fieldset class="pm-scope"><legend>What to fix</legend>',
                          '<label class="pm-check"><input type="radio" name="scope" value="owner"', ' checked' if own else ' disabled',
                          '> Owner and group only — ', str(own), ' file(s); modes stay as they are</label>',
                          '<label class="pm-check"><input type="radio" name="scope" value="all"', '' if own else ' checked',
                          '> Mode, owner and group — all ', str(n), ' file(s) become ', _e(pre['target']), '</label></fieldset>',
                          '<label class="pm-check"><input type="checkbox" name="confirm" value="1" required> I understand these theme.mp3 files will be changed '
                          '(an undo record is kept)</label>',
                          '<button type="submit"', ' disabled' if fix and fix['running'] else '', '>Fix files</button></form>']
    parts.append('</div>')
    return ''.join(parts)


STYLE = ('<style>#permissions [hidden]{display:none!important}#permissions .pm-row{display:grid;grid-template-columns:200px minmax(0,1fr);gap:24px;align-items:start;margin:14px 0}'
         '#permissions .pm-label{text-align:right;font-weight:700;padding-top:9px}'
         '#permissions .pm-ctl input:not([type=checkbox]),#permissions .pm-ctl select{padding:10px;background:#101c30;color:#fff;border:1px solid #647da0;'
         'border-radius:7px;width:min(420px,100%);box-sizing:border-box;display:block}'
         '#permissions #pm-mode{margin-top:8px}#permissions .pm-check{display:flex;gap:8px;align-items:center;padding-top:8px}'
         '#permissions .pm-table{margin:10px 0 0 16px;border-collapse:collapse}#permissions .pm-table th{text-align:left;padding:2px 24px 2px 0}'
         '#permissions .pm-table td{padding:2px 20px 2px 0;font-variant-numeric:tabular-nums}'
         '#permissions .pm-help{color:var(--muted,#adbed9);margin:8px 0 0}#permissions .pm-warn{color:#ffd36b;margin:8px 0 0}'
         '#permissions .pm-ok{color:#8fe3a8;margin:8px 0 0}#permissions .pm-off .pm-dep{opacity:.55}'
         '#permissions .pm-fix{margin-top:22px;padding-top:16px;border-top:1px solid var(--line,#3b4b6b)}'
         '#permissions .pm-inline{display:inline-block;margin:6px 0}#permissions .pm-list{max-height:320px;overflow:auto;margin:8px 0}'
         '#permissions .pm-list td,#permissions .pm-list th{text-align:left;padding:4px 12px 4px 0}#permissions .pm-list code{overflow-wrap:anywhere}'
         '#permissions .pm-sum{margin:4px 0 8px;padding-left:20px}#permissions .pm-scope{border:1px solid var(--line,#3b4b6b);border-radius:8px;padding:8px 14px;margin:0}'
         '#permissions .pm-scope legend{font-weight:700;padding:0 6px}'
         '#permissions .pm-confirm{display:flex;flex-direction:column;gap:10px;align-items:flex-start;margin-top:10px}'
         '@media(max-width:760px){#permissions .pm-row{grid-template-columns:1fr;gap:6px}#permissions .pm-label{text-align:left;padding-top:0}}</style>')

SCRIPT = ('<script>(function(){const sel=document.getElementById("pm-preset"),i=document.getElementById("pm-mode");if(!sel||!i)return;'
          'const r=(m,d)=>(d?"d":"-")+[...m].map(c=>{const n=+c;return(n&4?"r":"-")+(n&2?"w":"-")+(n&1?"x":"-")}).join("");'
          'const t=(id,v)=>{document.getElementById(id).textContent=v};'
          'function show(v){if(!/^7[0-7]{2}$/.test(v))return;const f=[...v].map(c=>(+c&6)).join("");'
          't("pm-umask",(0o777&~parseInt(v,8)).toString(8).padStart(4,"0"));t("pm-dir",v);t("pm-dir-rwx",r(v,1));t("pm-file",f);t("pm-file-rwx",r(f,0))}'
          'sel.addEventListener("change",()=>{if(sel.value==="custom"){i.hidden=false;i.focus()}else{i.hidden=true;i.value=sel.value}show(i.value)});'
          'i.addEventListener("input",()=>show(i.value));'
          'const en=document.getElementById("pm-enabled"),form=document.getElementById("pm-form");'
          'if(en)en.addEventListener("change",()=>form.classList.toggle("pm-off",!en.checked));'
          'const st=document.getElementById("pm-fix-status");if(st&&st.dataset.running){let pmT=0;const tick=async()=>{try{const x=await fetch("/media-management/permissions-status",{cache:"no-store"});'
          'if(x.status===401||(x.redirected&&new URL(x.url).pathname==="/login")){clearInterval(pmT);st.textContent="Signed out — reload to sign in again.";return}'
          'const j=await x.json();if(!j.fix||!j.fix.running){location.reload();return}st.textContent="Fixing… "+j.fix.done+" / "+j.fix.total}catch(e){}};pmT=setInterval(tick,1500)}'
          '})();</script>')


# --------------------------------------------------------------------------------- POST ----
def _form(handler):
    size = int(handler.headers.get('Content-Length', '0') or 0)
    if not 0 <= size <= 1024:
        raise ValueError('Permissions: invalid form.')
    body = handler.rfile.read(size).decode('utf-8', 'strict') if size else ''
    form = urllib.parse.parse_qs(body, keep_blank_values=True, max_num_fields=6)
    return lambda k: form.get(k, [''])[0].strip()


def post(app, handler, redirect):
    route = urllib.parse.urlsplit(handler.path).path
    try:
        f = _form(handler)
        if route == '/media-management/permissions-preview':
            p = preview(app)
            msg = 'Permissions preview: ' + str(len(p['wrong'])) + ' of ' + str(p['checked']) + ' theme.mp3 file(s) differ.'
        elif route == '/media-management/permissions-fix':
            if f('confirm') != '1':
                raise ValueError('Permissions: tick the box to confirm the fix.')
            scope = f('scope')
            if scope not in ('owner', 'all'):
                raise ValueError('Permissions: choose what to fix.')
            start_fix(app, scope)
            msg = 'Permissions: fixing existing theme.mp3 files…'
        else:
            mode = f('folder_mode')
            if not re.fullmatch(r'7[0-7]{2}', mode):
                raise ValueError('Permissions: folder mode must be three digits starting with 7 (the owner keeps full access), e.g. 775.')
            data = {'enabled': f('enabled') == '1', 'folder_mode': mode,
                    'user': _resolve(f('user'), 'user'), 'group': _resolve(f('group'), 'group')}
            path = Path(app.CONFIG) / FILE
            tmp = path.with_name('.' + FILE + '.part')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out)
            os.replace(tmp, path)
            with _LOCK:
                _STATE['preview'] = None  # settings changed → an old preview would be misleading
            msg = ('Permissions saved: ' + ('on — folder ' + mode + ', file ' + file_mode(mode) + ', owner ' + (data['user'] or 'unchanged') +
                                            ', group ' + (data['group'] or 'unchanged') if data['enabled'] else 'off'))
    except (ValueError, OSError, UnicodeError) as error:
        msg = str(error) if isinstance(error, ValueError) and str(error).startswith('Permissions') else 'Permissions: not saved — settings could not be written.'
    return redirect(handler, '/media-management?' + urllib.parse.urlencode({'notice': msg}) + '#permissions')
