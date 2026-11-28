# SPDX-License-Identifier: GPL-3.0-or-later
"""Theme Studio: edit private temporary audio, compare, and confirm one exact MP3.

Sources and rendered previews expire. The normal safe installer publishes the
reviewed bytes with the usual fingerprint, ownership and replacement backup.
"""
import array
import hashlib
import html
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import custom_upload
import tasks
import theme_history

MAX_AUDIO = 40 * 1024 * 1024
MAX_SECONDS = 600
TTL = 900
MAX_STAGES = 4
PREFIX = 'preludarr-studio-'
_SESSIONS = {}
_LOCK = threading.RLock()


def _e(value):
    return html.escape(str(value or ''), quote=True)


def _url(path='', token='', **query):
    if path:
        query['path'] = str(path)
    if token:
        query['token'] = token
    return '/theme-studio?' + urllib.parse.urlencode(query)


def _owner(session):
    if not session:
        raise ValueError('Sign in again to use Theme Studio.')
    return hashlib.sha256(str(session).encode()).hexdigest()


def _cleanup(stage, config):
    stage, config = Path(stage), Path(config)
    if stage.parent == config and stage.name.startswith(PREFIX) and stage.is_dir() and not stage.is_symlink():
        shutil.rmtree(stage, ignore_errors=True)


def sweep_stale(config):
    for stage in Path(config).glob(PREFIX + '*'):
        try:
            if stage.is_dir() and not stage.is_symlink() and stage.stat().st_mtime < time.time() - 3600:
                _cleanup(stage, config)
        except OSError:
            continue


def _expire(config):
    now = time.monotonic()
    for token, item in list(_SESSIONS.items()):
        if not item['busy'] and item['expiry'] <= now:
            del _SESSIONS[token]
            _cleanup(item['stage'], config)


def _new(app, session):
    owner = _owner(session)
    with _LOCK:
        _expire(app.CONFIG)
        if len(_SESSIONS) >= MAX_STAGES:
            raise ValueError('Theme Studio is full. Close an editor or wait until it expires.')
        token = secrets.token_urlsafe(24)
        stage = Path(tempfile.mkdtemp(prefix=PREFIX, dir=app.CONFIG))
        item = {'stage': stage, 'owner': owner, 'token': token, 'expiry': time.monotonic() + TTL, 'busy': True}
        _SESSIONS[token] = item
    return item


def _find(app, token, session, claim=False):
    if not re.fullmatch(r'[A-Za-z0-9_-]{32}', token or ''):
        raise ValueError('This Studio session is unavailable.')
    owner = _owner(session)
    with _LOCK:
        _expire(app.CONFIG)
        item = _SESSIONS.get(token)
        if not item or item['owner'] != owner or item['busy']:
            raise ValueError('This Studio session expired or is busy.')
        if claim:
            item['busy'] = True
            return item
        return dict(item)


def _release(app, item, remove=False):
    with _LOCK:
        if remove:
            _SESSIONS.pop(item['token'], None)
            _cleanup(item['stage'], app.CONFIG)
        else:
            item.update(busy=False, expiry=time.monotonic() + TTL)


def _copy_fd(fd, dest):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not 1024 <= info.st_size <= MAX_AUDIO:
        raise ValueError('Audio must be a regular file between 1 KB and 40 MB.')
    with Path(dest).open('xb') as output:
        remaining = info.st_size
        while remaining:
            data = os.read(fd, min(262144, remaining))
            if not data:
                raise ValueError('Audio changed while copying.')
            output.write(data)
            remaining -= len(data)
    after = os.fstat(fd)
    if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Audio changed while copying.')


def copy_source(source, dest):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        _copy_fd(fd, dest)
    finally:
        os.close(fd)


def _title(app, path):
    folder, name = custom_upload._destination(path, app.ROOTS, app.db)
    with app.open_safe_theme_directory(folder):
        pass
    return folder, name


def start(app, session, mode, path='', slug='', video='', pending=''):
    item = _new(app, session)
    try:
        dest = item['stage'] / 'source.audio'
        initial = None
        if mode in ('upload', 'youtube'):
            if not custom_upload._enabled():
                raise ValueError('Manual audio installs are disabled.')
            reader = custom_upload.copy_for_studio if mode == 'upload' else __import__('youtube_manual').copy_for_studio
            path, metadata = reader(app.CONFIG, pending, dest)
            folder, name = _title(app, path)
        elif mode == 'current':
            folder, name = _title(app, path)
            initial = app.theme_confirmation_fingerprint(folder, 'studio', item['token'])
            with app.open_safe_theme_directory(folder) as directory:
                fd = os.open('theme.mp3', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    _copy_fd(fd, dest)
                finally:
                    os.close(fd)
            old = theme_history.current(app, folder)
            keys = ('source', 'song', 'artist', 'track', 'version', 'source_id')
            metadata = {k: old[k] for k in keys} if old else {'source': 'Existing theme', 'song': name}
        elif mode == 'animethemes':
            folder, name = _title(app, path)
            initial = app.theme_confirmation_fingerprint(folder, 'studio', item['token'])
            track = app.resolve_animethemes_track(slug, video)
            app.download_verified_theme_audio(track, dest)
            metadata = theme_history.track_metadata(track, slug)
        else:
            raise ValueError('Unknown Studio source.')
        duration, codec = custom_upload._probe(dest)
        fingerprint = app.theme_confirmation_fingerprint(folder, 'studio', item['token'])
        if initial is not None and initial != fingerprint:
            raise ValueError('The installed theme changed; start again.')
        item.update(path=str(folder), name=name, source=mode, metadata=metadata, duration=duration, codec=codec,
                    existing=(folder / 'theme.mp3').exists(), fingerprint=fingerprint, revision='', options=None, waveform=[])
        _release(app, item)
        return item['token']
    except Exception:
        _release(app, item, remove=True)
        raise


def options(fields, duration):
    """Accept only finite times, and ensure both fades fit inside the clip."""
    values = {}
    for key, default in (('start', 0), ('end', duration), ('fade_in', 0), ('fade_out', 0)):
        try:
            value = float(fields.get(key, default))
        except (TypeError, ValueError):
            raise ValueError('Enter valid numeric times.') from None
        if not math.isfinite(value) or not 0 <= value <= MAX_SECONDS:
            raise ValueError('Enter times between 0 and 600 seconds.')
        values[key] = round(value, 3)
    if duration < values['end'] <= duration + .01:
        values['end'] = duration
    length = values['end'] - values['start']
    if values['end'] > duration or length < 1:
        raise ValueError('Choose a clip at least one second long within the source.')
    if max(values['fade_in'], values['fade_out']) > 30 or values['fade_in'] + values['fade_out'] > length:
        raise ValueError('Each fade must be at most 30 seconds; together they must fit inside the clip.')
    values.update(length=length, normalize=fields.get('normalize') == '1')
    return values


def _ffmpeg(args, timeout=120):
    launcher = ('import os,resource,sys;'
                'resource.setrlimit(resource.RLIMIT_AS,(1073741824,1073741824));'
                'os.execvp(sys.argv[1],sys.argv[1:])')
    result = subprocess.run([sys.executable, '-c', launcher, 'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error'] + args,
                            capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise ValueError('The audio could not be processed.')
    return result.stdout


def _waveform(path, duration, bars=96):
    pcm = _ffmpeg(['-protocol_whitelist', 'file,pipe', '-threads', '1', '-i', str(path), '-map', '0:a:0', '-vn',
                   '-t', f'{min(MAX_SECONDS, duration + .2):.3f}', '-ac', '1', '-ar', '4000', '-threads', '1', '-f', 's16le', '-'], timeout=45)
    samples = array.array('h')
    samples.frombytes(pcm[:len(pcm)//2*2])
    if sys.byteorder != 'little':
        samples.byteswap()
    if not samples:
        raise ValueError('Could not generate waveform.')
    return [round(max((abs(x) for x in samples[i*len(samples)//bars:(i+1)*len(samples)//bars]), default=0) / 32768, 3)
            for i in range(bars)]


def _render_audio(source, dest, opts, metadata):
    filters = [f"atrim=start={opts['start']:.3f}:end={opts['end']:.3f}", 'asetpts=PTS-STARTPTS']
    if opts['normalize']:
        filters.append('loudnorm=I=-16:TP=-1.5:LRA=11')
    if opts['fade_in']:
        filters.append(f"afade=t=in:st=0:d={opts['fade_in']:.3f}")
    if opts['fade_out']:
        filters.append(f"afade=t=out:st={opts['length']-opts['fade_out']:.3f}:d={opts['fade_out']:.3f}")
    args = ['-y', '-protocol_whitelist', 'file,pipe', '-threads', '1', '-i', str(source), '-map', '0:a:0',
            '-vn', '-sn', '-dn', '-map_metadata', '-1', '-threads', '1', '-filter_threads', '1',
            '-af', ','.join(filters), '-t', f"{opts['length']:.3f}", '-ar', '44100', '-ac', '2',
            '-codec:a', 'libmp3lame', '-q:a', '4',
            '-metadata', f"title={metadata.get('song') or ''}", '-metadata', f"artist={metadata.get('artist') or ''}", '-f', 'mp3', str(dest)]
    _ffmpeg(args)
    if dest.is_symlink() or not dest.is_file() or not 1024 <= dest.stat().st_size <= MAX_AUDIO:
        raise ValueError('Converted preview has an invalid size.')
    seconds, codec = custom_upload._probe(dest)
    if codec != 'mp3' or abs(seconds - opts['length']) > .3:
        raise ValueError('The preview length or format is not valid.')
    return seconds, _waveform(dest, seconds)


def render(app, session, token, fields):
    item = _find(app, token, session, claim=True)
    temp = item['stage'] / 'preview.part.mp3'
    try:
        folder, _ = _title(app, item['path'])
        if app.theme_confirmation_fingerprint(folder, 'studio', token) != item['fingerprint']:
            raise ValueError('The installed theme changed. Start a new Studio session.')
        opts = options(fields, item['duration'])
        metadata = dict(item['metadata'])
        for key in ('song', 'artist'):
            if key in fields:
                metadata[key] = theme_history.clean(fields[key])
        if not tasks.CONVERSION_LOCK.acquire(blocking=False):
            raise ValueError('Another theme conversion is running. Try again shortly.')
        try:
            seconds, waveform = _render_audio(item['stage'] / 'source.audio', temp, opts, metadata)
        finally:
            tasks.CONVERSION_LOCK.release()
        preview = item['stage'] / 'preview.mp3'
        digest = hashlib.sha256(temp.read_bytes()).hexdigest()
        os.replace(temp, preview)
        item.update(revision=secrets.token_urlsafe(18), options=opts, metadata=metadata, waveform=waveform,
                    preview_duration=seconds, preview_hash=digest)
        return dict(item)
    finally:
        try:
            temp.unlink(missing_ok=True)
        finally:
            _release(app, item)


def install(app, session, token, revision, confirmed):
    item = _find(app, token, session, claim=True)
    done = False
    try:
        if not item.get('revision') or not secrets.compare_digest(item['revision'], revision or ''):
            raise ValueError('Render and review the current preview first.')
        if item['existing'] and not confirmed:
            raise ValueError('Explicitly confirm replacement of the existing theme.')
        if item['source'] in ('upload', 'youtube') and not custom_upload._enabled():
            raise ValueError('Manual audio installs are disabled.')
        folder, name = _title(app, item['path'])
        if app.theme_confirmation_fingerprint(folder, 'studio', token) != item['fingerprint']:
            raise ValueError('The installed theme changed. Start a new Studio session.')
        preview = item['stage'] / 'preview.mp3'
        if preview.is_symlink() or not preview.is_file() or not 1024 <= preview.stat().st_size <= MAX_AUDIO:
            raise ValueError('Preview unavailable.')
        if hashlib.sha256(preview.read_bytes()).hexdigest() != item['preview_hash']:
            raise ValueError('Preview changed; generate it again.')
        metadata = dict(item['metadata'])
        if not str(metadata.get('source') or '').startswith('Theme Studio · '):
            metadata['source'] = f"Theme Studio · {metadata.get('source') or 'Audio'}"
        metadata['details'] = item['options']
        app.manual_install_mp3(folder, preview, replace=item['existing'], expected_fingerprint=item['fingerprint'],
                               anime_slug='studio', video_id=token, activity_label=metadata.get('song') or name,
                               prepared_hash=item['preview_hash'], metadata=metadata, duration=item['preview_duration'])
        done = True
        try:
            app.save(folder, next(label for label, root in app.ROOTS if root == folder.parent), name,
                     'installed', 'Theme Studio · ' + str(metadata.get('song') or name)[:180])
        except Exception as error:
            print(f'Studio installed; reindex on next scan ({type(error).__name__})', flush=True)
        return item['path']
    finally:
        _release(app, item, remove=done)


def cancel(app, session, token):
    item = _find(app, token, session, claim=True)
    path = item['path']
    _release(app, item, remove=True)
    return path


def _number(value):
    return f'{float(value):.3f}'.rstrip('0').rstrip('.')


def _wave_svg(values):
    bars = ''.join(f'<rect x="{i*5}" y="{40-v*36:.2f}" width="3.5" height="{max(1,v*72):.2f}" rx="1"/>'
                   for i, v in enumerate(values))
    return ('<svg id="studio-wave" viewBox="0 0 480 80" role="img" aria-label="Preview waveform" '
            'style="width:100%;height:80px;cursor:pointer;fill:#79b7ff;background:#111e32;border-radius:9px">' + bars + '</svg>')


def entry_form(path, slug='', video='', label='Edit in Theme Studio'):
    fields = {'path': path, 'source': 'animethemes' if slug and video else 'current'}
    if fields['source'] == 'animethemes':
        fields.update(slug=slug, video=video)
    hidden = ''.join(f'<input type="hidden" name="{k}" value="{_e(v)}">' for k, v in fields.items())
    return f'<form method="post" action="/theme-studio/start" style="display:inline-block;margin:6px 0">{hidden}<button type="submit">{_e(label)}</button></form>'


def pending_form(token, source):
    return (f'<form method="post" action="/theme-studio/start"><input type="hidden" name="source" value="{_e(source)}">'
            f'<input type="hidden" name="pending" value="{_e(token)}"><button type="submit">Edit in Theme Studio</button></form>')


def page(app, handler, session):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    token, path = q.get('token', [''])[0], q.get('path', [''])[0]
    notice = q.get('notice', [''])[0][:200]
    try:
        item = _find(app, token, session) if token else None
        if item:
            path = item['path']
        folder, name = _title(app, path)
    except (ValueError, OSError):
        return handler.send(404, 'Studio unavailable. Reopen the title from the Theme Browser.', 'text/plain')
    back = '/themes?' + urllib.parse.urlencode({'path': path})
    parts = ['<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Theme Studio - Preludarr</title><style>',
             app.CSS, '</style></head><body>', app.full_sidebar('themes', fixed=True), '<div class="themearr-content"><main>',
             f'<p><a href="{_e(back)}">← Theme Browser</a></p><section class="panel"><h1>Theme Studio</h1><h2>{_e(name)}</h2>',
             '<p class="muted">Edit and listen before installing. Only one theme.mp3 stays active; an explicitly replaced theme is kept in the recycle bin.</p>',
             f'<p class="notice" role="status">{_e(notice)}</p>' if notice else '']
    if not item:
        if (folder / 'theme.mp3').is_file() and not (folder / 'theme.mp3').is_symlink():
            parts.append(entry_form(path, label='Edit current theme'))
        parts.append('<p>To edit a new track, choose Edit in Theme Studio from an AnimeThemes track or a reviewed upload/YouTube audio.</p></section>')
    else:
        metadata = item['metadata']
        opts = item.get('options') or {'start': 0, 'end': item['duration'], 'fade_in': 0, 'fade_out': 0, 'normalize': False}
        hidden = f'<input type="hidden" name="token" value="{_e(token)}">'
        parts.extend([f'<p><strong>{_e(theme_history.summary(metadata))}</strong></p><p class="muted">Source length: {item["duration"]:.2f}s · {_e(item["codec"].upper())}</p>',
                      '<form method="post" action="/theme-studio/render">' + hidden,
                      '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px">',
                      *[f'<label>{label} (seconds)<input style="width:100%" type="number" name="{k}" min="0" step="0.001" value="{_number(opts[k])}" required></label>'
                        for k, label in (('start', 'Trim start'), ('end', 'Trim end'), ('fade_in', 'Fade in'), ('fade_out', 'Fade out'))],
                      f'<label>Song / title<input name="song" maxlength="200" value="{_e(metadata.get("song"))}"></label>',
                      f'<label>Artist<input name="artist" maxlength="200" value="{_e(metadata.get("artist"))}"></label></div>',
                      '<p><label><input type="checkbox" name="normalize" value="1"' + (' checked' if opts['normalize'] else '') +
                      '> Normalize perceived loudness (−16 LUFS target)</label></p>',
                      '<button type="submit">Generate preview</button></form>',
                      '<p class="muted">Rendering replaces only the temporary preview. Changes to this form must be rendered before they can be installed.</p></section>'])
        if item.get('revision'):
            source_url = '/theme-studio/audio?' + urllib.parse.urlencode({'token': token, 'revision': item['revision']})
            parts.append('<section class="panel"><h2>A/B comparison</h2><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px">')
            if item['existing']:
                current_url = '/current-theme?' + urllib.parse.urlencode({'path': path})
                parts.append(f'<div><h3>A · Current theme</h3><audio id="studio-current" controls preload="metadata" style="width:100%" src="{_e(current_url)}"></audio></div>')
            parts.extend([f'<div><h3>B · Preview ({item["preview_duration"]:.2f}s)</h3><audio id="studio-preview" controls preload="metadata" style="width:100%" src="{_e(source_url)}"></audio>',
                          _wave_svg(item['waveform']), '</div></div>',
                          '<script>(()=>{const a=document.getElementById("studio-current"),b=document.getElementById("studio-preview"),w=document.getElementById("studio-wave");'
                          'if(a&&b){a.addEventListener("play",()=>b.pause());b.addEventListener("play",()=>a.pause())}'
                          'if(w&&b)w.addEventListener("click",e=>{if(Number.isFinite(b.duration)){const r=w.getBoundingClientRect();b.currentTime=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))*b.duration}})'
                          '})();</script>',
                          '<form method="post" action="/theme-studio/install">' + hidden,
                          f'<input type="hidden" name="revision" value="{_e(item["revision"])}">',
                          ('<p><label><input type="checkbox" name="confirm" value="1" required> Replace the current theme with this exact preview (old theme kept in recycle bin)</label></p>' if item['existing'] else ''),
                          '<button type="submit">Install this preview as theme.mp3</button></form></section>'])
        parts.append('<section class="panel"><form method="post" action="/theme-studio/cancel">' + hidden +
                     '<button type="submit">Cancel and discard temporary audio</button></form><p class="muted">Editor sessions expire after 15 minutes.</p></section>')
    parts.extend([theme_history.panel(app, path), '</main></div></body></html>'])
    return handler.send(200, ''.join(parts))


def audio(app, handler, session):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    token, revision = q.get('token', [''])[0], q.get('revision', [''])[0]
    fd = None
    sent = False
    try:
        with _LOCK:
            item = _find(app, token, session)
            if not revision or item.get('revision') != revision:
                raise ValueError('Preview expired.')
            fd = os.open(item['stage'] / 'preview.mp3', os.O_RDONLY | os.O_NOFOLLOW)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 1024 <= info.st_size <= MAX_AUDIO:
                raise ValueError('Preview unavailable.')
        size, start, end = info.st_size, 0, info.st_size - 1
        requested = handler.headers.get('Range', '')
        if requested:
            match = re.fullmatch(r'bytes=([0-9]{0,12})-([0-9]{0,12})', requested)
            if not match or not any(match.groups()):
                start = size
            elif not match.group(1):
                suffix = int(match.group(2))
                start = max(0, size-suffix) if suffix else size
            else:
                start = int(match.group(1))
                if match.group(2):
                    end = min(end, int(match.group(2)))
            if start >= size or start > end:
                handler.send_response(416)
                handler.send_header('Content-Range', f'bytes */{size}')
                handler.send_header('Content-Length', '0')
                handler.end_headers()
                return
        handler.send_response(206 if requested else 200)
        handler.send_header('Content-Type', 'audio/mpeg')
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('Content-Length', str(end-start+1))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        if requested:
            handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        handler.end_headers()
        sent = True
        os.lseek(fd, start, os.SEEK_SET)
        left = end-start+1
        while left:
            chunk = os.read(fd, min(left, 262144))
            if not chunk:
                break
            handler.wfile.write(chunk)
            left -= len(chunk)
    except (OSError, ValueError):
        if not sent:
            return handler.send(404, 'Preview unavailable.', 'text/plain')
    finally:
        if fd is not None:
            os.close(fd)


def post(app, handler, session, redirect):
    token = path = ''
    route = urllib.parse.urlsplit(handler.path).path
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 8192:
            raise ValueError('Invalid Studio request.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=12)
        if any(len(v) != 1 for v in form.values()):
            raise ValueError('Invalid Studio request.')
        fields = {k: v[0] for k, v in form.items()}
        token, path = fields.get('token', ''), fields.get('path', '')
        if route == '/theme-studio/start':
            token = start(app, session, fields.get('source'), path, fields.get('slug', ''), fields.get('video', ''), fields.get('pending', ''))
            return redirect(handler, _url(token=token))
        if route == '/theme-studio/render':
            render(app, session, token, fields)
            return redirect(handler, _url(token=token, notice='Preview rendered. Review it before installing.'))
        if route == '/theme-studio/install':
            path = install(app, session, token, fields.get('revision', ''), fields.get('confirm') == '1')
            return redirect(handler, '/themes?' + urllib.parse.urlencode({'path': path, 'installed': '1'}))
        if route == '/theme-studio/cancel':
            path = cancel(app, session, token)
            return redirect(handler, '/themes?' + urllib.parse.urlencode({'path': path}))
        raise ValueError('Unknown Studio action.')
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as error:
        message = str(error)[:160] if isinstance(error, ValueError) else 'Audio operation did not finish. Check the title before retrying.'
        if token:
            try:
                _find(app, token, session)
                return redirect(handler, _url(token=token, notice=message))
            except ValueError:
                token = ''
        if path:
            return redirect(handler, _url(path=path, notice=message))
        return handler.send(409, 'Studio could not start. Reopen the audio from the Theme Browser.', 'text/plain')
