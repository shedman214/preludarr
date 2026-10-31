# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import hashlib
import hmac
import html
import json
import re
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import youtube_ui

APP_VERSION='1.12.0-beta.1'

# Branding assets shipped in /app/static (V1.10.0: Preludarr logo + emblem; the old Themearr files stay allowed so cached pages
# never show a broken image). Only these exact files are served.
STATIC_DIR=Path(__file__).resolve().parent/'static'
STATIC_FILES={'theme-forge-logo.png':'image/png','themearr-emblem-512.png':'image/png','anidb-logo.png':'image/png','sonarr-logo.png':'image/png','radarr-logo.png':'image/png','animethemes-logo.png':'image/png','plex-logo.png':'image/png','themerrdb-logo.png':'image/png',
 'themearr-icon-16.png':'image/png','themearr-icon-32.png':'image/png','themearr-icon-48.png':'image/png','themearr-icon-180.png':'image/png','themearr-icon-192.png':'image/png',
 'preludarr-logo.png':'image/png','preludarr-emblem-512.png':'image/png','preludarr-emblem-192.png':'image/png','preludarr-emblem-180.png':'image/png',
 'preludarr-emblem-48.png':'image/png','preludarr-emblem-32.png':'image/png','preludarr-emblem-16.png':'image/png',
 'splicarr-logo.png':'image/png','splicarr-emblem-512.png':'image/png','splicarr-emblem-192.png':'image/png','splicarr-emblem-180.png':'image/png',
 'splicarr-emblem-48.png':'image/png','splicarr-emblem-32.png':'image/png','splicarr-emblem-16.png':'image/png'}
_STATIC_CACHE={}

def _static_asset(name):
 """Return (bytes, short hash) for an allow-listed asset, loading it once."""
 if name not in STATIC_FILES:return None
 if name not in _STATIC_CACHE:
  try:data=(STATIC_DIR/name).read_bytes()
  except OSError:return None
  _STATIC_CACHE[name]=(data,hashlib.sha256(data).hexdigest()[:12])
 return _STATIC_CACHE[name]

def static_url(name):
 asset=_static_asset(name)
 return '/static/'+name+('?v='+asset[1] if asset else '')

def brand_html(variant='sidebar'):
 """Preludarr logo (approved artwork, 3:1, transparent); sidebar version links to the dashboard, login version is larger and centred."""
 alt='Preludarr — Theme your library'
 if variant=='login':
  return ('<img src="'+static_url('preludarr-logo.png')+'" alt="'+alt+'" width="720" height="245" '
          'style="display:block;width:100%;max-width:330px;height:auto;margin:0 auto">')
 return ('<a href="/" aria-label="Preludarr — go to dashboard" style="display:block;line-height:0">'
         '<img src="'+static_url('preludarr-logo.png')+'" alt="'+alt+'" width="720" height="245" '
         'style="display:block;width:100%;max-width:210px;max-height:70px;height:auto;object-fit:contain;margin:0 auto"></a>')

# V1.9.7 (user): the app's own emblem as the browser/site icon (tabs, Edge sidebar, phone home screen). V1.10.0: Preludarr emblem.
def icon_tags():
 return ('<link rel="icon" type="image/png" sizes="32x32" href="'+static_url('preludarr-emblem-32.png')+'">'
  '<link rel="icon" type="image/png" sizes="16x16" href="'+static_url('preludarr-emblem-16.png')+'">'
  '<link rel="icon" type="image/png" sizes="48x48" href="'+static_url('preludarr-emblem-48.png')+'">'
  '<link rel="apple-touch-icon" sizes="180x180" href="'+static_url('preludarr-emblem-180.png')+'">'
  '<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#101c30">'+BACK_BUTTON)


# Preludarr 1.11.7 (user 2026-10-29 "where is the back button"): the installed app (Edge/Chrome/iPhone, display-mode standalone)
# has no browser back button. Show our own "← Back" at the top of the page — only inside the installed app, and only when there is
# a Preludarr page to go back to. If the browser already gives minimal-ui (its own back/reload), this stays hidden.
BACK_BUTTON=('<style>.sp-back{display:inline-flex;align-items:center;gap:6px;margin:0 0 12px;padding:7px 14px;border:1px solid #3e5174;'
 'border-radius:9px;background:#16233a;color:#eef4ff;font:600 14.5px system-ui,-apple-system,"Segoe UI",sans-serif;cursor:pointer}'
 '.sp-back:hover{background:#203252}.sp-back:focus-visible{outline:3px solid #8fc1ff;outline-offset:2px}</style>'
 '<script>(function(){"use strict";'
 'function same(){try{return !!document.referrer&&new URL(document.referrer).origin===location.origin}catch(e){return false}}'
 'window.spBack=function(force){var m=document.querySelector("main");if(!m||document.getElementById("sp-back"))return;'
 'if(!force&&!(history.length>1||same()))return;'
 'var b=document.createElement("button");b.type="button";b.id="sp-back";b.className="sp-back";b.textContent="\u2190 Back";'
 'b.setAttribute("aria-label","Go back to the previous page");'
 'b.addEventListener("click",function(){if(history.length>1)history.back();else location.href="/"});m.insertBefore(b,m.firstChild)};'
 'var app=(window.matchMedia&&matchMedia("(display-mode: standalone)").matches)||navigator.standalone===true;'
 'if(app){if(document.readyState==="loading")addEventListener("DOMContentLoaded",function(){spBack()});else spBack()}'
 '})();</script>')


def add_icons(body):
 """Insert the icon tags into an HTML page once (inside <head> when there is one)."""
 if not isinstance(body,str) or 'rel="icon"' in body or '<html' not in body[:400].lower() and '<!doctype' not in body[:200].lower():
  return body
 tags=icon_tags()
 name=instance_name()
 if name!='Preludarr':  # V1.9.7 Host → Instance name in the browser tab (1.12: default name is Preludarr)
  body=re.sub(r'(<title>[^<]*?)Preludarr',lambda m:m.group(1)+html.escape(name),body,count=1)
 low=body.lower()
 i=low.find('</head>')
 if i!=-1:return body[:i]+tags+body[i:]
 j=low.find('<head>')
 if j!=-1:return body[:j+6]+tags+body[j+6:]
 k=low.find('<html')
 k=body.find('>',k)+1 if k!=-1 else 0
 return body[:k]+'<head>'+tags+'</head>'+body[k:]


def manifest_json():
 # Preludarr 1.11.4 (user 2026-10-29: the installed Edge/Chrome app had no back button, unlike Reddit's): Edge/Chrome use
 # display_override → minimal-ui (back + reload in the title bar); Safari ignores it and keeps 'standalone'. 'id' pins the
 # app identity to '/' (the value browsers already derived), so installed apps update instead of becoming a second app.
 return json.dumps({'id':'/','name':instance_name(),'short_name':instance_name()[:12],'start_url':'/','display':'standalone','display_override':['minimal-ui'],
  'background_color':'#0a1221','theme_color':'#101c30',
  'icons':[{'src':static_url('preludarr-emblem-192.png'),'sizes':'192x192','type':'image/png'},
           {'src':static_url('preludarr-emblem-512.png'),'sizes':'512x512','type':'image/png'}]})


def instance_name():
 try:return __import__('general_settings').instance_name()
 except Exception:return 'Preludarr'


def serve_static(h,path):
 name=path[len('/static/'):]
 asset=_static_asset(name) if re.fullmatch(r'[a-z0-9-]{1,64}\.png',name) else None
 if asset is None:
  body=b'Not found'
  h.send_response(404);h.send_header('Content-Type','text/plain; charset=utf-8')
  h.send_header('Content-Length',str(len(body)));h.end_headers();h.wfile.write(body)
  return
 data=asset[0]
 h.send_response(200)
 h.send_header('Content-Type',STATIC_FILES[name])
 h.send_header('Content-Length',str(len(data)))
 h.send_header('Cache-Control','public, max-age=31536000, immutable')
 h.send_header('X-Content-Type-Options','nosniff')
 h.end_headers()
 h.wfile.write(data)

def extra_allowed_hosts():
 """Optional extra dashboard host:port values (e.g. a test container); empty unless configured."""
 hosts=set()
 for item in os.environ.get('THEMEARR_EXTRA_ALLOWED_HOSTS','').split(','):
  item=item.strip().lower()
  if re.fullmatch(r'[a-z0-9.-]{1,253}:[0-9]{1,5}',item):hosts.add(item)
 return hosts

# V1.9.6 (user 2026-10-27: Cloudflare tunnel → "Request origin rejected"): optional public HTTPS address(es).
PUBLIC_FILE='themearr_public_url.json'
_PUBLIC_RE=re.compile(r'https://([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)(?::([0-9]{1,5}))?/?')


def parse_public_url(raw):
 """'https://host[:port]' → 'host[:port]' (lower case). ValueError with a plain reason otherwise."""
 text=str(raw or '').strip().lower()
 m=_PUBLIC_RE.fullmatch(text)
 if not m:
  raise ValueError('Enter the full HTTPS address only, e.g. https://themearr.example.com (no path).')
 if m.group(2) and not 1<=int(m.group(2))<=65535:
  raise ValueError('Invalid port.')
 return m.group(1)+((':'+m.group(2)) if m.group(2) and m.group(2)!='443' else '')


def public_hosts():
 """Hosts Themearr may be reached at over HTTPS (reverse proxy / Cloudflare tunnel): env THEMEARR_PUBLIC_URL + Settings."""
 hosts=set()
 for item in os.environ.get('THEMEARR_PUBLIC_URL','').split(','):
  if item.strip():
   try:hosts.add(parse_public_url(item))
   except ValueError:pass
 try:
  path=Path(os.environ.get('CONFIG_DIR','/config'))/PUBLIC_FILE
  if not path.is_symlink():
   for item in json.loads(path.read_text()).get('urls',[])[:5]:
    try:hosts.add(parse_public_url(item))
    except ValueError:pass
 except (OSError,ValueError,TypeError,AttributeError):
  pass
 return hosts


def lan_hosts():
 """Named LAN hosts the user allowed (Settings → Host, THEMEARR_EXTRA_ALLOWED_HOSTS). Private IPs need no entry (is_lan_host)."""
 extra=set()
 try:extra=__import__('general_settings').allowed_hosts()  # V1.9.7 Settings → Host → Allowed hosts
 except Exception:pass
 return extra_allowed_hosts()|extra


def is_lan_host(host):
 """1.12 (pre-release task 5/6): any private / loopback / link-local IP address of this server, or localhost, on any port, works
 without configuration; host NAMES must be allowed explicitly. (DNS-rebinding attacks need a host name, never an IP literal.)"""
 import ipaddress
 host=str(host or '').lower()
 if host in lan_hosts():return True
 m=re.fullmatch(r'(?:\[([0-9a-f:.]+)\]|([0-9.]+)|(localhost))(?::([0-9]{1,5}))?',host)
 if not m:return False
 if m.group(3):return True
 try:ip=ipaddress.ip_address(m.group(1) or m.group(2))
 except ValueError:return False
 return ip.is_private or ip.is_loopback or ip.is_link_local


def origin_ok(h):
 """Shared host + same-origin rule (login form, sessions, write guard). LAN hosts over http://, public hosts over https://."""
 host=h.headers.get('Host','').lower()
 public=public_hosts()
 if is_lan_host(host):scheme='http'
 elif host in public:scheme='https'
 else:return False
 origin=h.headers.get('Origin','')
 referer=h.headers.get('Referer','')
 if origin:return origin.lower()==scheme+'://'+host
 if referer:
  u=urllib.parse.urlsplit(referer)
  return u.scheme==scheme and u.netloc.lower()==host
 return False


def host_ok(h):
 host=h.headers.get('Host','').lower()
 return is_lan_host(host) or host in public_hosts()


def via_https(h):
 return h.headers.get('Host','').lower() in public_hosts()


SESSION_MAX=7*86400  # V1.9.14: absolute session lifetime (idle timeout stays 12 h)


class Auth:
 def __init__(self):
  self.sessions={}
  self.born={}  # V1.9.14: session start times — a session ends after SESSION_MAX even if it stays active
  self.failures={}
  self.lock=threading.RLock()
  base=Path(os.environ.get('CONFIG_DIR','/config'))/'.themearr-auth'  # 1.12: follows CONFIG_DIR (was a fixed /config path)
  self.password_file=base/'password_hash'
  self.account_file=base/'account.json'
  self.migrated_marker=base/'.migrated'
  self.setup_code=None  # 1.12 first-run setup: one-time code, printed to the container log, kept only in memory
  self.setup_failures=[]

 def hash_text(self):
  path=self.password_file
  if path.is_symlink():return ''
  if path.exists():
   try:return path.read_text().strip()
   except OSError:return ''
  return os.environ.get('THEMEARR_WRITE_AUTH','')

 def credentials(self):
  """Return persisted username/hash, or legacy admin/hash before first account edit."""
  if self.account_file.is_symlink():return None,None
  if self.account_file.exists():
   try:
    data=json.loads(self.account_file.read_text())
    username=data['username']
    stored=data['password_hash']
    if not self.valid_username(username) or not self.valid_hash(stored):return None,None
    return username,stored
   except (OSError,ValueError,TypeError,KeyError):return None,None
  if self.migrated_marker.exists() or self.migrated_marker.is_symlink():return None,None
  return 'admin',self.hash_text()

 def valid_username(self,username):
  return isinstance(username,str) and re.fullmatch(r'[A-Za-z0-9_.-]{3,32}',username) is not None

 def valid_hash(self,stored):
  if not isinstance(stored,str):return False
  try:
   a,b=stored.split(':',1)
   return len(bytes.fromhex(a))==16 and len(bytes.fromhex(b))==32
  except (ValueError,TypeError):return False

 def verify(self,password,username=None):
  if not isinstance(password,str) or len(password)>256:return False
  user,stored=self.credentials()
  if user is None or (username is not None and username!=user):return False
  try:
   salt_hex,hash_hex=stored.split(':',1)
   salt=bytes.fromhex(salt_hex)
   expected=bytes.fromhex(hash_hex)
   if len(salt)!=16 or len(expected)!=32:return False
   actual=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,240000)
   return hmac.compare_digest(actual,expected)
  except (ValueError,TypeError,UnicodeError):return False

 def same_origin(self,h):
  return origin_ok(h)  # V1.9.6: shared rule (LAN http + optional public https address)

 def token(self,h):
  raw=h.headers.get('Cookie','')
  if len(raw)>4096:return None
  for part in raw.split(';'):
   name,sep,value=part.strip().partition('=')
   if sep and name=='themearr_session' and len(value)<=128:
    return value
  return None

 def active(self,h):
  if not host_ok(h):return False
  token=self.token(h)
  if not token:return False
  key=hashlib.sha256(token.encode()).hexdigest()
  now=time.monotonic()
  with self.lock:
   expires=self.sessions.get(key)
   if expires is None:return False
   if expires<now or now-self.born.get(key,now)>SESSION_MAX:
    self.sessions.pop(key,None);self.born.pop(key,None)
    return False
   self.sessions[key]=now+43200
   return True

 def issue(self):
  raw=secrets.token_urlsafe(32)
  key=hashlib.sha256(raw.encode()).hexdigest()
  now=time.monotonic()
  with self.lock:
   self.sessions={k:v for k,v in self.sessions.items() if v>now and now-self.born.get(k,now)<=SESSION_MAX}
   self.born={k:self.born[k] for k in self.sessions if k in self.born}
   self.sessions[key]=now+43200;self.born[key]=now
  return raw

 def redirect(self,h,path='/',cookie=None):
  h.send_response(303)
  h.send_header('Location',path)
  if cookie is not None:
   h.send_header('Set-Cookie',cookie)
  h.send_header('Cache-Control','no-store')
  h.send_header('Content-Length','0')
  h.end_headers()

 def page(self,error=''):
  msg=('<p role="alert" style="color:#ff9c9c">'+html.escape(error)+'</p>') if error else ''
  return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
   '<title>Preludarr Login</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;'
   'background:#0b1424;color:#eaf2ff;font:16px system-ui}main{box-sizing:border-box;width:min(380px,calc(100vw - 24px));background:#15223b;'
   'border:1px solid #344564;border-radius:15px;padding:30px}input,button{display:block;box-sizing:border-box;'
   'width:100%;padding:12px;margin:12px 0;border-radius:8px}button{background:#457bf2;color:white;'
   'border:0;cursor:pointer}input{background:#0b1424;color:white;border:1px solid #637189}</style>'
   '</head><body><main><h1>'+brand_html('login')+'</h1><p>Sign in to theme your library.</p>'
   +msg+'<form method="post" action="/login"><label>Username<input name="username" '
   'autocomplete="username" maxlength="32" required></label><label>Password<input name="password" type="password" '
   'autocomplete="current-password" required></label><button>Sign in</button></form></main></body></html>')

 def form(self,h):
  try:
   size=int(h.headers.get('Content-Length','0'))
   if not 0<size<=8192:raise ValueError('Invalid form size')
   content_type=h.headers.get('Content-Type','').split(';',1)[0].lower()
   if content_type!='application/x-www-form-urlencoded':raise ValueError('Invalid form type')
   raw=h.rfile.read(size).decode('utf-8')
   return urllib.parse.parse_qs(raw,keep_blank_values=True)
  except (ValueError,UnicodeError):return None

 def login(self,h):
  if not self.same_origin(h):return h.send(403,'Request origin rejected','text/plain')
  form=self.form(h)
  if form is None:return h.send(400,'Invalid sign-in form','text/plain')
  username=form.get('username',[''])[0]
  password=form.get('password',[''])[0]
  ip=__import__('general_settings').client_ip(h)  # V1.9.7: visitor IP behind a trusted proxy
  now=time.monotonic()
  with self.lock:
   times=[t for t in self.failures.get(ip,[]) if now-t<300]
   self.failures[ip]=times
   if len(self.failures)>5000:  # V1.9.14: bound memory (many different addresses)
    self.failures={k:v for k,v in self.failures.items() if v and now-v[-1]<300}
   blocked=len(times)>=5
  if blocked:return h.send(429,'Too many login attempts. Try again in five minutes.','text/plain')
  if not self.verify(password,username):
   with self.lock:self.failures.setdefault(ip,[]).append(now)
   return h.send(401,self.page('Invalid username or password'))
  with self.lock:self.failures.pop(ip,None)
  cookie='themearr_session='+self.issue()+'; Path=/; HttpOnly; SameSite=Strict'+('; Secure' if via_https(h) else '')
  return self.redirect(h,'/',cookie)

 def settings(self,h,message=''):
  # themearr-settings-views-v3
  _q=urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
  requested=_q.get('section',[''])[0]
  if not requested:  # V1.9.8: after saving, come back to the tab that was saved
   requested=next((t for k,t in (('recycle_notice','recycle'),('remote_notice','remote'),('host_notice','host'),('security_notice','security'),('logging_notice','logging')) if k in _q),'general')
  section=requested if requested in (
   'general','youtube','ui','recycle','host','remote','security','logging'
  ) else 'general'
  visible={
   'general':'#settings-authentication',  # V1.9.8: each General tab shows only its own section (user 2026-10-27)
   'recycle':'#settings-recycle-bin','host':'#settings-host','remote':'#settings-remote-access',
   'security':'#settings-security','logging':'#settings-logging',
   'youtube':'#youtube-settings',
   'ui':'#settings-ui'
  }[section]
  gtab=section in ('general','recycle','host','remote','security','logging')
  note=('<div role="status" class="notice">'+html.escape(message)+'</div>') if message else ''
  username,stored=self.credentials()
  username=html.escape(username or '',quote=True)
  return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
   '<title>Settings - Preludarr</title><style>'
   ':root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0a1221;color:#eaf2ff;'
   'font:15px system-ui,-apple-system,Segoe UI,sans-serif;display:flex;min-height:100vh}'
   'a{color:#b4cfff;text-decoration:none}a:hover{text-decoration:underline}'
   'aside{background:#101c30;border-right:1px solid #293954;width:236px;flex:none;padding:24px 14px;'
   'display:flex;flex-direction:column;gap:22px}nav{display:flex;flex-direction:column;gap:6px}'
   'nav a{display:block;padding:12px;border-radius:8px;color:#c3d3ec}nav a:hover,nav a.active{'
   'background:#223a61;color:white;text-decoration:none}.brand{font-weight:800;font-size:20px;padding:6px}'
   '.footer{margin-top:auto;color:#8fa4c5;font-size:12px;padding:6px}'
   'main{width:min(980px,100%);margin:0 auto;padding:36px 28px 80px}'
   '.crumb{color:#9aaccc;margin-bottom:10px;font-size:13px}h1{font-size:29px;margin:0 0 8px}'
   '.muted{color:#a5b9d7}.panel{background:#14233c;border:1px solid #34496c;'
   'border-radius:12px;margin-top:22px;padding:24px;max-width:750px}'
   '.pill{display:inline-block;background:#263e60;padding:6px 11px;border-radius:6px;'
   'color:#cddfff;font-size:13px;margin-top:12px}label{display:block;font-weight:650;margin:18px 0 8px}'
   'input{width:100%;max-width:560px;display:block;background:#0c172a;color:#f3f7ff;'
   'border:1px solid #536787;border-radius:8px;padding:12px;margin-top:7px;font:inherit}'
   'input:focus{outline:2px solid #609aff;outline-offset:1px}'
   'button{font:inherit;background:#487de9;border:0;color:white;border-radius:8px;'
   'padding:11px 18px;cursor:pointer;font-weight:650;margin-top:20px}'
   '.notice{border:1px solid #4a8c67;background:#193a2b;color:#d9ffea;'
   'padding:12px;border-radius:8px;margin-top:14px}'
   '.help{color:#9eb2d1;font-size:13px;margin:3px 0 10px}'
   '.logout{margin:0}.logout button{background:#293b57;width:100%;text-align:left;margin:0}'
   '@media(max-width:700px){body{display:block}aside{width:100%;border-right:0;border-bottom:1px solid #293954;'
   'gap:10px;padding:12px}nav{flex-direction:row;flex-wrap:wrap}nav a{padding:7px 9px}'
   'main{padding:24px 15px}.panel{padding:19px}.footer{display:none}}'
   '.settings-subnav{display:flex;flex-direction:column;'
    +'gap:3px;margin:-2px 0 10px 13px;padding:7px 0 7px 13px;'
    +'border-left:2px solid #476588;background:#15233a;'
    +'border-radius:0 7px 7px 0}'
    +'.settings-subnav a{display:block;padding:9px 11px;'
    +'font-size:13px;color:#c5d5ed;border-radius:6px}'
    +'.settings-subnav a:hover,.settings-subnav a:focus{'
    +'background:#304665;color:#fff}'
    +'.settings-subnav .settings-subgroup{font-size:12px;'
    +'font-weight:750;color:#84baff;padding:12px 11px 3px}'
    +'.settings-subnav a.settings-nested{padding-left:23px;'
    +'font-size:12px;color:#a9cfff}'
    +'section.panel{scroll-margin-top:22px}'
    +'/* themearr-settings-sticky-v2 */'+'aside.themearr-sidebar{position:sticky;top:0;'+'height:100vh;max-height:100vh;overflow-y:auto;'+'align-self:flex-start;overscroll-behavior:contain;'+'scrollbar-width:thin;scrollbar-color:#3b4b6b #101c30}'+'.settings-subnav .settings-group-link{'+'font-weight:750;color:#dce9ff}'+'.settings-subnav .settings-nested{'+'padding-left:25px;color:#b9d5ff}'+'/* V1.7.3 aligned settings menu: one left edge for sections, one indent for sub-items */'+'.themearr-sidebar .settings-subnav a,.themearr-sidebar .settings-subnav .settings-subgroup{display:block;margin:0;'+'padding:8px 12px;font-size:14px;font-weight:700;line-height:1.3;color:#dce9ff;text-align:left}'+'.themearr-sidebar .settings-subnav .settings-subgroup{cursor:default}'+'.themearr-sidebar .settings-subnav a.settings-nested{padding:6px 12px 6px 28px;font-size:13px;font-weight:500;color:#b9d5ff}'+'@media(max-width:700px){aside.themearr-sidebar{'+'position:relative;height:auto;max-height:none;'+'overflow:visible}}'+'/* themearr-settings-views-v3 */'
  +'main .panel{display:none!important}'
  +visible+'{display:block!important}'
  +'</style></head><body>'+full_sidebar('downloads' if section=='youtube' else 'general' if gtab else section)+'<main id="settings-general"><div class="crumb">Settings / General</div><h1>⚙️ Settings</h1><p class="muted">Account, recycle bin, host, remote access, API key and logging.</p>'+(general_tabs(section) if gtab else '')+'<div class="panel" id="settings-authentication"><div class="pill">General · Security</div><h2>🔐 Authentication</h2><p class="muted">Change your login username, password, or both from here.</p>'
   +note+'<form method="post" action="/change-account">'
   '<label for="username">Username<input id="username" name="username" value="'+username+'" '
   'autocomplete="username" minlength="3" maxlength="32" required></label>'
   '<p class="help">3–32 characters: letters, numbers, dots, underscores, or hyphens.</p>'
   '<label for="new">New password<input id="new" type="password" name="new" '
   'autocomplete="new-password" minlength="16" maxlength="128" placeholder="Leave blank to keep existing password"></label>'
   '<label for="confirm">Confirm new password<input id="confirm" type="password" name="confirm" '
   'autocomplete="new-password" maxlength="128" placeholder="Re-enter new password"></label>'
   '<p class="help">Use a new password of 16–128 characters, or leave both fields blank to change only the username.</p>'
   '<label for="current">Current password<input id="current" type="password" name="current" '
   'autocomplete="current-password" required placeholder="Required to save changes"></label>'
   '<button type="submit">Save Changes</button></form>'
   '<p class="help">After saving, you will sign in again using the new credentials. Other sessions will be signed out.</p>'
   '</div>'+_recycle_panel(h)+__import__('general_settings').panels(h)+youtube_ui.panel()
    +'<section class="panel" id="settings-ui">'
    +'<div class="pill">Interface</div>'
    +'<h2>🎨 Appearance</h2>'
    +'<p class="help">Colour scheme, OLED mode and themes are in <a href="/settings/appearance">Settings → Appearance</a>.</p>'
    +'</section>'
    +'</main></body></html>')

 def change_password(self,h):
  """Compatibility alias for the existing account edit route."""
  return self.change_account(h)

 def change_account(self,h):
  form=self.form(h)
  if form is None:return h.send(400,'Invalid account form','text/plain')
  current=form.get('current',[''])[0]
  new=form.get('new',[''])[0]
  confirm=form.get('confirm',[''])[0]
  username=form.get('username',[''])[0]
  current_user,stored_hash=self.credentials()
  if not self.verify(current):return h.send(403,self.settings(h,'Current password is incorrect'))
  if not self.valid_username(username):
   return h.send(400,self.settings(h,'Username must contain 3–32 letters, numbers, dots, underscores or hyphens'))
  if new or confirm:
   if new!=confirm or not 16<=len(new)<=128:
    return h.send(400,self.settings(h,'New password must match and be 16–128 characters'))
   salt=secrets.token_bytes(16)
   digest=hashlib.pbkdf2_hmac('sha256',new.encode(),salt,240000)
   stored_hash=salt.hex()+':'+digest.hex()
  if current_user==username and not new:
   return h.send(400,self.settings(h,'No changes requested'))
  try:self.write_account(username,stored_hash)
  except ValueError as error:return h.send(500,str(error),'text/plain')
  with self.lock:self.sessions.clear();self.born.clear()
  return self.redirect(h,'/login','themearr_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')

 def write_account(self,username,stored_hash):
  """Persist the account (0600, atomic) in the private .themearr-auth folder (0700)."""
  directory=self.account_file.parent
  if directory.is_symlink():raise ValueError('Unsafe credentials directory')
  directory.mkdir(mode=0o700,parents=True,exist_ok=True)
  os.chmod(directory,0o700)
  if self.account_file.is_symlink() or self.migrated_marker.is_symlink():raise ValueError('Unsafe account file')
  fd,temp=tempfile.mkstemp(dir=directory,prefix='.account-')
  try:
   os.fchmod(fd,0o600)
   with os.fdopen(fd,'w') as f:
    json.dump({'username':username,'password_hash':stored_hash},f,separators=(',',':'))
    f.write('\n');f.flush();os.fsync(f.fileno())
   os.replace(temp,self.account_file)
  finally:
   if os.path.exists(temp):os.unlink(temp)
  marker_fd=os.open(str(self.migrated_marker),os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
  os.close(marker_fd)

 # ------------------------------------------------------------------ first-run setup (1.12, pre-release task 5) ----
 def needs_setup(self):
  """No usable account yet (no account file, no saved hash, no THEMEARR_WRITE_AUTH): the next visitor must create one."""
  user,stored=self.credentials()
  return user is None and not (self.account_file.exists() or self.migrated_marker.exists()) or (user is not None and not self.valid_hash(stored))

 def ensure_setup_code(self):
  """A one-time code, shown ONLY in the container log, so the first person to reach the page can't claim the server."""
  with self.lock:
   if self.setup_code is None:
    alphabet='ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    raw=''.join(secrets.choice(alphabet) for _ in range(8))
    self.setup_code=raw[:4]+'-'+raw[4:]
    print('='*64+'\nPreludarr first-run setup: open the web page and enter this setup code: '+self.setup_code+
          '\n(It is shown only here, in the container log, and changes every time Preludarr restarts.)\n'+'='*64,flush=True)
   return self.setup_code

 def setup_page(self,error='',username=''):
  msg=('<p role="alert" style="color:#ff9c9c">'+html.escape(error)+'</p>') if error else ''
  return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
   '<title>Preludarr Setup</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;'
   'background:#0b1424;color:#eaf2ff;font:16px system-ui}main{box-sizing:border-box;width:min(440px,calc(100vw - 24px));background:#15223b;'
   'border:1px solid #344564;border-radius:15px;padding:30px}input,button{display:block;box-sizing:border-box;'
   'width:100%;padding:12px;margin:8px 0 14px;border-radius:8px}button{background:#457bf2;color:white;border:0;cursor:pointer;font-weight:700}'
   'input{background:#0b1424;color:white;border:1px solid #637189}.hint{color:#b7c7df;font-size:.9em;line-height:1.5}code{color:#ffd38a}</style>'
   '</head><body><main><h1>'+brand_html('login')+'</h1><h2 style="margin:0 0 6px">Create your account</h2>'
   '<p class="hint">Welcome! Preludarr has no account yet. To prove this is your server, enter the <strong>setup code</strong> from the '
   'container log (Unraid: Docker → Preludarr → Logs; other systems: <code>docker logs preludarr</code>).</p>'
   +msg+'<form method="post" action="/setup"><label>Setup code<input name="code" autocomplete="one-time-code" maxlength="9" placeholder="XXXX-XXXX" required></label>'
   '<label>Username<input name="username" autocomplete="username" maxlength="32" value="'+html.escape(username,quote=True)+'" required></label>'
   '<p class="hint" style="margin:-8px 0 10px">3–32 letters, numbers, dots, underscores or hyphens.</p>'
   '<label>Password<input name="password" type="password" autocomplete="new-password" minlength="16" maxlength="128" required></label>'
   '<label>Confirm password<input name="confirm" type="password" autocomplete="new-password" minlength="16" maxlength="128" required></label>'
   '<p class="hint" style="margin:-8px 0 10px">At least 16 characters — a passphrase of a few words is easy to remember.</p>'
   '<button>Create account</button></form></main></body></html>')

 def setup_post(self,h):
  if not self.needs_setup():return self.redirect(h,'/login')
  if not self.same_origin(h):return h.send(403,'Request origin rejected','text/plain')
  now=time.monotonic()
  with self.lock:
   self.setup_failures=[t for t in self.setup_failures if now-t<300]
   if len(self.setup_failures)>=5:return h.send(429,'Too many attempts. Try again in five minutes.','text/plain')
  form=self.form(h)
  if form is None:return h.send(400,'Invalid setup form','text/plain')
  f=lambda k:form.get(k,[''])[0]
  code,username,password,confirm=f('code').strip().upper(),f('username').strip(),f('password'),f('confirm')
  expected=self.ensure_setup_code()
  if not hmac.compare_digest(code.replace(' ',''),expected):
   with self.lock:self.setup_failures.append(now)
   return h.send(403,self.setup_page('That setup code is not right. Copy it from the container log (it changes on every restart).',username))
  if not self.valid_username(username):
   return h.send(400,self.setup_page('Username must be 3–32 letters, numbers, dots, underscores or hyphens.',username))
  if password!=confirm or not 16<=len(password)<=128:
   return h.send(400,self.setup_page('The passwords must match and be 16–128 characters long.',username))
  salt=secrets.token_bytes(16)
  stored=salt.hex()+':'+hashlib.pbkdf2_hmac('sha256',password.encode(),salt,240000).hex()
  try:self.write_account(username,stored)
  except ValueError as error:return h.send(500,str(error),'text/plain')
  with self.lock:self.setup_code=None;self.setup_failures=[]
  print('Preludarr first-run setup completed: account "'+username+'" created.',flush=True)
  cookie='themearr_session='+self.issue()+'; Path=/; HttpOnly; SameSite=Strict'+('; Secure' if via_https(h) else '')
  return self.redirect(h,'/',cookie)

 def get(self,h):
  path=urllib.parse.urlsplit(h.path).path
  if path=='/health':return True
  if path.startswith('/static/'):
   # Public branding images: needed on the login page and by Unraid; allow-listed in serve_static.
   serve_static(h,path)
   return False
  if path=='/favicon.ico':  # V1.9.7: browsers ask for this before signing in
   serve_static(h,'/static/preludarr-emblem-32.png')
   return False
  if path=='/manifest.webmanifest':
   data=manifest_json().encode()
   h.send_response(200);h.send_header('Content-Type','application/manifest+json');h.send_header('Content-Length',str(len(data)))
   h.send_header('Cache-Control','no-cache');h.end_headers();h.wfile.write(data)
   return False
  if self.needs_setup():  # 1.12: first run — everything leads to "Create your account"
   if not host_ok(h):h.send(403,'Invalid host','text/plain');return False
   if path=='/setup':self.ensure_setup_code();h.send(200,self.setup_page());return False
   if path.startswith('/api/'):h.send(503,'{"error":"First-run setup is not finished"}','application/json');return False
   self.redirect(h,'/setup');return False
  if path=='/setup':self.redirect(h,'/login');return False
  if path=='/login':
   if self.active(h):self.redirect(h,'/')
   else:h.send(200,self.page())
   return False
  import general_settings  # V1.9.7: API key for read-only API endpoints; V1.9.14: header-only by default, rate-limited
  r=general_settings.api_auth(h,path,'GET')
  if r is not None:return r
  if not self.active(h):
   if path.startswith('/api/'):
    h.send(401,'{"error":"API key or sign-in required"}','application/json')
    return False
   self.redirect(h,'/login')
   return False
  if path=='/settings':
   h.send(200,self.settings(h))
   return False
  return True

 def post(self,h):
  path=urllib.parse.urlsplit(h.path).path
  if path=='/setup' or self.needs_setup():
   if path=='/setup':self.setup_post(h)
   else:h.send(403,'First-run setup is not finished','text/plain')
   return False
  if path=='/login':
   self.login(h)
   return False
  import general_settings  # V1.9.7: API key (header only) for the few API commands; V1.9.14: scope + cooldown + rate limit
  r=general_settings.api_auth(h,path,'POST')
  if r is not None:return r
  if not self.active(h):
   self.redirect(h,'/login')
   return False
  if not self.same_origin(h):
   h.send(403,'Request origin rejected','text/plain')
   return False
  if path in ('/youtube-cookies-upload','/youtube-cookies-remove','/youtube-cookies-test'):
   youtube_ui.handle(h,path)
   return False
  if path=='/logout':
   raw=self.token(h)
   if raw:
    with self.lock:self.sessions.pop(hashlib.sha256(raw.encode()).hexdigest(),None)
   self.redirect(h,'/login','themearr_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
   return False
  if path=='/settings/remote-access':
   remote_post(h,self.redirect)
   return False
  if path=='/settings/general':
   __import__('general_settings').post(h,self.redirect)
   return False
  if path in ('/change-account','/change-password'):
   self.change_account(h)
   return False
  return True

 def decorate(self,h,body):
  body=add_icons(body)  # V1.9.7: site emblem on every page, signed in or not
  body=__import__('ui').apply(body,getattr(h,'path',''))  # V1.9.8 shared Settings/System design
  try:  # 1.12 Settings → Appearance: theme every page (Dark + Blue is sent untouched)
   ck=re.search(r'(?:^|;\s*)pl_cs=(light|dark)',str(h.headers.get('Cookie','') or ''))
   body=__import__('appearance').apply(body,prefers=ck.group(1) if ck else '')
  except Exception:pass
  if isinstance(body,str) and 'name="color-scheme"' not in body and '</head>' in body:  # 1.12 #15: dark scrollbars/controls on the default theme
   body=body.replace('</head>','<meta name="color-scheme" content="dark"></head>',1)
  if not self.active(h):return body
  sidebar_actions=('<a href="/settings">⚙️ Settings</a><a href="/system">🖥️ System</a><form method="post" action="/logout" style="margin:8px 0 0"><button type="submit" style="width:100%;text-align:left;background:#293d5b;border:1px solid #506482;border-radius:7px;color:#eaf2ff;padding:10px;cursor:pointer">↪ Log out</button></form>')
  actions=''
  # V1.9.8 (user 2026-10-27: "remove this we dont need it anymore"): the floating Settings/System/Log out box is gone.
  # Every page has the full sidebar (Settings, System, Log out). The old check missed the fixed sidebar variant
  # ('themearr-sidebar tm-full tm-fixed') and so added the box on the Theme Browser even though the sidebar was there.
  if 'themearr-sidebar tm-full' not in body and '<aside class="themearr-sidebar">' in body and '</nav>' in body:
   settings_link,logout_tail=sidebar_actions.split('<form',1)
   additions=(
    (settings_link if 'href="/settings"' not in body else '')
    +('<form'+logout_tail if 'action="/logout"' not in body else '')
   )
   if additions:
    body=body.replace('</nav>',additions+'</nav>',1)
  query=urllib.parse.urlsplit(h.path)
  if query.path=='/themes' and urllib.parse.parse_qs(query.query).get('installed')==['1']:
   actions+=('<div role="status" style="position:fixed;z-index:10001;top:65px;right:12px;'
    'background:#163d2c;color:#e1ffec;padding:15px;border:1px solid #438a63;border-radius:8px">'
    '✅ Theme installed successfully! <a style="color:#adf0c7" href="/">Dashboard</a></div>')
  return body.replace('<body>','<body>'+actions,1)


# ------------------------------------------------------------------------------------------------
# V1.7.3 shared full sidebar for every Settings page and the System page (user rule 2026-10-26:
# these pages always show ALL items; alignment: one left edge per level, one indent for sub-items).
FULL_SIDEBAR_CSS=('<style>'
 'aside.tm-full{position:sticky;top:0;align-self:flex-start;height:100vh;max-height:100vh;overflow-y:auto;box-sizing:border-box;'
 'width:236px;flex:none;padding:24px 14px;background:#101c30;border-right:1px solid #293954;'
 'display:flex;flex-direction:column;gap:22px;font:15px system-ui,-apple-system,"Segoe UI",sans-serif;scrollbar-width:thin;scrollbar-color:#3b4b6b #101c30}'
 'aside.tm-full *{box-sizing:border-box}'
 'aside.tm-full .tm-brand{padding:6px;margin:0;font-size:20px;font-weight:800}'
 'aside.tm-full .tm-nav{display:flex;flex-direction:column;gap:6px;margin:0;padding:0}'
 'aside.tm-full .tm-nav>a{display:block;margin:0;padding:12px;border:0;border-radius:8px;color:#c3d3ec;text-decoration:none;'
 'font-size:15px;font-weight:400;line-height:1.3;text-align:left}'
 'aside.tm-full .tm-nav>a:hover,aside.tm-full .tm-nav>a.active{background:#223a61;color:#fff;text-decoration:none}'
 'aside.tm-full .tm-nav>a .tm-hint{display:block;margin-top:2px;font-size:12px;font-weight:400;line-height:1.3;color:#8fa4c5}'
 'aside.tm-full .tm-nav>a.active .tm-hint,aside.tm-full .tm-nav>a:hover .tm-hint{color:#c6d6ee}'
 'aside.tm-full .settings-subnav{display:flex;flex-direction:column;gap:3px;margin:-2px 0 0 13px;padding:7px 0 7px 13px;'
 'border-left:2px solid #476588;background:#15233a;border-radius:0 7px 7px 0}'
 'aside.tm-full .settings-subnav a,aside.tm-full .settings-subnav .settings-subgroup{display:block;margin:0;padding:8px 12px;'
 'border-radius:6px;font-size:14px;font-weight:700;line-height:1.3;color:#dce9ff;text-align:left;text-decoration:none}'
 'aside.tm-full .settings-subnav .settings-subgroup{cursor:default}'
 'aside.tm-full .settings-subnav a.settings-nested{padding:8px 12px;font-size:14px;font-weight:700;color:#dce9ff}'  # one straight line (user 2026-10-26)
 'aside.tm-full .settings-subnav a:hover,aside.tm-full .settings-subnav a.active{background:#304665;color:#fff}'
 'aside.tm-full .tm-logout{margin:0}'
 'aside.tm-full .tm-logout button{display:block;width:100%;margin:0;padding:12px;border:0;border-radius:8px;background:#293b57;'
 'color:#eaf2ff;font:inherit;font-weight:700;text-align:left;cursor:pointer}'
 'aside.tm-full .tm-logout button:hover{background:#34507a}'
 'aside.tm-full .tm-footer{margin-top:auto;padding:6px;color:#8fa4c5;font-size:12px}'
 '@media(max-width:700px){aside.tm-full{position:static;width:100%;height:auto;max-height:none;border-right:0;'
 'border-bottom:1px solid #293954;gap:10px;padding:12px}aside.tm-full .tm-nav{flex-direction:row;flex-wrap:wrap}'
 'aside.tm-full .tm-nav>a{padding:7px 9px}aside.tm-full .settings-subnav{flex-basis:100%;margin-left:0}aside.tm-full .tm-footer{display:none}}'
 'aside.tm-full a,aside.tm-full a:hover,aside.tm-full a:visited{text-decoration:none!important}'
 'aside.tm-full .tm-nav>a,aside.tm-full .tm-nav>a:visited{color:#c3d3ec!important}'
 'aside.tm-full .tm-nav>a.active,aside.tm-full .tm-nav>a:hover{color:#fff!important}'
 'aside.tm-full .settings-subnav a,aside.tm-full .settings-subnav a:visited{color:#dce9ff!important}'
 'aside.tm-full .settings-subnav a.settings-nested{color:#dce9ff!important}'
 'aside.tm-full .settings-subnav a.active,aside.tm-full .settings-subnav a:hover{color:#fff!important}'
 'aside.tm-full .tm-logout button{background:#293b57!important;color:#eaf2ff!important;box-shadow:none!important;border:0!important;padding:12px!important}'
 'aside.tm-full .tm-logout button:hover{background:#34507a!important}'
 'aside.tm-full .tm-brand img{display:block}'
 'aside.tm-full.tm-fixed{position:fixed;left:0;top:0;bottom:0;height:100vh;max-height:100vh;z-index:20}'
 '.tm-fixed~.themearr-content{margin-left:236px}'
 '@media(max-width:800px){aside.tm-full.tm-fixed{position:static;width:100%;height:auto}.tm-fixed~.themearr-content{margin-left:0}}'
 # V1.9.12: tablets and phones (≤1100px): the sidebar becomes a slide-in drawer behind a fixed "☰ Menu" button; pages get the full width
 '.tm-menu-btn,.tm-scrim{display:none}'
 '@media(max-width:1100px){'
 'aside.tm-full,aside.tm-full.tm-fixed{position:fixed!important;top:0!important;bottom:0;left:-300px;width:280px!important;height:100vh!important;max-height:100vh!important;'
 'flex-direction:column!important;gap:18px!important;padding:64px 14px 24px!important;border-right:1px solid #293954!important;border-bottom:0!important;z-index:60;'
 'visibility:hidden;transition:left .2s ease,visibility .2s;overflow-y:auto}'
 'aside.tm-full.tm-open{left:0;visibility:visible;box-shadow:0 0 48px #000b}'
 'aside.tm-full .tm-nav{flex-direction:column!important;flex-wrap:nowrap!important}aside.tm-full .tm-nav>a{padding:12px!important}'
 'aside.tm-full .settings-subnav{margin-left:13px!important}aside.tm-full .tm-footer{display:block!important}'
 '.tm-fixed~.themearr-content{margin-left:0!important}body{padding-top:58px!important}'
 'aside.tm-full .tm-menu-btn{display:inline-flex;align-items:center;gap:6px;position:fixed;top:10px;left:10px;z-index:2;visibility:visible;'
 'padding:9px 14px!important;border:1px solid #3e5174!important;border-radius:9px!important;background:#16233a!important;color:#eef4ff!important;'
 'font:600 15px system-ui,-apple-system,"Segoe UI",sans-serif!important;cursor:pointer;box-shadow:0 4px 14px #0007}'
 'aside.tm-full .tm-menu-btn:focus-visible{outline:3px solid #8fc1ff;outline-offset:2px}'
 'aside.tm-full .tm-scrim{display:block;position:fixed;top:0;right:0;bottom:0;left:280px;background:#02061299}'
 '@media (prefers-reduced-motion:reduce){aside.tm-full,aside.tm-full.tm-fixed{transition:none}}}'
 '</style>')

_MAIN_NAV=(('dashboard','/','🏠 Dashboard'),('series','/?view=library&library=Anime+TV','🍜 Anime Series'),
 ('movies','/?view=library&library=Anime+Movies','🍥 Anime Movies'),('themes','/themes','🎶 Theme Browser'),
 ('missing','/?view=library&state=missing','📥 Missing Themes'),('activity','/system?tab=activity','📊 Activity'),
 ('manage','/manage-themes','🎧 Theme Management'),('queue','/queue','🧾 Theme Queue'),('settings',None,None),('system','/system','🖥️ System'))
# order per user 2026-10-26: Settings, System, UI. V1.9.9: the UI entry is hidden until real appearance settings exist;
# /settings?section=ui still works for old bookmarks.
# V1.9.14: one-line hints so the four theme pages are told apart (routes and labels unchanged)
_NAV_HINTS={'library':'Your titles, a tab per root folder','collections':'Theme songs for movie collections','themes':'Find and preview themes for a title','missing':'Titles that have no theme yet',
            'manage':'Themes already installed','queue':'Proposed installs waiting for approval'}
_SETTINGS_NAV=(('settings-group-link','general','/settings?section=general','General'),
 ('settings-group-link','media','/media-management','Media Management'),
 ('settings-group-link','connect','/media-servers','Connect'),
 ('settings-group-link','downloads','/download-clients','Download Clients'),
 ('settings-group-link','appearance','/settings/appearance','Appearance'))  # 1.12


GENERAL_TABS=(('general','Account'),('recycle','Recycle bin'),('host','Host'),('remote','Remote access'),('security','API key & security'),('logging','Logging'))


def general_tabs(current):
 """Settings → General tabs: real links, each tab shows only its own section (V1.9.8)."""
 return ('<nav class="tm-subnav tm-tabs" aria-label="General settings">'+''.join(
  '<a href="/settings?section='+k+'"'+(' class="on" aria-current="page"' if k==current else '')+'>'+html.escape(label)+'</a>' for k,label in GENERAL_TABS)+'</nav>')


def remote_panel(h):
 """Settings → General → Remote access (V1.9.6)."""
 q=urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
 notice=q.get('remote_notice',[''])[0][:300]
 saved=''
 try:
  saved=(json.loads((Path(os.environ.get('CONFIG_DIR','/config'))/PUBLIC_FILE).read_text()).get('urls') or [''])[0]
 except (OSError,ValueError,TypeError,AttributeError,IndexError):
  pass
 env=os.environ.get('THEMEARR_PUBLIC_URL','').strip()
 here=h.headers.get('Host','').lower()
 bad=notice.startswith('✗')
 return ('<section class="panel" id="settings-remote-access"><div class="pill">General · Remote access</div><h2>🌐 Remote access</h2>'
  '<p class="muted">Using Preludarr through a reverse proxy or a Cloudflare tunnel? Enter the public HTTPS address here so the sign-in page accepts it. '
  'Your LAN address keeps working.</p>'
  +('<div role="'+('alert' if bad else 'status')+'" class="notice">'+html.escape(notice.lstrip('✗ '))+'</div>' if notice else '')+
  '<form method="post" action="/settings/remote-access" autocomplete="off">'
  '<label for="remote-url">Public address (HTTPS)<input id="remote-url" name="url" maxlength="200" placeholder="https://themearr.example.com — blank = LAN only" '
  'value="'+html.escape(saved,quote=True)+'" data-bwignore="true" data-lpignore="true" data-1p-ignore="true"></label>'
  '<p class="help">Only this exact HTTPS address is accepted, in addition to the LAN address. Sign-in cookies are marked Secure on it.'
  +(' Also set by the Docker variable THEMEARR_PUBLIC_URL: <code>'+html.escape(env)+'</code>.' if env else '')+'</p>'
  '<p class="help">Recommended: put Cloudflare Access (Zero Trust login) in front of the tunnel, so only you can reach the Preludarr sign-in page.</p>'
  '<p class="help">You are using: <code>'+html.escape(here)+'</code>.</p>'
  '<button type="submit">Save Remote Access</button></form></section>')


def remote_post(h,redirect):
 """Save the public address. Same-origin + session were already checked by Auth.post."""
 try:
  size=int(h.headers.get('Content-Length','0') or 0)
  if not 0<size<=1024:raise ValueError('Invalid form.')
  form=urllib.parse.parse_qs(h.rfile.read(size).decode('utf-8','strict'),keep_blank_values=True,max_num_fields=4)
  raw=form.get('url',[''])[0].strip()
  urls=[]
  if raw:
   host=parse_public_url(raw)
   urls=['https://'+host]
  path=Path(os.environ.get('CONFIG_DIR','/config'))/PUBLIC_FILE
  tmp=path.with_name('.'+PUBLIC_FILE+'.part')
  fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600)
  with os.fdopen(fd,'w') as out:json.dump({'urls':urls},out)
  os.replace(tmp,path)
  msg=('Remote access saved: '+urls[0]+' is accepted now (no restart needed).') if urls else 'Remote access saved: LAN only.'
 except ValueError as error:
  msg='✗ '+str(error)
 except OSError:
  msg='✗ Could not save the setting.'
 return redirect(h,'/settings?'+urllib.parse.urlencode({'section':'remote','remote_notice':msg}))


def _recycle_panel(h):
 """V1.9.5 (user): Recycle bin settings under Authentication on Settings → General."""
 try:
  import theme_review
  notice=urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query).get('recycle_notice',[''])[0][:400]
  return theme_review.settings_panel(os.getenv('CONFIG_DIR','/config'),notice)
 except Exception as error:
  return '<section class="panel" id="settings-recycle-bin"><h2>♻️ Recycle bin</h2><p class="help">Recycle bin settings could not be loaded ('+html.escape(type(error).__name__)+').</p></section>'

# V1.9.12: drawer toggle for tablets/phones. Lives inside the <aside> (settings_shell callers close the aside themselves);
# the button is position:fixed and visibility:visible, so it shows while the closed drawer is hidden (closed links are not tabbable).
_MENU_TOGGLE=('<button type="button" class="tm-menu-btn" aria-controls="tm-sidebar" aria-expanded="false">☰ Menu</button><div class="tm-scrim"></div>'
 '<script>(function(){"use strict";const a=document.getElementById("tm-sidebar");const b=a&&a.querySelector(".tm-menu-btn");if(!b)return;'
 'function set(o){a.classList.toggle("tm-open",o);b.setAttribute("aria-expanded",String(o));b.textContent=o?"✕ Close":"☰ Menu";'
 'if(o){const f=a.querySelector(".tm-nav a.active")||a.querySelector(".tm-nav a");if(f)setTimeout(()=>f.focus(),230)}}'  # after the slide-in: hidden links can't take focus
 'b.addEventListener("click",()=>set(!a.classList.contains("tm-open")));'
 'const sc=a.querySelector(".tm-scrim");if(sc)sc.addEventListener("click",()=>set(false));'
 'document.addEventListener("keydown",e=>{if(e.key==="Escape"&&a.classList.contains("tm-open")){set(false);b.focus()}});'
 'addEventListener("resize",()=>{if(innerWidth>1100&&a.classList.contains("tm-open"))set(false)})})();</script>')


def library_first():
 """Preludarr 1.11.9: the Library link opens the first root folder's tab."""
 import sys,root_folders as rf
 app=next((m for m in (sys.modules.get('__main__'),sys.modules.get('app')) if m is not None and hasattr(m,'ROOTS')),None)
 return rf.root_value(app.ROOTS[0][1]) if app is not None and app.ROOTS else 'all'


def library_kinds():
 """Root-folder types that exist, in tab order (Anime TV / Anime Movies / Shows / Movies)."""
 import sys,kinds as k
 app=next((m for m in (sys.modules.get('__main__'),sys.modules.get('app')) if m is not None and hasattr(m,'ROOTS')),None)
 return (k.present(app.ROOTS) if app is not None else ['Anime TV','Anime Movies']) or ['Anime TV']


def _nav_items():
 """Preludarr 1.11 (user 2026-10-29): Anime Series + Anime Movies become ONE "Library" entry; the types are tabs inside the
 Library page. Collections is its own entry (user 2026-10-29: "it is its own tab")."""
 import urllib.parse as up
 out=[]
 for item in _MAIN_NAV:
  if item[0]=='series':out+=[('library','/?'+up.urlencode({'view':'library','library':library_first()}),'📚 Library'),('collections','/collections','🗂️ Collections')]
  elif item[0]!='movies':out.append(item)
 return out


def full_sidebar(active='',fixed=False):
 """Whole app sidebar (all items + Settings submenu + Log out). `active`: a main key (e.g. 'system') or a
 Settings key ('general','media','connect','youtube','ui'). Returns CSS + <aside ...>…</aside>."""
 settings_keys={k for _,k,_,_ in _SETTINGS_NAV if k}
 open_settings=active in settings_keys  # Settings list opens only on Settings pages (System stays separate, user 2026-10-26)
 parts=[FULL_SIDEBAR_CSS,'<aside id="tm-sidebar" class="themearr-sidebar tm-full'+(' tm-fixed' if fixed else '')+'" aria-label="Main navigation"><div class="tm-brand">',brand_html(),'</div><nav class="tm-nav">']
 for key,href,label in _nav_items():
  if key=='settings':
   parts.append('<a href="/settings"'+(' class="active"' if active in settings_keys else '')+'>⚙️ Settings'+(' ▾' if open_settings else '')+'</a>')
   if open_settings:
    parts.append('<div class="settings-subnav" aria-label="Settings sections">')
    for cls,skey,shref,slabel in _SETTINGS_NAV:
     on=skey==active
     parts.append('<a class="'+cls+(' active' if on else '')+'" href="'+shref+'"'+(' aria-current="page"' if on else '')+'>'+slabel+'</a>')
    parts.append('</div>')
   continue
  on=key==active
  hint=_NAV_HINTS.get(key,'')
  parts.append('<a href="'+href+'"'+(' class="active" aria-current="page"' if on else '')+(' title="'+hint+'"' if hint else '')+'>'+label
               +('<small class="tm-hint">'+hint+'</small>' if hint else '')+'</a>')
 parts.append('</nav><form class="tm-logout" method="post" action="/logout"><button type="submit">↪ Log out</button></form>'
              '<div class="tm-footer">👑 Preludarr v'+APP_VERSION+__import__('updates').footer_html(APP_VERSION)+'</div>'+_MENU_TOGGLE+'</aside>')
 return ''.join(parts)
