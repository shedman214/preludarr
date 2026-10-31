# SPDX-License-Identifier: GPL-3.0-or-later
"""Themearr custom audio uploads; isolated opt-in extension for v0.7."""
import html
import json
import mmap
import os
import re
import subprocess
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
from email.message import Message
from pathlib import Path

MAX_AUDIO = 40 * 1024 * 1024
MAX_REQUEST = MAX_AUDIO + 65536
PENDING_SECONDS = 300
MIN_SECONDS = 1
MAX_SECONDS = 600
AUDIO_TYPES = {'.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.flac': 'audio/flac', '.wav': 'audio/wav'}
_PENDING = {}
_PENDING_LOCK = threading.Lock()


def _enabled():
 return os.getenv('THEMEARR_MANUAL_INSTALL_ENABLED', '0') == '1'


def _probe(path):
 """Return (seconds, codec) when the file holds real audio of an acceptable length; else ValueError."""
 try:
  done = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                         '-show_entries', 'stream=codec_name:format=duration', '-of', 'json', str(path)],
                        capture_output=True, text=True, timeout=30)
 except (OSError, subprocess.TimeoutExpired):
  raise ValueError('The audio file could not be checked. Please try again.')
 try:
  data = json.loads(done.stdout or '{}')
 except ValueError:
  data = {}
 streams = data.get('streams') or []
 if done.returncode != 0 or not streams:
  raise ValueError('This file does not contain playable audio.')
 try:
  seconds = float((data.get('format') or {}).get('duration'))
 except (TypeError, ValueError):
  raise ValueError('The length of this audio could not be read.')
 if not MIN_SECONDS <= seconds <= MAX_SECONDS:
  raise ValueError('Audio must be between 1 second and 10 minutes long.')
 return seconds, str(streams[0].get('codec_name') or 'unknown')


def _page(title, body, css):
 return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
         '<title>'+html.escape(title)+' - Preludarr</title><style>'+css+'</style></head><body><main>'
         '<div class="panel">'+body+'</div></main></body></html>')


def _back(selected):
 return '/themes?'+urllib.parse.urlencode({'path': selected}) if selected else '/themes'


def _redirect(handler, location):
 handler.send_response(303)
 handler.send_header('Location', location)
 handler.send_header('Cache-Control', 'no-store')
 handler.send_header('Content-Length', '0')
 handler.end_headers()


def sweep_stale(config, max_age=3600):
 """Delete upload staging folders left behind by a restart; pending uploads only live in memory."""
 config = Path(config)
 cutoff = time.time() - max_age
 removed = 0
 for directory in config.glob('themearr-custom-upload-*'):
  try:
   if directory.is_dir() and not directory.is_symlink() and directory.stat().st_mtime < cutoff:
    _stage_remove(directory, config)
    removed += 1
  except OSError:
   pass
 return removed


def preview(handler, config, token):
 """Stream a pending upload so it can be heard on the review page before installing."""
 if not _enabled():
  return handler.send(403, 'Custom uploads disabled', 'text/plain')
 if not re.fullmatch('[A-Za-z0-9_-]{20,128}', token or ''):
  return handler.send(404, 'Not found', 'text/plain')
 with _PENDING_LOCK:
  _expire(Path(config))
  item = _PENDING.get(token)
 if item is None:
  return handler.send(404, 'Upload expired', 'text/plain')
 source = item['stage']/'uploaded.audio'
 try:
  with source.open('rb') as audio:
   size = os.fstat(audio.fileno()).st_size
   handler.send_response(200)
   handler.send_header('Content-Type', AUDIO_TYPES.get(item['extension'], 'application/octet-stream'))
   handler.send_header('Content-Length', str(size))
   handler.send_header('Cache-Control', 'no-store')
   handler.send_header('X-Content-Type-Options', 'nosniff')
   handler.end_headers()
   while True:
    chunk = audio.read(262144)
    if not chunk:
     break
    handler.wfile.write(chunk)
 except (BrokenPipeError, ConnectionResetError):
  return
 except OSError:
  return handler.send(404, 'Upload unavailable', 'text/plain')


def _stage_remove(directory, config):
 directory = Path(directory)
 config = Path(config)
 if (directory.parent == config and
     directory.name.startswith('themearr-custom-upload-') and
     not directory.is_symlink()):
  shutil.rmtree(directory, ignore_errors=True)


def _expire(config):
 now = time.monotonic()
 for key, item in list(_PENDING.items()):
  if item['expiry'] <= now:
   _stage_remove(item['stage'], config)
   del _PENDING[key]


def _destination(selected, roots, db):
 if not isinstance(selected, str) or len(selected) > 1024:
  raise ValueError('Invalid anime path')
 folder = Path(selected)
 if not any(folder.parent == root and root.is_dir() and
            not root.is_symlink() for _, root in roots):
  raise ValueError('Destination must be a direct child of a library')
 if not folder.is_dir() or folder.is_symlink():
  raise ValueError('Invalid anime directory')
 if (folder / 'theme.mp3').is_symlink():
  raise ValueError('Unsafe existing theme')
 with db() as connection:
  row = connection.execute('SELECT name FROM shows WHERE path=?',
                           (selected,)).fetchone()
 if row is None:
  raise ValueError('Anime was not found in library')
 return folder, row['name']


def _multipart(mm, boundary):
 """Locate exactly two standard multipart fields without loading audio into RAM."""
 if not re.fullmatch(rb'[A-Za-z0-9\x27()+_,./:=?-]{1,70}', boundary):
  raise ValueError('Invalid multipart boundary')
 opening = b'--' + boundary + b'\r\n'
 divider = b'\r\n--' + boundary
 if mm[:len(opening)] != opening:
  raise ValueError('Invalid multipart body')
 pos = len(opening)
 result = {}
 while True:
  head_end = mm.find(b'\r\n\r\n', pos)
  if head_end < 0 or head_end - pos > 8192:
   raise ValueError('Invalid multipart headers')
  header = mm[pos:head_end].decode('latin-1')
  matches = re.findall(r'(?im)^content-disposition:\s*form-data;\s*name="([^"]+)"(?:;\s*filename="([^"]*)")?\s*$', header)
  if len(matches) != 1:
   raise ValueError('Invalid upload field')
  field, filename = matches[0]
  if field not in ('path', 'audio') or field in result:
   raise ValueError('Unexpected or duplicate upload field')
  content_start = head_end + 4
  end = mm.find(divider, content_start)
  if end < 0:
   raise ValueError('Missing multipart closing boundary')
  result[field] = (content_start, end, filename)
  pos = end + len(divider)
  if mm[pos:pos+2] == b'--':
   if mm[pos+2:] not in (b'', b'\r\n'):
    raise ValueError('Unexpected upload trailer')
   break
  if mm[pos:pos+2] != b'\r\n':
   raise ValueError('Invalid multipart delimiter')
  pos += 2
 if len(result) != 2 or result['audio'][2] == '':
  raise ValueError('A file and anime destination are required')
 return result


def review(handler, config, roots, db, issue_confirmation, css=''):
 if not _enabled():
  return handler.send(403, 'Custom uploads disabled', 'text/plain')
 if handler.headers.get('Transfer-Encoding'):
  return handler.send(400, 'Chunked uploads are not supported', 'text/plain')
 lengths = handler.headers.get_all('Content-Length') or []
 if len(lengths) != 1 or not lengths[0].isdigit():
  return handler.send(411, 'Content-Length required', 'text/plain')
 size = int(lengths[0])
 if size < 64 or size > MAX_REQUEST:
  return handler.send(413, 'Upload exceeds 40 MB limit', 'text/plain')
 content_type = Message()
 content_type['Content-Type'] = handler.headers.get('Content-Type', '')
 if content_type.get_content_type() != 'multipart/form-data':
  return handler.send(415, 'Multipart upload required', 'text/plain')
 boundary = content_type.get_param('boundary', header='content-type')
 if not boundary or not boundary.isascii():
  return handler.send(400, 'Missing multipart boundary', 'text/plain')
 config = Path(config)
 with _PENDING_LOCK:
  _expire(config)
  if len(_PENDING) >= 2:
   return handler.send(429, 'Two uploads are already awaiting confirmation', 'text/plain')
  stage = Path(tempfile.mkdtemp(prefix='themearr-custom-upload-', dir=config))
  selected = ''
  try:
   raw = stage / 'request.bin'
   with raw.open('xb') as output:
    remaining = size
    handler.connection.settimeout(30)
    while remaining:
     chunk = handler.rfile.read(min(262144, remaining))
     if not chunk:
      raise ValueError('Upload was interrupted')
     output.write(chunk)
     remaining -= len(chunk)
   with raw.open('rb') as input_file:
    with mmap.mmap(input_file.fileno(), 0, access=mmap.ACCESS_READ) as mm:
     fields = _multipart(mm, boundary.encode('ascii'))
     ps, pe, _ = fields['path']
     if pe-ps > 1024:
      raise ValueError('Invalid path size')
     selected = mm[ps:pe].decode('utf-8')
     folder, anime_name = _destination(selected, roots, db)
     start, end, filename = fields['audio']
     extension = Path(filename.replace('\\', '/')).suffix.lower()
     if extension not in ('.mp3', '.ogg', '.flac', '.wav'):
      raise ValueError('Only MP3, OGG, FLAC and WAV are supported')
     audio_size = end-start
     if not 1024 <= audio_size <= MAX_AUDIO:
      raise ValueError('Audio file must be between 1 KB and 40 MB')
     source = stage / 'uploaded.audio'
     with source.open('xb') as output:
      pos = start
      while pos < end:
       stop = min(end, pos + 262144)
       output.write(mm[pos:stop])
       pos = stop
   raw.unlink()
   seconds, codec = _probe(source)
   video_id = str(secrets.randbelow(10**18 - 10**17) + 10**17)
   token = issue_confirmation(folder, 'custom_upload', video_id)
   _PENDING[token] = {
    'expiry': time.monotonic() + PENDING_SECONDS,
    'stage': stage,
    'folder': str(folder),
    'video_id': video_id,
    'filename': filename,
    'bytes': audio_size,
    'extension': extension,
   }
   replace_text = ('This will replace the currently installed theme.mp3.'
                   if (folder/'theme.mp3').exists()
                   else 'No existing theme was found.')
   minutes, rest = divmod(int(round(seconds)), 60)
   token_field = '<input type="hidden" name="token" value="'+html.escape(token, quote=True)+'">'
   body = (
    '<h2>Review Custom Audio</h2>'
    '<p><strong>Anime:</strong> '+html.escape(anime_name)+'</p>'
    '<p><strong>Selected audio:</strong> '+html.escape(filename)+'</p>'
    '<p><strong>Length:</strong> '+f'{minutes}:{rest:02d}'+' · <strong>Format:</strong> '+html.escape(codec.upper())+
    ' · <strong>Size:</strong> '+f'{audio_size/1048576:.2f}'+' MB</p>'
    '<audio controls preload="metadata" style="width:100%" src="/preview-custom-upload?'+
    html.escape(urllib.parse.urlencode({'token': token}), quote=True)+'"></audio>'
    '<p><strong>Destination:</strong> theme.mp3 — '+html.escape(replace_text)+'</p>'
    '<p class="muted">Your audio will be converted to MP3. The existing theme is only replaced if conversion succeeds. '
    'This review expires in five minutes.</p>'
    '<form method="post" action="/confirm-custom-upload" style="display:inline">'+token_field+
    '<button type="submit">Confirm and Install</button></form> '
    '<form method="post" action="/cancel-custom-upload" style="display:inline">'+token_field+
    '<button type="submit" style="background:#52617c">Cancel and Delete Upload</button></form>'
   )
   return handler.send(200, _page('Review Custom Audio', body, css), 'text/html')
  except (OSError, ValueError) as error:
   _stage_remove(stage, config)
   reason = str(error) if isinstance(error, ValueError) else 'The upload could not be saved. Please try again.'
   back = _back(selected)
   body = ('<h2>Upload not accepted</h2><p>'+html.escape(reason)+'</p>'
           '<p class="muted">Supported: MP3, OGG, FLAC or WAV, up to 40 MB and 10 minutes. Nothing was changed.</p>'
           '<p><a href="'+html.escape(back, quote=True)+'">← Back to the Theme Browser</a></p>')
   return handler.send(400, _page('Upload not accepted', body, css), 'text/html')
  except Exception:
   _stage_remove(stage, config)
   raise


def confirm(handler, config, roots, db, take_confirmation, install, css=''):
 if not _enabled():
  return handler.send(403, 'Custom uploads disabled', 'text/plain')
 length = handler.headers.get('Content-Length', '')
 if not length.isdigit() or int(length) > 2048 or int(length) < 1:
  return handler.send(413, 'Invalid confirmation request', 'text/plain')
 if not handler.headers.get('Content-Type', '').lower().startswith('application/x-www-form-urlencoded'):
  return handler.send(415, 'Invalid confirmation type', 'text/plain')
 form = urllib.parse.parse_qs(handler.rfile.read(int(length)).decode('utf-8'))
 token = form.get('token', [''])[0]
 if not re.fullmatch('[A-Za-z0-9_-]{20,128}', token):
  return handler.send(400, 'Invalid token', 'text/plain')
 with _PENDING_LOCK:
  _expire(config)
  item = _PENDING.pop(token, None)
 if item is None:
  return handler.send(409, 'Upload expired or already used', 'text/plain')
 try:
  folder, name = _destination(item['folder'], roots, db)
  fingerprint = take_confirmation(token, folder, 'custom_upload', item['video_id'])
  if fingerprint is None:
   return handler.send(409, 'Confirmation expired or theme changed', 'text/plain')
  install(folder, item['stage']/'uploaded.audio',
          replace=True, expected_fingerprint=fingerprint,
          anime_slug='custom_upload', video_id=item['video_id'],
          activity_label=item['filename'])
  with db() as conn:
   conn.execute('UPDATE shows SET state=?,detail=?,updated=? WHERE path=?',
                ('installed', 'Manual custom audio upload',
                 time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime()), item['folder']))
  return _redirect(handler, '/themes?'+urllib.parse.urlencode({'path': item['folder'], 'installed': 'custom'}))
 except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
  if isinstance(error, subprocess.CalledProcessError):
   reason = 'The audio could not be converted to MP3.'
  elif isinstance(error, subprocess.TimeoutExpired):
   reason = 'Converting the audio took too long.'
  elif isinstance(error, (ValueError, RuntimeError)):
   reason = str(error)
  else:
   reason = 'The theme file could not be written.'
  body = ('<h2>Custom theme not installed</h2><p>'+html.escape(reason)+'</p>'
          '<p>Your existing theme.mp3 was not changed.</p>'
          '<p><a href="'+html.escape(_back(item['folder']), quote=True)+'">← Back to the Theme Browser</a></p>')
  return handler.send(409, _page('Custom theme not installed', body, css), 'text/html')
 finally:
  _stage_remove(item['stage'], config)


def cancel(handler, config):
 length = handler.headers.get('Content-Length', '')
 if not length.isdigit() or not 1 <= int(length) <= 2048:
  return handler.send(400, 'Invalid request', 'text/plain')
 if not handler.headers.get('Content-Type', '').lower().startswith('application/x-www-form-urlencoded'):
  return handler.send(415, 'Invalid request type', 'text/plain')
 form = urllib.parse.parse_qs(handler.rfile.read(int(length)).decode('utf-8'))
 token = form.get('token', [''])[0]
 with _PENDING_LOCK:
  item = _PENDING.pop(token, None)
 if item:
  _stage_remove(item['stage'], config)
 return _redirect(handler, _back(item['folder'] if item else ''))
