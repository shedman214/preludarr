#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preludarr (formerly Splicarr and Themearr): self-hosted theme songs for your library. Run: python3 app.py"""
import os, sys, json, sqlite3, threading, time, re, urllib.request, urllib.parse, subprocess, tempfile, shutil, html, hashlib, contextlib, unicodedata
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
# Settings may use PRELUDARR_* (1.12), SPLICARR_* (1.10/1.11) or the original THEMEARR_* names; an existing THEMEARR_* value wins
# (THEMEARR_* wins if both are set, so an existing template is never overridden).
for _k in ('WRITE_AUTH','EXTRA_ALLOWED_HOSTS','LIBRARY_MOUNTS','MANUAL_INSTALL_ENABLED','PUBLIC_URL','DEMO'):
 for _p in ('SPLICARR_','PRELUDARR_'):
  if os.environ.get(_p+_k) is not None and os.environ.get('THEMEARR_'+_k) is None:os.environ['THEMEARR_'+_k]=os.environ[_p+_k]
# 1.12: a restore confirmed on the Backup page runs here, before any module opens the database or a settings file.
if __name__=='__main__':__import__('restore').apply_pending(os.getenv('CONFIG_DIR','/config'))
from pathlib import Path
from datetime import datetime, timezone
import custom_upload
import poster_connect
import poster_format
import poster_filters
import release_notes
import system
import theme_management
import youtube_manual
import poster_manual
import animethemes
import anidb
import root_folders
import auto_themes
import unmapped
import permissions
import library_health  # V1.9.3 Library Health / Path Validator
import auto_backup
import ratelimit  # V1.9.14 per-source rate limits
import theme_review  # V1.9.4 review extra theme files + theme trash
import arr
import sources
import kinds  # Preludarr 1.11: Anime / Anime Movies / Shows / Movies
import httpserver  # 1.12: hardened HTTP server
import movie_collections  # 1.12: Collections
import plex_signin  # 1.12: Sign in with Plex
import jellyfin_quickconnect  # 1.12: Jellyfin Quick Connect
import emby_signin  # 1.12: sign in to Emby
import appearance  # 1.12: Settings → Appearance
import theme_queue

CONFIG=Path(os.getenv('CONFIG_DIR','/config')); CONFIG.mkdir(parents=True,exist_ok=True)
DB=CONFIG/'library.db'
EXISTING_INSTALL=DB.exists()  # 1.12: before this start created anything (API default migration)
ROOTS=[('Anime TV',Path(os.getenv('TV_ROOT','/anime'))),('Anime Movies',Path(os.getenv('MOVIE_ROOT','/movies')))]
INTERVAL=max(1,int(os.getenv('SCAN_INTERVAL_HOURS','12')))*3600
PORT=int(os.getenv('PORT','8787'))
AUTO=os.getenv('AUTO_DOWNLOAD','false').lower()=='true'
MAX_PER_SCAN=max(1,int(os.getenv('MAX_DOWNLOADS_PER_SCAN','5')))
TMDB_KEY=os.getenv('TMDB_API_KEY','').strip()
TVDB_KEY=os.getenv('TVDB_API_KEY','').strip()
POSTER_PROVIDER=os.getenv('POSTER_PROVIDER','tmdb').strip().lower()
POSTER_DIR=CONFIG/'posters'; POSTER_DIR.mkdir(parents=True,exist_ok=True)
from login import APP_VERSION, brand_html, full_sidebar

import app_logs
app_logs.install(CONFIG)
custom_upload.sweep_stale(CONFIG)
POSTER_LOCK=threading.Lock()
POSTER_BUSY=set()
poster_manual.sweep_stale(CONFIG)
root_folders.load(sys.modules[__name__])  # V1.7.2: registered root folders (seeded from TV_ROOT/MOVIE_ROOT)
if anidb.enabled(CONFIG):anidb.ensure(CONFIG)  # V1.7.0: load (and at most daily refresh) the AniDB title list in the background
LOCK=threading.Lock()
STATUS={'running':False,'message':'Idle','last_scan':None,'active_path':None,'stage':'idle'}

def db():
 c=sqlite3.connect(DB,timeout=15); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL')
 c.execute('CREATE TABLE IF NOT EXISTS shows (path TEXT PRIMARY KEY, library TEXT, name TEXT, state TEXT, detail TEXT, updated TEXT)')
 c.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, name TEXT, status TEXT, detail TEXT)')
 c.execute('CREATE TABLE IF NOT EXISTS posters (path TEXT PRIMARY KEY, status TEXT, checked INTEGER, provider TEXT)')
 c.execute('CREATE TABLE IF NOT EXISTS poster_attempts (path TEXT PRIMARY KEY, at INTEGER, outcome TEXT, reason TEXT, candidates TEXT)')
 c.execute('CREATE TABLE IF NOT EXISTS show_added (path TEXT PRIMARY KEY, added INTEGER, baseline INTEGER)')  # V1.7.3 Recently added
 c.execute('CREATE TABLE IF NOT EXISTS media_ids (path TEXT PRIMARY KEY, tvdb INTEGER, tmdb INTEGER, imdb TEXT, source TEXT)')  # V1.9.0 secondary sources
 c.execute('CREATE TABLE IF NOT EXISTS theme_suggestions (path TEXT PRIMARY KEY, found TEXT, notes TEXT, fetched INTEGER)')  # Preludarr 1.11 Shows/Movies suggestions
 c.execute('CREATE TABLE IF NOT EXISTS youtube_search_cache (path TEXT PRIMARY KEY, query TEXT, results TEXT, fetched INTEGER)')  # Preludarr 1.11, kept <= 30 days
 c.execute('CREATE TABLE IF NOT EXISTS theme_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, name TEXT, library TEXT, state TEXT, source TEXT, proposal TEXT, note TEXT, created INTEGER, updated INTEGER)')  # V1.9.0 Bulk Theme Queue
 c.commit();return c

def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')
_STATE_LABEL={'installed':'Theme installed','review':'Needs review','missing':'Missing','searching':'Searching','downloading':'Downloading','converting':'Converting'}
def last_op(path,name,state,detail=''):
 """V1.9.14: remember the last single-title outcome WITH its title and time (the dashboard used to show a bare global message)."""
 STATUS['last']={'path':str(path),'title':str(name),'state':state,'detail':str(detail or '')[:200],'at':time.time()}
def status_display():
 """One honest line for the dashboard: what runs now, or the last title outcome (+ its current state if that changed since)."""
 if STATUS.get('running'):return str(STATUS.get('message') or 'Working')+' (in progress…)'
 last=STATUS.get('last')
 if not last:return 'Idle'
 text='Last title: '+last['title']+' — '+_STATE_LABEL.get(last['state'],last['state'])
 if last['state']=='review' and last['detail']:text+=': '+last['detail']
 text+=' · '+__import__('ui').when(last['at'])
 try:
  with db() as c:row=c.execute('SELECT state FROM shows WHERE path=?',(last['path'],)).fetchone()
  if row and row['state']!=last['state']:text+=' (now '+_STATE_LABEL.get(row['state'],row['state'])+')'
 except Exception:pass
 return text
def scan_line():
 if not STATUS.get('last_scan'):return 'No library scan since Preludarr started.'
 try:t=datetime.fromisoformat(str(STATUS['last_scan']))
 except ValueError:return 'Last library scan: '+str(STATUS['last_scan'])
 return 'Last library scan: '+__import__('ui').when(t)+(' · '+STATUS['scan_summary'] if STATUS.get('scan_summary') else '')
def event(name,status,detail):
 with db() as c:c.execute('INSERT INTO events(at,name,status,detail) VALUES(?,?,?,?)',(now(),name,status,detail[:800]))
def save(path,library,name,state,detail=''):
 with db() as c:c.execute('INSERT INTO shows VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET library=excluded.library,name=excluded.name,state=excluded.state,detail=excluded.detail,updated=excluded.updated',(str(path),library,name,state,detail[:800],now()))
def request_json(url, data=None, fresh=False):
 headers={'User-Agent':'Preludarr/1.12 (self-hosted theme manager)','Accept':'application/json'}
 if data is None and urllib.parse.urlsplit(url).hostname==animethemes.API_HOST:
  # V1.6.4: paced, retried and remembered (same data); fresh=True always asks AnimeThemes again.
  return animethemes.get_json(url,headers,fresh)
 if data is not None:headers['Content-Type']='application/json'
 req=urllib.request.Request(url,data=json.dumps(data).encode() if data is not None else None,headers=headers)
 source=ratelimit.for_host(urllib.parse.urlsplit(url).hostname)  # V1.9.14: per-source pacing (AniList, TMDB, TVDB …)
 if source:ratelimit.wait(source)
 try:
  with _JSON_OPENER.open(req,timeout=18) as r:
   body=r.read(JSON_MAX_BYTES+1)  # V1.9.14: bounded answer size
 except urllib.error.HTTPError as error:
  if error.code==429 and source:ratelimit.backoff(source,(error.headers or {}).get('Retry-After') if hasattr(error,'headers') else 30)
  raise
 if len(body)>JSON_MAX_BYTES:raise ValueError('Provider answer exceeds the 8 MB limit')
 return json.loads(body)
JSON_MAX_BYTES=8*1024*1024
class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
 """V1.9.14: provider requests follow redirects only to https:// (never to http, file, or anything else)."""
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  if urllib.parse.urlsplit(newurl).scheme!='https':raise urllib.error.HTTPError(newurl,code,'Redirect to a non-HTTPS address refused',headers,fp)
  return super().redirect_request(req,fp,code,msg,headers,newurl)
_JSON_OPENER=urllib.request.build_opener(_HttpsOnlyRedirect)
ANIMETHEMES_AUDIO_HOSTS=('a.animethemes.moe','v.animethemes.moe')
def _approved(url,hosts):
 p=urllib.parse.urlsplit(url)
 return p.scheme=='https' and p.hostname in hosts and not p.username and not p.password and p.port in (None,443)
class _ApprovedRedirect(urllib.request.HTTPRedirectHandler):
 """1.12 review finding: a download follows a redirect only to https:// on the same approved hosts (checked BEFORE following)."""
 def __init__(self,hosts):
  super().__init__();self.hosts=hosts
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  if not _approved(newurl,self.hosts):raise urllib.error.HTTPError(newurl,code,'Redirect to an unapproved address refused',headers,fp)
  return super().redirect_request(req,fp,code,msg,headers,newurl)
def open_approved(req,hosts,timeout):
 """urlopen for poster / theme audio downloads: start and every redirect must be on `hosts`; the final address is checked again."""
 if not _approved(req.full_url,hosts):raise ValueError('Download address is not from an approved provider')
 response=urllib.request.build_opener(_ApprovedRedirect(hosts)).open(req,timeout=timeout)
 if not _approved(response.geturl(),hosts):
  response.close();raise ValueError('Download ended on an unapproved address')
 return response
def poster_file(path):
 return POSTER_DIR/(hashlib.sha256(path.encode()).hexdigest()+'.jpg')


def poster_titles(name):
 titles=[clean(name)]
 try:
  media=anilist(name)
 except Exception as exc:
  if not (isinstance(exc,urllib.error.HTTPError) and exc.code==404):  # 1.12: 404 = AniList has no match — normal, not an error
   print('Poster AniList lookup: '+str(exc)[:120],flush=True)
  media=None
 if not media:
  # V1.6.2: AniList often misses titles with decorative punctuation; try once more with a plain variant.
  plain=' '.join(re.sub(r'[^\w]+',' ',clean(name),flags=re.UNICODE).split())
  if plain and plain.casefold()!=clean(name).casefold():
   try:
    media=anilist(plain)
   except Exception as exc:
    if not (isinstance(exc,urllib.error.HTTPError) and exc.code==404):
     print('Poster AniList lookup (plain title): '+str(exc)[:120],flush=True)
 yr=year(name)
 if media and (not yr or not media.get('seasonYear') or media['seasonYear']==yr):
  titles += [v for v in (media.get('title') or {}).values() if v]
  titles += media.get('synonyms') or []
 return list(dict.fromkeys(t for t in titles if t))[:12]

def poster_title_match(candidate,titles):
 value=norm(candidate)
 if not value:return False
 for title in titles:
  expected=norm(title)
  if not expected:continue
  if value==expected:return True
  # Allow provider prefixes, e.g. "Beyblade: Metal Fight Beyblade".
  if len(expected)>=12 and value.endswith(expected):
   prefix=value[:-len(expected)]
   if len(prefix)>=4 and any(norm(t).startswith(prefix) for t in titles):
    return True
 return False

# ---- V1.6.2 poster matching additions: run only after the exact checks above find nothing ----
POSTER_SEASON=re.compile(r'(?i)\b(season|part|cour|s\d+|\d+(st|nd|rd|th)|ii|iii|iv|movie|film|ova|ona|special|specials|recap)\b')

def poster_key(value):
 """Unicode-aware comparison key; norm() keeps only a-z0-9, which erases Japanese/Chinese titles."""
 value=unicodedata.normalize('NFKC',str(value or '')).casefold()
 return ''.join(ch for ch in value if ch.isalnum())

def poster_unicode_exact(candidate,titles):
 """Exact match for titles in non-Latin scripts (e.g. TMDB original_name 斗破苍穹 = AniList native)."""
 key=poster_key(candidate)
 if not key or key.isascii():return False
 return any(poster_key(t)==key for t in titles)

def poster_near_match(candidates,titles,yr,providers):
 """Second pass. Auto-accept only one credible result from a provider: same year, poster present, and a
 very close title (>= 0.90) or the folder title = provider title + a subtitle (never a season/part/movie
 marker). Returns (chosen candidate or None, year-matching candidates for manual choice)."""
 import difflib
 if not yr:return None,[]
 choices=[c for c in candidates if c.get('poster') and c.get('year')==str(yr)]
 seen=set();unique=[]
 for c in choices:
  if (c['provider'],c['id']) not in seen:
   seen.add((c['provider'],c['id']));unique.append(c)
 for provider in providers:
  credible=[]
  for c in unique:
   if c['provider']!=provider.upper():continue
   best,how=0.0,''
   for value in (c.get('title'),c.get('original')):
    key=poster_key(value)
    if len(key)<4:continue
    for t in titles:
     tk=poster_key(t)
     if not tk:continue
     ratio=difflib.SequenceMatcher(None,key,tk).ratio()
     if ratio>best:best,how=ratio,f'close title match ({ratio:.2f})'
     if len(key)>=12 and tk.startswith(key) and tk!=key and not POSTER_SEASON.search(t) and best<0.90:
      best,how=0.90,'folder title adds a subtitle'
   if best>=0.90:credible.append({**c,'why':how})
  if len(credible)==1:return credible[0],unique
  if len(credible)>1:return None,unique
 return None,unique

def media_ids_for(path):
 """TVDB/TMDB/IMDb IDs Sonarr/Radarr reported for this folder (V1.9.0 media_ids table), or {}."""
 try:
  with db() as c:r=c.execute('SELECT tvdb,tmdb,imdb,source FROM media_ids WHERE path=?',(str(path),)).fetchone()
  return {k:r[k] for k in ('tvdb','tmdb','imdb','source') if r[k]} if r else {}
 except Exception:return {}

def poster_id_match(path,candidates):
 """V1.9.14: a candidate whose provider ID equals the ID Sonarr/Radarr gave for this folder is the same title — apply it.
 At most one candidate per provider can carry that ID; TMDB and TVDB hits name the same title."""
 ids=media_ids_for(path)
 hits=[]
 for c in candidates:
  if not c.get('poster'):continue
  want=ids.get(str(c.get('provider','')).lower())
  if want and str(c.get('id'))==str(want) and all(h['provider']!=c['provider'] for h in hits):
   hits.append({**c,'why':'same '+c['provider']+' ID as '+str(ids.get('source') or 'Sonarr/Radarr').capitalize()+' ('+str(want)+')'})
 return hits[0] if hits else None  # one per provider; TMDB and TVDB hits name the same title, search order = preference

def record_poster_attempt(path,outcome,reason='',candidates=None):
 """Latest poster search result per anime (outcome: found / no_match / needs_choice / provider_error / queued)."""
 try:
  with db() as c:
   c.execute('INSERT OR REPLACE INTO poster_attempts VALUES(?,?,?,?,?)',
             (path,int(time.time()),outcome,str(reason)[:300],json.dumps((candidates or [])[:8],ensure_ascii=False)))
 except Exception as exc:
  print('Poster attempt log error: '+type(exc).__name__,flush=True)

def fetch_poster(path,library,name):
 # Run only in a daemon thread; never block dashboard rendering.
 try:
  TMDB_KEY,TVDB_KEY,TVDB_PIN,POSTER_PROVIDER=poster_connect.effective_values(CONFIG)
  if poster_file(path).is_file():return
  providers=['tvdb','tmdb'] if POSTER_PROVIDER=='tvdb' else ['tmdb','tvdb']
  titles=poster_titles(name)
  yr=year(name)
  poster_url=None
  provider=''
  seen_candidates=[]
  provider_errors=[]
  match_reason='exact title'
  for which in providers:
   try:
    if which=='tmdb' and TMDB_KEY:
     category='movie' if kinds.is_movie(library) else 'tv'
     for title in titles:
      qs=urllib.parse.urlencode({'api_key':TMDB_KEY,'query':title})
      result=request_json('https://api.themoviedb.org/3/search/'+category+'?'+qs)
      for item in result.get('results') or []:
       candidate=item.get('title') or item.get('name') or ''
       original=item.get('original_title') or item.get('original_name') or ''
       date=item.get('release_date') or item.get('first_air_date') or ''
       seen_candidates.append({'provider':'TMDB','id':item.get('id'),'title':candidate,'original':original,'year':date[:4],
                               'poster':('https://image.tmdb.org/t/p/w342'+item['poster_path']) if item.get('poster_path') else ''})
       if not item.get('poster_path'):continue
       if yr and date[:4].isdigit() and int(date[:4])!=yr:continue
       if not (poster_title_match(candidate,titles) or poster_title_match(original,titles)
               or poster_unicode_exact(original,titles)):
        continue
       poster_url='https://image.tmdb.org/t/p/w342'+item['poster_path']
       provider='TMDB'
       break
      if poster_url:break
     if poster_url:break
    if which=='tvdb' and TVDB_KEY:
     login=request_json('https://api4.thetvdb.com/v4/login',poster_connect.tvdb_login_payload(TVDB_KEY,TVDB_PIN))
     token=(login.get('data') or {}).get('token')
     if not token:continue
     for title in titles:
      q=urllib.parse.urlencode({'query':title,'type':'movie' if kinds.is_movie(library) else 'series'})
      req=urllib.request.Request(
       'https://api4.thetvdb.com/v4/search?'+q,
       headers={'Authorization':'Bearer '+token,'Accept':'application/json','User-Agent':'Preludarr/1.12 (self-hosted theme manager)'})
      ratelimit.wait('tvdb')  # V1.9.14
      with urllib.request.urlopen(req,timeout=18) as response:
       items=json.load(response).get('data') or []
      for item in items:
       candidate=item.get('name') or ''
       first=str(item.get('first_air_time') or item.get('year') or '')
       url=item.get('image_url') or ''
       checked_url=urllib.parse.urlsplit(url)
       seen_candidates.append({'provider':'TVDB','id':item.get('tvdb_id') or item.get('id'),'title':candidate,'original':'',
                               'year':first[:4],'poster':url if checked_url.scheme=='https' and checked_url.hostname in (
                                'artworks.thetvdb.com','www.thetvdb.com','thetvdb.com') else ''})
       if yr and first[:4].isdigit() and int(first[:4])!=yr:continue
       if not poster_title_match(candidate,titles):continue
       parsed=urllib.parse.urlsplit(url)
       if parsed.scheme=='https' and parsed.hostname in (
        'artworks.thetvdb.com','www.thetvdb.com','thetvdb.com'):
        poster_url=url
        provider='TVDB'
        break
      if poster_url:break
     if poster_url:break
   except Exception as exc:
    print('Poster lookup error ('+which+'): '+type(exc).__name__,flush=True)
    code=getattr(exc,'code',None)
    provider_errors.append(which.upper()+': '+(f'HTTP {code}' if code else type(exc).__name__))
  near_choices=[]
  if not poster_url:
   chosen=poster_id_match(path,seen_candidates)  # V1.9.14: the exact TMDB/TVDB ID Sonarr/Radarr gave for this folder
   if chosen:
    poster_url,provider,match_reason=chosen['poster'],chosen['provider'],chosen['why']
  if not poster_url:
   chosen,near_choices=poster_near_match(seen_candidates,titles,yr,providers)
   if chosen:
    poster_url,provider,match_reason=chosen['poster'],chosen['provider'],chosen['why']
  if poster_url:
   req=urllib.request.Request(poster_url,headers={'User-Agent':'Preludarr/1.12 (self-hosted theme manager)'})
   with open_approved(req,__import__('poster_manual').IMAGE_HOSTS,25) as response:
    content=response.read(3*1024*1024+1)
    ctype=response.headers.get('Content-Type','')
   if len(content)>3*1024*1024 or not ctype.startswith('image/') or not content:
    raise ValueError('Invalid poster image')
   file=poster_file(path)
   # Write an exclusive, crash-safe temporary file and link only when
   # destination is absent. Neither retries nor overlapping jobs overwrite.
   if not content.startswith(b'\xff\xd8'):
    content=poster_format.to_jpeg(content,ctype)
    print('Poster image safely normalized to JPEG',flush=True)
   if not file.is_file():
    import tempfile
    tmp_name=None
    try:
     with tempfile.NamedTemporaryFile(mode='wb',dir=str(file.parent),
                                      prefix=file.name+'.',suffix='.part',
                                      delete=False) as tmp:
      tmp_name=tmp.name
      tmp.write(content)
      tmp.flush()
      os.fsync(tmp.fileno())
     try:
      os.link(tmp_name,str(file))  # fails rather than clobbers an existing poster
     except FileExistsError:
      pass
    finally:
     if tmp_name is not None:
      try: os.unlink(tmp_name)
      except FileNotFoundError: pass
  # An outage must not look like "no poster exists": report provider errors when nothing was found.
  status='ok' if poster_url else ('error' if provider_errors else 'none')
  with db() as c:
   c.execute('INSERT OR REPLACE INTO posters VALUES(?,?,?,?)',
             (path,status,int(time.time()),provider))
  poster_filters.log_result(path,status,provider,reason=None if poster_url else '; '.join(provider_errors) or None)
  if poster_url:
   record_poster_attempt(path,'found',f'{provider}: {match_reason}')
  elif provider_errors:
   record_poster_attempt(path,'provider_error','; '.join(provider_errors),near_choices)
  elif near_choices:
   record_poster_attempt(path,'needs_choice','Possible posters found, but none was certain enough to apply automatically',near_choices)
  else:
   record_poster_attempt(path,'no_match','No matching poster found')
 except Exception as exc:
  print('Poster error: '+type(exc).__name__,flush=True)
  with db() as c:
   c.execute('INSERT OR REPLACE INTO posters VALUES(?,?,?,?)',
             (path,'error',int(time.time()),''))
  poster_filters.log_result(path,'error',reason=type(exc).__name__)
  record_poster_attempt(path,'provider_error','Poster download or image check failed ('+type(exc).__name__+')')
 finally:
  with POSTER_LOCK:
   POSTER_BUSY.discard(path)

def queue_poster(path,library,name):
 """Start a poster lookup now if allowed. Returns '' when started, else why it did not start (V1.6.2)."""
 if poster_v2_paused():return 'Poster recovery is paused'
 if not poster_connect.has_provider(CONFIG):return 'No poster provider is configured'
 file=poster_file(path)
 if file.is_file():return 'Poster already exists'
 with db() as c:r=c.execute('SELECT checked FROM posters WHERE path=?',(path,)).fetchone()
 if r and int(time.time())-r['checked']<7*86400:return 'Checked recently'
 with POSTER_LOCK:
  if path in POSTER_BUSY:return 'Already searching'
  if len(POSTER_BUSY)>=4:return 'Other poster searches are running'
  POSTER_BUSY.add(path)
 threading.Thread(target=fetch_poster,args=(path,library,name),daemon=True).start()
 return ''

POSTER_RETRY_SCRIPT='''<script>/* themearr-retry-poster-v2 (V1.6.2): poster-only retries that follow the real job status */
(function () {
  "use strict";
  const POLL_MS = 3000, MAX_POLLS = 20;  // about one minute, then a clear "still searching" message
  const FINAL = {no_match: 1, needs_choice: 1, provider_error: 1};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  function showPoster(path) {
    const url = '/poster?' + new URLSearchParams({path}).toString() + '&fresh=' + Date.now();
    for (const link of document.querySelectorAll('a[href^="/themes?"]')) {
      let linked;
      try { linked = new URL(link.href, window.location.href).searchParams.get('path'); } catch (_) { continue; }
      if (linked !== path) continue;
      const placeholder = link.querySelector('.card-empty, .poster-empty');
      if (!placeholder) continue;
      const img = document.createElement('img');
      img.className = placeholder.classList.contains('card-empty') ? 'card-poster' : 'poster';
      img.alt = '';
      img.src = url;
      placeholder.replaceWith(img);
    }
  }

  function statusBox(form) {
    // V1.6.3: cards carry their own status line inside .poster-tools, in normal flow below the buttons.
    const tools = form.closest('.poster-tools');
    const own = tools && tools.querySelector('[data-poster-retry-status]');
    if (own) return own;
    const host = form.parentElement;
    let box = host.querySelector('[data-poster-retry-status]');
    if (!box) {
      box = document.createElement('p');
      box.setAttribute('data-poster-retry-status', '');
      box.setAttribute('role', 'status');
      box.setAttribute('aria-live', 'polite');
      box.style.cssText = 'display:block;clear:both;width:100%;margin:.6rem 0 0;font-size:.8rem;line-height:1.4;overflow-wrap:anywhere';
      host.append(box);
    }
    return box;
  }

  function offerChoice(form, path) {
    // Possible posters exist: give the card a real "Select Poster Manually" button (opens the in-page picker).
    const tools = form.closest('.poster-tools') || form.parentElement;
    if (!window.themearrPicker || tools.querySelector('[data-poster-pick]')) return;
    const card = form.closest('.anime-card');
    const title = card && card.querySelector('.card-title');
    form.after(window.themearrPicker.pickButton(path, title ? title.textContent.trim() : 'this anime'));
  }

  document.addEventListener('themearr:poster-chosen', function (event) {
    for (const pick of document.querySelectorAll('[data-poster-pick]')) {
      if (pick.getAttribute('data-poster-pick') !== event.detail.path) continue;
      const tools = pick.closest('.poster-tools');
      if (!tools) continue;
      const box = tools.querySelector('[data-poster-retry-status]');
      if (box) box.textContent = event.detail.message;
      const retry = tools.querySelector('form[action="/poster-retry"]');
      if (retry) retry.remove();  // the title has a poster now; the button stays so a wrong pick can be replaced
    }
  });

  function finish(form, button, box, message) {
    box.textContent = message;
    button.textContent = 'Retry Poster';
    button.disabled = false;
    form.dataset.posterRetryBusy = '0';
  }

  document.addEventListener('submit', async function (event) {
    const form = event.target;
    if (!form || form.tagName !== 'FORM' || form.getAttribute('action') !== '/poster-retry') return;
    event.preventDefault();
    if (form.dataset.posterRetryBusy === '1') return;
    const input = form.querySelector('input[name="path"]');
    const button = form.querySelector('button');
    if (!input || !input.value || !button) return;
    const path = input.value;
    const box = statusBox(form);
    form.dataset.posterRetryBusy = '1';
    button.disabled = true;
    button.textContent = 'Searching…';
    box.textContent = 'Sending poster retry request…';
    try {
      const response = await fetch('/poster-retry', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        body: new URLSearchParams(new FormData(form))
      });
      if (new URL(response.url, window.location.href).pathname === '/login') throw new Error('Session expired. Log in again.');
      if (response.status === 429) throw new Error('Poster retry limit reached (6 per hour). Try again later.');
      if (!response.ok) throw new Error('Retry could not be started (HTTP ' + response.status + ').');
      const statusUrl = '/poster-status?' + new URLSearchParams({path}).toString();
      for (let poll = 0; poll < MAX_POLLS; poll++) {
        if (!document.contains(form)) return;
        let state = null;
        try {
          const reply = await fetch(statusUrl, {cache: 'no-store', credentials: 'same-origin'});
          if (reply.ok) state = await reply.json();
        } catch (_) { /* network hiccup: keep polling until the limit */ }
        if (state) {
          box.textContent = state.message;
          if (state.state === 'found') {
            showPoster(path);
            box.textContent = 'Poster found and added.';
            button.textContent = 'Poster updated';
            setTimeout(() => { if (document.contains(form)) form.remove(); }, 2500);
            return;
          }
          if (FINAL[state.state]) {
            finish(form, button, box, state.message);
            if (state.state === 'needs_choice') offerChoice(form, path);
            return;
          }
        }
        await sleep(POLL_MS);
      }
      finish(form, button, box, 'Still searching or waiting for a free search slot. Check back in a minute; you can retry then.');
    } catch (error) {
      finish(form, button, box, error.message || 'Poster retry failed. Try again later.');
    }
  }, true);
})();
</script>'''

def poster_job_status(path):
 """Job state for one anime, for the Retry button (V1.6.2). (Not named poster_status: do_GET uses that local name.)"""
 if poster_file(path).is_file():
  return {'state':'found','message':'Poster found.'}
 with POSTER_LOCK:
  busy=path in POSTER_BUSY
 if busy:
  return {'state':'searching','message':'Searching poster providers…'}
 with db() as c:
  poster=c.execute('SELECT status,checked FROM posters WHERE path=?',(path,)).fetchone()
  attempt=c.execute('SELECT at,outcome,reason FROM poster_attempts WHERE path=?',(path,)).fetchone()
 if poster and poster['status']=='queued':
  reason=attempt['reason'] if attempt and attempt['outcome']=='queued' else ''
  return {'state':'queued','message':'Waiting for a free poster search slot'+(f' ({reason}).' if reason else '.')}
 if attempt:
  if attempt['outcome']=='no_match':
   return {'state':'no_match','message':'No matching poster found. Try another title or select artwork manually.'}
  if attempt['outcome']=='needs_choice':
   return {'state':'needs_choice','message':'Possible posters were found, but none was certain enough to apply automatically. Use Select Poster Manually to choose one.'}
  if attempt['outcome']=='provider_error':
   return {'state':'provider_error','message':'Poster provider temporarily unavailable ('+(attempt['reason'] or 'unknown error')+'). Try again later.'}
 if poster and poster['status'] in ('none','error'):
  return {'state':'no_match' if poster['status']=='none' else 'provider_error',
          'message':'No matching poster found. Try another title or select artwork manually.' if poster['status']=='none'
          else 'Poster provider temporarily unavailable. Try again later.'}
 return {'state':'idle','message':'No poster search has run yet.'}

POSTER_BACKGROUND_ENABLED=os.getenv(
 'POSTER_BACKGROUND_ENABLED','false'
).lower()=='true'

POSTER_BACKGROUND_DELAY=max(
 30,int(os.getenv('POSTER_BACKGROUND_DELAY','180'))
)

POSTER_BACKGROUND_LIMIT=max(
 1,min(20,int(os.getenv('POSTER_BACKGROUND_LIMIT','20')))
)

def background_poster_worker():
 poster_v2_init()
 print('Background poster worker started (V2)',flush=True)
 attempts=[]

 while True:
  try:
   if poster_v2_paused():
    time.sleep(5)
    continue
   if not poster_connect.has_provider(CONFIG):
    time.sleep(300)
    continue

   current=time.time()
   attempts=[t for t in attempts if current-t<3600]

   if len(attempts)>=POSTER_BACKGROUND_LIMIT:
    time.sleep(60)
    continue

   with POSTER_LOCK:
    busy=bool(POSTER_BUSY)

   if busy:
    time.sleep(10)
    continue

   with db() as conn:
    rows=conn.execute("""
     SELECT s.path,s.library,s.name,p.checked
     FROM shows s
     LEFT JOIN posters p ON p.path=s.path
     WHERE p.checked IS NULL OR p.checked < ?
     ORDER BY CASE WHEN p.status='queued' THEN 0 ELSE 1 END,COALESCE(p.checked,0),s.name
     LIMIT 5000
    """,(int(current)-7*86400,)).fetchall()

   selected=None

   for row in rows:
    if not poster_file(row['path']).is_file():
     selected=row
     break

   if selected is None:
    time.sleep(300)
    continue

   with POSTER_LOCK:
    if POSTER_BUSY:
     time.sleep(10)
     continue
    POSTER_BUSY.add(selected['path'])

   attempts.append(time.time())

   fetch_poster(
    selected['path'],
    selected['library'],
    selected['name']
   )

   time.sleep(POSTER_BACKGROUND_DELAY)

  except Exception as exc:
   print(
    'Background poster worker error: '+str(exc)[:180],
    flush=True
   )
   time.sleep(60)

def clean(name):return re.sub(r'\s*\(\d{4}\)\s*$','',re.sub(r'\s*-\s*imdb-tt\d+\s*$','',name,flags=re.I)).strip()
def year(name):
  m=re.search(r'\((\d{4})\)(?:\s*-\s*imdb-tt\d+)?\s*$',name,flags=re.I)
  return int(m.group(1)) if m else None
def norm(name):return re.sub(r'[^a-z0-9]','',name.casefold())
def anilist(name):
 q='query($s:String){Media(search:$s,type:ANIME){seasonYear title{english romaji native} synonyms}}'
 j=request_json('https://graphql.anilist.co',{'query':q,'variables':{'s':clean(name)}})
 return (j.get('data') or {}).get('Media')

def anime_theme(name, media):
 import difflib
 import unicodedata

 def key(value):
  value=unicodedata.normalize('NFKC',str(value or '')).casefold()
  return ''.join(ch for ch in value if ch.isalnum())

 def query_variants(value):
  value=clean(value)
  # Collapse dotted acronyms such as A.I.C.O. to AICO.
  collapsed=re.sub(r'(?<![A-Za-z])(?:[A-Za-z]\.){2,}',
                   lambda m:m.group().replace('.',''),value)
  plain=re.sub(r'[^\w]+',' ',collapsed,flags=re.UNICODE).strip()
  results=[]
  for item in (value,collapsed,plain):
   item=' '.join(item.split())
   if item and item.casefold() not in [x.casefold() for x in results]:
    results.append(item)
  return results

 media=media or {}
 raw_titles=[clean(name)]+[v for v in (media.get('title') or {}).values() if v]+(media.get('synonyms') or [])
 titles=[]
 for title in raw_titles:
  if isinstance(title,str) and key(title) and key(title) not in [key(v) for v in titles]:
   titles.append(title)
 titles=titles[:8]
 target_keys=[key(t) for t in titles]
 desired_year=year(name)
 candidates={}
 queried=set()
 request_count=0

 for title in titles:
  for query in query_variants(title):
   qkey=query.casefold()
   if qkey in queried:continue
   queried.add(qkey)
   request_count+=1
   if request_count>12:break
   url='https://api.animethemes.moe/anime?'+urllib.parse.urlencode({
     'q':query,'page[size]':'40','include':'animesynonyms'})
   try:
    shows=request_json(url).get('anime') or []
   except Exception:
    continue
   for show in shows:
    if not isinstance(show,dict):continue
    slug=show.get('slug')
    if not isinstance(slug,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',slug):continue
    show_year=show.get('year')
    if desired_year and show_year and str(show_year)!=str(desired_year):continue
    aliases=[show.get('name'),show.get('name_en'),show.get('name_jp')]
    for entry in show.get('animesynonyms') or []:
     if isinstance(entry,dict):aliases.append(entry.get('text') or entry.get('name'))
    aliases=[key(v) for v in aliases if isinstance(v,str) and key(v)]
    if not aliases:continue
    exact=any(t==a for t in target_keys for a in aliases)
    if exact:
     score=1.0
    else:
     # Very close spelling only; unrelated search results are never accepted.
     score=max((difflib.SequenceMatcher(None,t,a).ratio()
                for t in target_keys if len(t)>=8
                for a in aliases if len(a)>=8),default=0.0)
     if score<0.96:continue
    if desired_year and not show_year and not exact:continue
    if desired_year and not show_year:score-=0.02
    prev=candidates.get(slug)
    if prev is None or score>prev[0]:candidates[slug]=(score,show)
  if request_count>=12:break
  if any(score==1.0 and (not desired_year or str(show.get('year'))==str(desired_year))
         for score,show in candidates.values()):
   break

 ranked=sorted(candidates.values(),key=lambda pair:pair[0],reverse=True)
 if not ranked:return None
 best_score,best=ranked[0]
 if len(ranked)>1 and best_score-ranked[1][0]<0.05:
  # Multiple similarly credible titles: prefer manual selection.
  return None
 slug=best['slug']
 detail_url='https://api.animethemes.moe/anime/'+urllib.parse.quote(slug,safe='')+'?'+urllib.parse.urlencode({
  'include':'animethemes.animethemeentries.videos.audio,animethemes.song'})
 try:
  detail=request_json(detail_url).get('anime')
  return detail if isinstance(detail,dict) else None
 except Exception:
  return None


def _match_evidence(path,row,show,via):
 """V1.9.11: why the Theme Browser shows this AnimeThemes entry. Built only from data the lookup already returned; changes nothing."""
 esc=html.escape
 folder=clean(row['name']);want=year(row['name'])
 got=show.get('year');slug=str(show.get('slug') or '');name=str(show.get('name') or slug)
 exact=poster_key(folder)==poster_key(name)
 how={'confirmed':'You confirmed this AnimeThemes entry for this folder (Remember this anime / Match / Import).',
      'anilist':'Folder title search found nothing certain, so Preludarr searched with the English, Romaji and native titles from AniList.',
      'folder':'Searched AnimeThemes with your folder title.'}[via]
 title=('Exact title — your folder and the entry have the same name.' if exact else
        'Matched an alternative title or a spelling at least 96% identical (Original Matching never accepts loose matches).') if via!='confirmed' else (
        'Exact title.' if exact else 'Different title — accepted because you confirmed it.')
 if want and got:yr=('Same year: '+str(got)+'.') if str(got)==str(want) else ('Year differs: folder '+str(want)+', entry '+str(got)+(' — accepted because you confirmed it.' if via=='confirmed' else '.'))
 elif want:yr='The entry has no year; folder says '+str(want)+'.'
 else:yr='Your folder name has no year'+(', entry is '+str(got) if got else '')+'.'
 ids=[]
 try:
  with db() as c:
   r=c.execute('SELECT tvdb,tmdb,imdb,source FROM media_ids WHERE path=?',(path,)).fetchone()
  if r:
   ids=[(k.upper(),r[k]) for k in ('tvdb','tmdb','imdb') if r[k]]
   src=str(r['source'] or '').capitalize()
 except Exception:
  r=None
 try:
  with db() as c:
   st=c.execute('SELECT state,detail FROM shows WHERE path=?',(path,)).fetchone()
 except Exception:
  st=None
 rows=[('Provider','AnimeThemes.moe'),
       ('Matched entry',esc(name)+(' ('+esc(str(got))+')' if got else '')+' · <a href="https://animethemes.moe/anime/'+esc(urllib.parse.quote(slug,safe=''),quote=True)+'" target="_blank" rel="noopener noreferrer">'+esc(slug)+'</a>'+(' · id '+esc(str(show.get('id'))) if show.get('id') else '')),
       ('Your folder',esc(row['name'])),('How it was found',esc(how)),('Title check',esc(title)),('Year check',esc(yr))]
 if ids:rows.append(('Other IDs',' · '.join(esc(k)+' '+esc(str(v)) for k,v in ids)+(' <small>(from '+esc(src)+')</small>' if src else '')))
 if st and st['detail'] and st['state']!='installed':rows.append(('Last automatic attempt',esc(str(st['detail'])[:300])))
 short=('Confirmed by you' if via=='confirmed' else 'Exact title' if exact else 'Alternative title')+' · '+('same year' if want and got and str(got)==str(want) else 'year checked' if want else 'no folder year')
 return ('<details class="tp-ev"><summary>Why this match <span class="tp-ev-s">'+esc(name)+(' ('+esc(str(got))+')' if got else '')+' — '+esc(short)+'</span></summary>'
         '<dl>'+''.join('<dt>'+k+'</dt><dd>'+v+'</dd>' for k,v in rows)+'</dl>'
         '<p class="muted">Nothing is installed without your confirmation; the year and title checks above are the same ones automatic installs use.</p></details>')


def list_available_themes(show):
 themes=[]
 for theme in show.get('animethemes') or []:
  kind=str(theme.get('type') or '').upper()
  if not kind.startswith(('OP','ED')):continue
  song=theme.get('song') or {}
  for entry in theme.get('animethemeentries') or []:
   for video in entry.get('videos') or []:
    audio=video.get('audio') or {}
    url=audio.get('link')
    if not url:continue
    parsed=urllib.parse.urlparse(url)
    if parsed.scheme!='https' or parsed.hostname not in ('a.animethemes.moe','v.animethemes.moe'):continue
    item={
     'type':kind,
     'slug':str(theme.get('slug') or kind),
     'sequence':theme.get('sequence'),
     'title':song.get('title') or kind,
     'artist':', '.join(a.get('name','') for a in song.get('artists') or [] if isinstance(a,dict)),
     'audio_url':url,
     'video_url':video.get('link') or '',
     'entry_id':entry.get('id'),
     'video_id':video.get('id'),
    }
    if not any(str(t['video_id'])==str(item['video_id']) for t in themes):
     themes.append(item)
 return themes

def pick_audio(show):
 for theme in show.get('animethemes') or []:
  if not str(theme.get('type','')).upper().startswith('OP'):continue
  for entry in theme.get('animethemeentries') or []:
   for video in entry.get('videos') or []:
    audio=video.get('audio') or {}
    url=audio.get('link')
    if url and urllib.parse.urlparse(url).hostname in ('a.animethemes.moe','v.animethemes.moe'):
     return url,(theme.get('song') or {}).get('title') or 'Opening'
 return None,None



def resolve_animethemes_track(anime_slug, video_id):
 """Verify an exact OP/ED video against fresh AnimeThemes API metadata."""
 anime_slug=str(anime_slug).strip()
 video_id=str(video_id).strip()

 if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',anime_slug):
  raise ValueError('Invalid AnimeThemes anime slug')

 if not re.fullmatch(r'[0-9]{1,20}',video_id):
  raise ValueError('Invalid AnimeThemes video ID')

 params={
  'include':'animethemes.animethemeentries.videos.audio,animethemes.song'
 }

 api_url=(
  'https://api.animethemes.moe/anime/'
  +urllib.parse.quote(anime_slug,safe='')
  +'?'+urllib.parse.urlencode(params)
 )

 anime=request_json(api_url,fresh=True).get('anime') or {}  # verify before install: never a remembered answer

 if not isinstance(anime,dict) or anime.get('slug')!=anime_slug:
  raise ValueError('AnimeThemes anime entry could not be verified')

 tracks=list_available_themes(anime)

 selected=[
  track for track in tracks
  if str(track.get('video_id'))==video_id
 ]

 if len(selected)!=1:
  raise ValueError('Selected AnimeThemes OP/ED track could not be verified')

 track=selected[0]
 parsed=urllib.parse.urlsplit(track['audio_url'])

 if (
  parsed.scheme!='https'
  or parsed.hostname not in ('a.animethemes.moe','v.animethemes.moe')
  or parsed.username is not None
  or parsed.password is not None
  or parsed.port not in (None,443)
 ):
  raise ValueError('Untrusted AnimeThemes audio destination')

 return track



def download_verified_theme_audio(track, destination):
 """Download a previously verified AnimeThemes audio track safely."""
 import urllib.error
 import time

 url=str(track.get('audio_url') or '')
 parsed=urllib.parse.urlsplit(url)

 if (
  parsed.scheme!='https'
  or parsed.hostname not in ('a.animethemes.moe','v.animethemes.moe')
  or parsed.username is not None
  or parsed.password is not None
  or parsed.port not in (None,443)
  or parsed.fragment
 ):
  raise ValueError('Untrusted AnimeThemes audio URL')

 class NoRedirect(urllib.request.HTTPRedirectHandler):
  def redirect_request(self,req,fp,code,msg,headers,newurl):
   raise ValueError('Audio redirects are not permitted')

 opener=urllib.request.build_opener(NoRedirect)
 request=urllib.request.Request(
  url,
  headers={'User-Agent':'Preludarr/1.12 (self-hosted theme manager)','Accept':'audio/*'}
 )

 destination=Path(destination)

 if destination.exists() or destination.is_symlink():
  raise ValueError('Download destination already exists')

 maximum=40*1024*1024
 total=0
 deadline=time.monotonic()+60
 created=False
 created_identity=None

 try:
  with opener.open(request,timeout=30) as response:
   if response.status!=200:
    raise ValueError('Unexpected audio HTTP response')

   content_length=response.headers.get('Content-Length')

   if content_length:
    if int(content_length)>maximum:
     raise ValueError('Audio download exceeds 40 MB')

   flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
   flags|=getattr(os,'O_NOFOLLOW',0)
   fd=os.open(destination,flags,0o600)
   created=True
   created_identity=os.fstat(fd)
   with os.fdopen(fd,'wb') as output:
    while True:
     if time.monotonic()>deadline:
      raise TimeoutError('Audio download exceeded 60 seconds')
     chunk=response.read(1024*256)
     if time.monotonic()>deadline:
      raise TimeoutError('Audio download exceeded 60 seconds')

     if not chunk:
      break

     total+=len(chunk)

     if total>maximum:
      raise ValueError('Audio download exceeds 40 MB')

     output.write(chunk)

  if total<1024:
   raise ValueError('Downloaded audio is unexpectedly small')

  return destination

 except Exception:
  if created and created_identity is not None:
   try:
    current=os.lstat(destination)
    if (current.st_dev,current.st_ino)==(created_identity.st_dev,created_identity.st_ino):
     destination.unlink(missing_ok=True)
   except FileNotFoundError:
    pass
  raise



@contextlib.contextmanager
def stage_verified_theme_audio(track, staging_root):
 """Download verified audio inside a dedicated private directory."""
 staging_root=Path(staging_root)

 if not staging_root.is_dir() or staging_root.is_symlink():
  raise ValueError('Invalid staging root')

 with tempfile.TemporaryDirectory(
  prefix='themearr-download-',
  dir=staging_root
 ) as private_dir:
  private_path=Path(private_dir)
  audio_path=private_path/'source-audio'

  download_verified_theme_audio(track,audio_path)

  if not audio_path.is_file() or audio_path.is_symlink():
   raise ValueError('Staged audio is not a regular file')

  yield audio_path

_theme_confirmation_lock=threading.Lock()
_theme_confirmation_tokens={}

def issue_theme_confirmation(folder, anime_slug, video_id):
 """Issue a short-lived single-use token for a reviewed theme."""
 import secrets
 fingerprint=theme_confirmation_fingerprint(folder,anime_slug,video_id)
 token=secrets.token_urlsafe(32)
 now=time.monotonic()
 with _theme_confirmation_lock:
  expired=[key for key,value in _theme_confirmation_tokens.items() if value[0]<=now]
  for key in expired:
   del _theme_confirmation_tokens[key]
  _theme_confirmation_tokens[token]=(now+300,fingerprint)
 return token

def take_theme_confirmation(token, folder, anime_slug, video_id):
 """Consume a confirmation and return its original fingerprint."""
 import secrets
 if not isinstance(token,str) or len(token)>128:
  return None
 with _theme_confirmation_lock:
  record=_theme_confirmation_tokens.pop(token,None)
 if record is None or record[0]<=time.monotonic():
  return None
 try:
  actual=theme_confirmation_fingerprint(folder,anime_slug,video_id)
 except (OSError,ValueError):
  return None
 if not secrets.compare_digest(record[1],actual):
  return None
 return record[1]


def consume_theme_confirmation(token, folder, anime_slug, video_id):
 """Preserve the existing boolean confirmation interface."""
 return take_theme_confirmation(token,folder,anime_slug,video_id) is not None


def theme_confirmation_fingerprint(folder, anime_slug, video_id):
 """Bind a confirmation to the selected track and current theme state."""
 folder=Path(folder)
 target=folder/'theme.mp3'
 if target.is_symlink():
  raise ValueError('Unsafe theme destination')
 if target.exists():
  if not target.is_file():
   raise ValueError('Theme destination is not a regular file')
  stat=target.stat()
  state=f'{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}'
 else:
  state='missing'
 payload='\\0'.join((str(folder),str(anime_slug),str(video_id),state))
 return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _manual_install_mp3_unlocked(folder, source_audio, replace=False, expected_fingerprint=None, anime_slug=None, video_id=None, progress=None, backup=True):
 """Convert and atomically install a theme through a verified directory."""
 import secrets
 import stat
 folder=Path(folder)
 source_audio=Path(source_audio)
 if not source_audio.is_file() or source_audio.is_symlink():
  raise ValueError('Source audio must be a regular file')
 source_size=source_audio.stat().st_size
 if not 0<source_size<=40*1024*1024:
  raise ValueError('Source audio must be between 1 byte and 40 MB')
 with open_safe_theme_directory(folder) as folder_fd:
  def theme_state():
   try:
    item=os.stat('theme.mp3',dir_fd=folder_fd,follow_symlinks=False)
   except FileNotFoundError:
    return None
   if not stat.S_ISREG(item.st_mode):
    raise ValueError('Unsafe theme destination')
   return item
  existing=theme_state()
  if existing is not None and not replace:
   raise ValueError('Existing theme requires explicit replacement')
  staged_name='.themearr-manual-'+secrets.token_hex(16)+'.mp3'
  staged_fd=os.open(
   staged_name,
   os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,
   0o600,
   dir_fd=folder_fd
  )
  os.close(staged_fd)
  try:
   import sys
   ffmpeg_args=[
    'ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y',
    '-i',str(source_audio),'-vn','-t','600',
    '-codec:a','libmp3lame','-q:a','4','-threads','1',
    '-f','mp3','/proc/self/fd/'+str(folder_fd)+'/'+staged_name
   ]
   launcher=(
    'import os,resource,sys;'
    'resource.setrlimit(resource.RLIMIT_AS,(1073741824,1073741824));'
    'os.execvp(sys.argv[1],sys.argv[1:])'
   )
   subprocess.run(
    [sys.executable,'-c',launcher]+ffmpeg_args,
    check=True,timeout=120,pass_fds=(folder_fd,)
   )
   if progress: progress("validating", "Validating converted MP3")
   staged_stat=os.stat(staged_name,dir_fd=folder_fd,follow_symlinks=False)
   if not stat.S_ISREG(staged_stat.st_mode) or not 1024<=staged_stat.st_size<=40*1024*1024:
    raise ValueError('Converted MP3 failed size validation')
   verify_theme_directory_identity(folder,folder_fd)
   current=theme_state()
   if current is not None and not replace:
    raise ValueError('Theme appeared during conversion')
   if expected_fingerprint is not None:
    if not anime_slug or not video_id:
     raise ValueError('Missing confirmation identity')
    actual=theme_confirmation_fingerprint(folder,anime_slug,video_id)
    if actual!=expected_fingerprint:
     raise ValueError('Theme changed after confirmation')
   if (existing is None)!=(current is None):
    raise ValueError('Theme changed during conversion')
   if existing is not None and current is not None:
    old=(existing.st_dev,existing.st_ino,existing.st_size,existing.st_mtime_ns)
    now=(current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns)
    if old!=now:
     raise ValueError('Theme changed during conversion')
   verify_theme_directory_identity(folder,folder_fd)
   # Apply Unraid-compatible ownership before publishing the MP3.
   permission_fd=os.open(staged_name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=folder_fd)
   try:
    permission_stat=os.fstat(permission_fd)
    if not stat.S_ISREG(permission_stat.st_mode):
     raise ValueError('Unsafe staged MP3')
    if not 1024<=permission_stat.st_size<=40*1024*1024:
     raise ValueError('Invalid staged MP3 size')
    permissions.set_theme_owner(permission_fd)  # 1.12: PUID/PGID (non-root), else 99:100 as root
   finally:
    os.close(permission_fd)
   verify_theme_directory_identity(folder,folder_fd)
   saved=None
   if current is not None and backup:
    # 1.12 (review finding): a replaced theme.mp3 is kept in the recycle bin first (Undo puts it back); if that copy fails, nothing is replaced
    if progress: progress("backing_up", "Keeping the current theme.mp3 in the recycle bin")
    saved=theme_review.save_replaced(sys.modules[__name__],folder,folder_fd)
   if progress: progress("installing", "Installing verified theme.mp3")
   os.replace(
    staged_name,'theme.mp3',
    src_dir_fd=folder_fd,dst_dir_fd=folder_fd
   )
   if saved:
    theme_review.finish_replaced(saved,folder_fd)
   if progress: progress("published", "New theme.mp3 published successfully")
  finally:
   try:
    os.unlink(staged_name,dir_fd=folder_fd)
   except FileNotFoundError:
    pass
 return folder/'theme.mp3'

_manual_install_lock=threading.Lock()

def manual_install_mp3(folder, source_audio, replace=False,
                       expected_fingerprint=None, anime_slug=None, video_id=None, progress=None,
                       activity_label=None, backup=True):
 """Allow one manual installation at a time."""
 if not _manual_install_lock.acquire(blocking=False):
  raise RuntimeError('Another manual installation is running')
 try:
  try:
   library_health.preflight(folder)  # V1.9.3: plain reason before any conversion (read-only check)
   result=_manual_install_mp3_unlocked(
    folder,source_audio,replace=replace,
    expected_fingerprint=expected_fingerprint,
    anime_slug=anime_slug,video_id=video_id,
    progress=progress,backup=backup
   )
  except Exception as error:
   if isinstance(error,subprocess.CalledProcessError):reason='audio conversion failed (file is not valid audio); existing theme kept'
   elif isinstance(error,subprocess.TimeoutExpired):reason='audio conversion timed out; existing theme kept'
   else:reason=(str(error) or type(error).__name__)[:200]
   _record_install_activity(folder,anime_slug,video_id,'install failed',reason,activity_label)
   raise
  permissions.apply_file(CONFIG,Path(folder)/'theme.mp3')  # V1.8.0: only if the user turned permissions on
  _record_install_activity(folder,anime_slug,video_id,'installed','',activity_label)
  media_servers.after_install(CONFIG,str(folder))
  return result
 finally:
  _manual_install_lock.release()

def _record_install_activity(folder,anime_slug,video_id,status,extra,label=None):
 """Write manual installs (Theme Browser, custom upload, YouTube) to Recent activity; never raises."""
 try:
  with db() as c:
   row=c.execute('SELECT name FROM shows WHERE path=?',(str(folder),)).fetchone()
  name=row['name'] if row else Path(folder).name
  if anime_slug=='custom_upload':source='Custom upload'+(' · '+label if label else '')
  elif anime_slug=='youtube_manual':source='YouTube'+(' · '+label if label else '')
  elif str(anime_slug).startswith('source_') or (label and label.startswith('Bulk queue')):source=label or str(anime_slug)
  elif label:source='Theme Browser · '+label
  else:source=f'Theme Browser · AnimeThemes {anime_slug} (track {video_id})'
  event(name,status,source+(' — '+extra if extra else ''))
 except Exception:
  pass

def activity_time(value):
 """Show an activity timestamp in local time on one line; the stored value stays in the tooltip."""
 try:
  stamp=datetime.fromisoformat(str(value).replace('Z','+00:00'))
  if stamp.tzinfo is None:
   # Before V1.9.14 a few writers stored LOCAL time as 'YYYY-MM-DD HH:MM:SS' (space, no zone); everything else is UTC ISO.
   stamp=stamp.astimezone() if ' ' in str(value) else stamp.replace(tzinfo=timezone.utc)
  return stamp.astimezone().strftime('%b %d, %Y · %I:%M %p %Z').replace(' 0',' ')
 except (TypeError,ValueError):
  return str(value)



def discover(only=None):
 count=0
 for label,root in (list(ROOTS) if only is None else [(l,r) for l,r in ROOTS if r==Path(only)]):
  if not root.is_dir():event(label,'warning',f'Library path missing: {root}');continue
  for folder in root.iterdir():
   if not folder.is_dir() or folder.name.startswith(('.', '!')):continue
   if (label,root) not in ROOTS:break  # Preludarr 1.11.2: the root was removed / moved while this scan ran
   count+=1
   theme=folder/'theme.mp3'
   state='installed' if theme.is_file() and theme.stat().st_size>0 else 'missing'
   # Preserve failed-download explanations until a retry is requested.
   with db() as c: previous=c.execute('SELECT state,detail FROM shows WHERE path=?',(str(folder),)).fetchone()
   if state=='missing' and previous and previous['state']=='review':
    save(folder,label,folder.name,'review',previous['detail'])
   elif state=='missing' and previous and previous['state'] in ('searching','downloading','converting') and STATUS['running']:
    continue
   else:
    save(folder,label,folder.name,state,'Existing theme' if state=='installed' else '')
 # Preludarr 1.11.2: a root removed during the scan may have had a few titles saved after its removal — drop them again
 with db() as c:
  current={str(r) for _,r in ROOTS}
  for (p,) in c.execute('SELECT path FROM shows').fetchall():
   if str(Path(p).parent) not in current:c.execute('DELETE FROM shows WHERE path=?',(p,))
 auto_themes.record_new(sys.modules[__name__])  # V1.7.3: 'Recently added' dates for newly found folders
 return count

def process(path):
 path=Path(path)
 root_label=None
 for label,root in ROOTS:
  if path.parent==root and root.is_dir():root_label=label;break
 if not root_label or not path.is_dir():raise ValueError('Not a direct child of an allowed library')
 name=path.name; target=path/'theme.mp3'
 if target.is_file() and target.stat().st_size>0:
  save(path,root_label,name,'installed','Already exists');last_op(path,name,'installed','Already exists');return 'Already installed'
 if kinds.is_regular(root_label):  # Preludarr 1.11: Shows / Movies are never installed automatically — suggestions only
  found,notes=sources.cached_find(sys.modules[__name__],str(path),name,root_label,interactive=False)
  msg=(f'{len(found)} theme suggestion(s) ready — open the title to preview and choose' if found else 'No theme found yet: '+' '.join(notes)[:200])
  save(path,root_label,name,'missing',msg);last_op(path,name,'review',msg);return 'Suggestions: '+msg
 try:library_health.preflight(path)  # V1.9.3: don't search/download when the folder can't take a theme
 except ValueError as error:
  save(path,root_label,name,'review',str(error));last_op(path,name,'review',str(error));return 'Needs review: '+str(error)
 save(path,root_label,name,'searching','Matching title with AniList and AnimeThemes')
 STATUS.update(stage='searching',message='Matching: '+name)
 try:
  if media_servers.has_override(CONFIG,str(path)):
   # V1.7.3: the user confirmed this folder's anime (Match / Import or Remember this anime) — use exactly that entry.
   show=media_servers.resolve(CONFIG,str(path),name,{},anime_theme,request_json)
   if not show:raise ValueError('Your saved anime match could not be loaded from AnimeThemes right now')
  else:
   media=anilist(name)
   if not media:raise ValueError('AniList did not identify the series')
   yr=year(name); ay=media.get('seasonYear')
   if yr and ay and yr!=ay:raise ValueError(f'AniList year mismatch: folder {yr}, result {ay}')
   show=anime_theme(name,media)
   if not show:raise ValueError('No exact alternative-title match on AnimeThemes')
   sy=show.get('year')
   if yr and sy and int(sy)!=yr:raise ValueError(f'AnimeThemes year mismatch: folder {yr}, result {sy}')
  url,song=pick_audio(show)
  if not url:raise ValueError('No OP audio available')
  save(path,root_label,name,'downloading','Downloading: '+song)
  STATUS.update(stage='downloading',message='Downloading: '+name)
  with tempfile.TemporaryDirectory(prefix='themearr-') as temp:
   source=Path(temp)/'source.audio';converted=Path(temp)/'theme.mp3'
   req=urllib.request.Request(url,headers={'User-Agent':'Preludarr/1.12 (self-hosted theme manager)'})
   with open_approved(req,ANIMETHEMES_AUDIO_HOSTS,35) as resp,open(source,'wb') as out:
    total=0
    while True:
     chunk=resp.read(1024*256)
     if not chunk:break
     total+=len(chunk)
     if total>40*1024*1024:raise ValueError('Source audio exceeds 40 MB limit')
     out.write(chunk)
   subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-i',str(source),'-vn','-codec:a','libmp3lame','-q:a','4',str(converted)],check=True,timeout=120)
   if converted.stat().st_size<1024:raise ValueError('Converted MP3 is unexpectedly small')
   # Copy into the media directory only after a complete conversion.
   staged=path/'.themearr-theme.tmp'
   if staged.exists():raise ValueError('Temporary output already exists; investigate before retrying')
   try:
    shutil.copyfile(converted,staged)
    if target.exists():raise ValueError('Theme appeared during download; preserving it')
    os.link(staged,target) # atomic no-overwrite installation on same filesystem
    permissions.apply_file(CONFIG,target)  # V1.8.0: only if the user turned permissions on
   finally:
    staged.unlink(missing_ok=True)
  save(path,root_label,name,'installed',song);event(name,'downloaded',song);last_op(path,name,'installed',song);STATUS.update(stage='completed',message='Downloaded: '+name+' — '+song);return 'Downloaded: '+song
 except Exception as e:
  save(path,root_label,name,'review',str(e));event(name,'review',str(e));last_op(path,name,'review',str(e));STATUS.update(stage='failed',message='Failed: '+name+' — '+str(e));return 'Review: '+str(e)

PENDING_SCANS=[]  # Preludarr 1.11.2: root folders added while another job ran; scanned as soon as it finishes
def job(download=False,one=None,root=None):
 if not LOCK.acquire(blocking=False):
  if root is not None and not download and not one and Path(root) not in PENDING_SCANS:PENDING_SCANS.append(Path(root))
  return False
 def work():
  STATUS.update(running=True,message='Scanning' if not one else 'Starting download',active_path=str(one) if one else None,stage='scanning' if not one else 'queued')
  try:
   if one:STATUS['message']=process(one)
   else:
    if root is None:arr.sync_all(sys.modules[__name__])  # V1.8.0: read Sonarr/Radarr (dates added; optional folder creation)
    count=discover(root);STATUS['message']=f'Discovered {count} titles'+(f' in {root}' if root else '');STATUS['scan_summary']=STATUS['message']
    if download:
     for path in auto_themes.to_install(sys.modules[__name__]):process(path)  # V1.7.3: per the user's Automatic Themes choice
   STATUS['last_scan']=now()
  except Exception as e:STATUS['message']='Error: '+str(e);event('System','error',str(e))
  finally:
   STATUS['running']=False;LOCK.release()
   while PENDING_SCANS:
    nxt=PENDING_SCANS.pop(0)
    if any(r==nxt for _,r in ROOTS):job(False,root=nxt);break
 threading.Thread(target=work,daemon=True).start();return True

TITLE_PAGE_CSS='''<style>
/* V1.9.9 title page: secondary tools in labelled, collapsible sections below the available themes (scoped to .tp-*) */
.tp-loading::before{content:"";display:inline-block;width:12px;height:12px;margin-right:9px;vertical-align:-1px;border:2px solid #ffffff40;border-top-color:#cfe0f8;border-radius:50%;animation:tp-spin .8s linear infinite}
@keyframes tp-spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.tp-loading::before{animation:none}}
.tp-more{margin-top:20px}
.tp-ev{margin:10px 0 4px;border:1px solid #3a4864;border-radius:10px;padding:10px 14px;background:#16243b}
.tp-ev>summary{cursor:pointer;font-weight:700;color:#eef4ff}
.tp-ev .tp-ev-s{font-weight:400;color:#9fb0cc;margin-left:6px}
.tp-ev dl{display:grid;grid-template-columns:170px minmax(0,1fr);gap:6px 14px;margin:10px 0 6px}
.tp-ev dt{color:#9fb0cc}.tp-ev dd{margin:0;overflow-wrap:anywhere}
@media (max-width:600px){.tp-ev dl{grid-template-columns:1fr}.tp-ev dt{margin-top:6px}}
.tp-more .tp-more-h{margin:0 0 8px;font-size:14px;font-weight:700;color:#9fb0cc;letter-spacing:.02em}
.tp-more details.tp-sec{border:1px solid #334967;border-radius:12px;background:#16243b;margin:8px 0}
.tp-more details.tp-sec>summary{display:flex;align-items:baseline;gap:6px 14px;flex-wrap:wrap;padding:14px 18px;cursor:pointer;list-style:none}
.tp-more details.tp-sec>summary::-webkit-details-marker{display:none}
.tp-more details.tp-sec>summary::before{content:"\\25B8";display:inline-block;width:12px;color:#9fb0cc;transition:transform .15s}
.tp-more details.tp-sec[open]>summary::before{transform:rotate(90deg)}
.tp-more details.tp-sec>summary:hover{background:#1b2b47;border-radius:12px}
.tp-more details.tp-sec>summary:focus-visible{outline:3px solid #8fc1ff;outline-offset:2px;border-radius:12px}
.tp-more .tp-sum-t{font-weight:700;font-size:16px;color:#eef4ff}
.tp-more .tp-sum-h{color:#9fb0cc;font-size:13.5px}
.tp-more .tp-body{padding:2px 18px 18px}
.tp-more .tp-body>.panel,.tp-more .tp-body>section.panel{border:0!important;background:transparent!important;padding:0!important;margin:0 0 10px!important;box-shadow:none!important}
.tp-more .tp-body>.panel>h3:first-child,.tp-more .tp-body>section.panel>h3:first-child{display:none}
@media (max-width:600px){.tp-more details.tp-sec>summary{padding:14px}.tp-more .tp-body{padding:2px 14px 16px}}
</style>'''
DASHBOARD_CSS=('<style>/* V1.9.14 compact dashboard summary: counts, current status and last scan, then search comes right up */'
 '#dashboard{padding-top:18px!important;padding-bottom:16px!important}#dashboard h2{margin:0 0 10px}'
 '#dashboard .stats{gap:10px}#dashboard .stat{padding:8px 14px;min-width:0}'
 '#dashboard .stat .stat-sub{font-size:12px}'
 '#dashboard .notice{margin:10px 0 2px;padding:8px 12px;font-size:14px;line-height:1.45}'
 '#dashboard .scan-line{margin:2px 0 10px;font-size:13px}'
 '#dashboard form{display:inline-block;margin:0 14px 6px 0;vertical-align:middle}'
 '#dashboard .auto-line{display:inline;margin:0!important;font-size:14px!important;vertical-align:middle}'
 '@media(max-width:600px){#dashboard .stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}#dashboard .stat{padding:6px 8px}'
 '#dashboard .stat strong{font-size:17px}#dashboard .stat{font-size:12.5px;line-height:1.3}#dashboard .stat .stat-sub{font-size:11px}}</style>')
LIBRARY_SEARCH_CSS='''<style>
/* V1.9.8 Library Search panel (scoped to .ls-panel; no shared styles changed) */
.ls-panel{padding:24px!important;container-type:inline-size;container-name:ls}
.ls-panel h2{margin:0 0 16px}
.ls-panel .ls-toolbar{display:grid!important;grid-template-columns:minmax(0,3fr) repeat(3,minmax(0,1fr)) auto;gap:12px;align-items:end;margin:0}
.ls-panel .ls-toolbar.ls-notype{grid-template-columns:minmax(0,3fr) repeat(2,minmax(0,1fr)) auto}
.ls-panel .ls-field{display:flex;flex-direction:column;gap:6px;min-width:0}
.ls-panel .ls-label{font-size:12.5px;font-weight:600;color:#9fb0cc;letter-spacing:.02em}
.ls-panel .ls-toolbar input[type=search],.ls-panel .ls-toolbar select{box-sizing:border-box;width:100%;height:40px;flex:0 0 auto;min-width:0;margin:0;padding:0 12px;font:inherit;font-size:14.5px;
 color:#eef4ff;background:#0d1626;border:1px solid #3e5174;border-radius:8px}
.ls-panel .ls-toolbar input[type=search]::placeholder{color:#8496b3}
.ls-panel .ls-buttons{display:flex;gap:8px;align-items:center}
.ls-panel .ls-btn{box-sizing:border-box;display:inline-flex;align-items:center;justify-content:center;height:40px;padding:0 16px;margin:0;border-radius:8px;
 font:inherit;font-size:14.5px;font-weight:600;line-height:1;cursor:pointer;text-decoration:none;white-space:nowrap}
.ls-panel .ls-primary{background:#4f8ff7;color:#fff;border:1px solid #4f8ff7}.ls-panel .ls-primary:hover{background:#3a77df}
.ls-panel .ls-quiet{background:transparent;color:#c3d3ec;border:1px solid #3e5174}.ls-panel .ls-quiet:hover{background:#1c2a44;color:#fff}
.ls-panel .ls-summary{margin:12px 0 0;color:#9fb0cc;font-size:14px}
.ls-panel .ls-bulkbox{margin-top:14px;border-top:1px solid #2b3a55;padding-top:10px}
.ls-panel .ls-bulkbox>summary{cursor:pointer;display:flex;flex-wrap:wrap;gap:4px 12px;align-items:baseline;padding:6px 0;list-style:none}
.ls-panel .ls-bulkbox>summary::-webkit-details-marker{display:none}
.ls-panel .ls-bulkbox>summary::before{content:"\\25B8";color:#9fb0cc;display:inline-block;transition:transform .15s}
.ls-panel .ls-bulkbox[open]>summary::before{transform:rotate(90deg)}
.ls-panel .ls-bulkbox>summary:focus-visible{outline:3px solid #8fc1ff;outline-offset:2px;border-radius:6px}
.ls-panel .ls-bulk-t{font-weight:700;color:#e3ecfa}
.ls-panel .ls-bulkbox .ls-bulk{border-top:0;margin-top:4px;padding-top:6px}
.ls-panel .ls-scope-note{margin:0;color:#9fb0cc;font-size:13.5px}
.ls-panel .ls-scope{margin:0;color:#cfe0f8;font-size:13.5px}
.ls-panel .ls-bulk{margin-top:16px;padding-top:16px;border-top:1px solid #2b3a55;display:flex;flex-direction:column;gap:14px}
.ls-panel .ls-row{display:grid!important;grid-template-columns:150px minmax(0,auto) 1fr;column-gap:12px;row-gap:6px;align-items:center;margin:0!important}
.ls-panel .ls-cat{display:inline-flex;align-items:center;gap:8px;font-weight:700;color:#e3ecfa}
.ls-panel .ls-count{min-width:26px;padding:2px 8px;border-radius:999px;background:#22324f;border:1px solid #3e5174;color:#cfe0f8;font-size:12.5px;font-weight:700;text-align:center}
.ls-panel .ls-act{display:inline-flex;align-items:center;gap:10px;flex-wrap:wrap}
.ls-panel .ls-outline{background:#16233a;color:#dbe7f9;border:1px solid #4f6a96}.ls-panel .ls-outline:hover{background:#203252;border-color:#6f8fc7}
.ls-panel .ls-outline[disabled]{opacity:.5;cursor:not-allowed}
.ls-panel .ls-check{display:inline-flex!important;align-items:center;gap:6px;margin:0!important;height:40px;color:#cfe0f8;font-size:14.5px}
.ls-panel .ls-check input{width:18px;height:18px;margin:0;accent-color:#4f8ff7}
.ls-panel .ls-help{grid-column:2/-1;max-width:68ch;color:#9fb0cc;font-size:13.5px;line-height:1.5}
.ls-panel .ls-link,.ls-panel #themearr-queue-reopen{background:transparent!important;color:#9fd0ff!important;border:0!important;padding:0 2px!important;margin:0!important;height:40px;
 display:inline-flex;align-items:center;font:inherit;font-size:14px;font-weight:600;text-decoration:underline;text-underline-offset:3px;cursor:pointer;white-space:nowrap}
.ls-panel .ls-link:hover,.ls-panel #themearr-queue-reopen:hover{color:#c6e3ff!important}
/* V1.9.9: each bulk row is one grid: category | action | its queue link, explanation under the action.
   The forms use display:contents so their parts sit in the row grid; the forms and their submission are unchanged. */
.ls-panel .ls-rowwrap,.ls-panel #themearr-poster-actionbar{display:grid!important;grid-template-columns:150px max-content minmax(0,1fr);column-gap:16px;row-gap:6px;align-items:center;width:100%}
.ls-panel .ls-rowwrap>form.ls-row,.ls-panel #themearr-poster-actionbar>form.ls-row{display:contents!important}
.ls-panel .ls-rowwrap .ls-cat,.ls-panel #themearr-poster-actionbar .ls-cat{grid-column:1;grid-row:1}
.ls-panel .ls-rowwrap .ls-act,.ls-panel #themearr-poster-actionbar .ls-act{grid-column:2;grid-row:1}
.ls-panel .ls-rowwrap .ls-help,.ls-panel #themearr-poster-actionbar .ls-help{grid-column:2/-1;grid-row:2}
.ls-panel .ls-rowwrap .ls-link,.ls-panel #themearr-poster-actionbar #themearr-queue-reopen{grid-column:3;grid-row:1;justify-self:start}
.ls-panel a:focus-visible,.ls-panel button:focus-visible,.ls-panel input:focus-visible,.ls-panel select:focus-visible{outline:3px solid #8fc1ff;outline-offset:2px}
@container ls (max-width:1000px){.ls-panel .ls-toolbar{grid-template-columns:repeat(3,minmax(0,1fr)) auto}.ls-panel .ls-toolbar.ls-notype{grid-template-columns:repeat(2,minmax(0,1fr)) auto}.ls-panel .ls-title{grid-column:1/-1}}
@container ls (max-width:640px){
 .ls-panel .ls-toolbar,.ls-panel .ls-toolbar.ls-notype{grid-template-columns:1fr}.ls-panel .ls-buttons .ls-btn{flex:1 1 0;height:44px}
 .ls-panel .ls-toolbar input[type=search],.ls-panel .ls-toolbar select{height:44px}
 .ls-panel .ls-row{grid-template-columns:1fr}.ls-panel .ls-help{grid-column:1}.ls-panel .ls-act{flex-wrap:wrap}.ls-panel .ls-act .ls-btn{flex:1 1 100%;width:100%;height:44px;white-space:normal;text-align:center}
 .ls-panel .ls-rowwrap,.ls-panel #themearr-poster-actionbar{grid-template-columns:1fr}.ls-panel .ls-link,.ls-panel #themearr-queue-reopen{height:44px}
 .ls-panel .ls-rowwrap .ls-cat,.ls-panel .ls-rowwrap .ls-act,.ls-panel .ls-rowwrap .ls-help,.ls-panel .ls-rowwrap .ls-link,.ls-panel #themearr-poster-actionbar .ls-cat,.ls-panel #themearr-poster-actionbar .ls-act,.ls-panel #themearr-poster-actionbar .ls-help,.ls-panel #themearr-poster-actionbar #themearr-queue-reopen{grid-column:1;grid-row:auto}}
</style>'''
CSS='''
/* V1.9.9: compact dark audio players on every page (native controls; color-scheme makes Chrome/Edge draw them dark, no player library) */
audio{color-scheme:dark;accent-color:#5b8cff;height:40px;max-width:100%;border-radius:20px;background:#0d1626}
audio::-webkit-media-controls-panel{background:#16233a}
/* V1.9.3: readable default for plain links on dark panels (was browser blue / visited purple). :where() = zero specificity, so every existing link style still wins. */
:where(main) :where(a:link,a:visited){color:#9fd0ff}

/* Themearr v0.7 readable anime titles */
.card-title a,
.card-title a:link,
.card-title a:visited,
.anime-info a,
.anime-info a:link,
.anime-info a:visited {
 color:#f1f5ff;
 text-decoration:none;
 font-weight:650;
}

.card-title a:hover,
.card-title a:focus-visible,
.anime-info a:hover,
.anime-info a:focus-visible {
 color:#93c5fd;
 text-decoration:underline;
}

.library-list td.missing,
.library-list td.installed,
.library-list td.review {
 white-space:nowrap;
 word-break:normal;
 overflow-wrap:normal;
}

.library-list th:nth-child(3),
.library-list td:nth-child(3) {
 min-width:85px;
}

.anime-info {
 overflow-wrap:break-word;
}

/* Themearr v0.7 emoji sidebar */
.themearr-sidebar{
 position:fixed;
 top:0;
 left:0;
 bottom:0;
 width:215px;
 box-sizing:border-box;
 background:#111827;
 border-right:1px solid #334155;
 padding:20px 12px;
 display:flex;
 flex-direction:column;
 z-index:20;
 overflow-y:auto;
}
.sidebar-brand{
 font-size:21px;
 font-weight:750;
 padding:12px 10px 22px;
 color:#fff;
}
.themearr-sidebar nav{
 display:flex;
 flex-direction:column;
 gap:5px;
}
.themearr-sidebar nav a,
.themearr-sidebar .nav-soon{
 display:block;
 padding:12px 11px;
 border-radius:8px;
 text-decoration:none;
 color:#e5e7eb;
 font-size:14px;
}
.themearr-sidebar nav a:hover,
.themearr-sidebar nav a:focus{
 background:#263c60;
 color:#93c5fd;
}
.themearr-sidebar .nav-soon{
 color:#8d9ab1;
 cursor:default;
}
.nav-soon small{
 font-size:10px;
 margin-left:4px;
}
.sidebar-footer{
 margin-top:auto;
 padding:18px 10px 4px;
 color:#94a3b8;
 font-size:12px;
}
.themearr-content{
 margin-left:215px;
 min-width:0;
}
.themearr-content main{
 max-width:1600px;
}
#dashboard,
#library-results,
#recent-activity{
 scroll-margin-top:18px;
}
@media(max-width:800px){
 .themearr-sidebar{
  position:relative;
  width:100%;
  height:auto;
  border-right:0;
  border-bottom:1px solid #334155;
  padding:10px;
 }
 .sidebar-brand{
  padding:8px;
 }
 .themearr-sidebar nav{
  flex-direction:row;
  flex-wrap:wrap;
 }
 .themearr-sidebar nav a,
 .themearr-sidebar .nav-soon{
  padding:9px;
  font-size:12px;
 }
 .sidebar-footer{
  display:none;
 }
 .themearr-content{
  margin-left:0;
 }
}
body{font:15px system-ui;background:#0e1422;color:#e7ecf6;margin:0}header{background:#172238;padding:22px 5%;border-bottom:1px solid #324158}h1{margin:0}main{max-width:1200px;margin:25px auto;padding:0 18px}.panel{background:#19243a;border:1px solid #324158;border-radius:12px;padding:18px;margin-bottom:18px}button{background:#477df2;border:0;border-radius:6px;color:white;padding:10px 14px;cursor:pointer}button:disabled{opacity:.5}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid #324158;word-break:break-word}small{color:#9baac5}.installed{color:#5ed0a1}.review{color:#ffbb70}.missing{color:#a5b3c9}input,select{padding:10px;background:#0e1422;color:white;border:1px solid #4b5a75;border-radius:6px}.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.filters input{flex:1;min-width:180px}.pager{display:flex;gap:14px;align-items:center;margin:16px 0}.pager a{color:#9fc1ff}.muted{color:#9baac5}.stats{display:flex;gap:14px;flex-wrap:wrap}.stat{background:#0e1422;border:1px solid #324158;padding:12px 18px;border-radius:8px}.stat strong{font-size:20px;display:block;line-height:1.2}.stat .stat-sub{display:block;margin-top:2px;font-size:12.5px;color:#9fb0cc}.notice{border:1px solid #477df2;background:#102744;padding:12px;border-radius:8px;margin:12px 0}.filters a{color:#9fc1ff}.anime-cell{display:flex;align-items:center;gap:12px}.poster{width:52px;height:76px;object-fit:cover;border-radius:5px;background:#0e1422;flex-shrink:0}.poster-empty{display:flex;align-items:center;justify-content:center;color:#6f84a4;font-size:11px;text-align:center;border:1px solid #324158}.anime-info{min-width:0}
.view-toolbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:18px}
.view-toggle{display:flex;gap:6px}
.view-toggle button{background:#263650;border:1px solid #405574}
.view-toggle button.active{background:#477df2;border-color:#477df2}

/* Themearr adjustable poster sizes */
.poster-grid{
 display:grid;
 grid-template-columns:repeat(auto-fill,minmax(0,min(100%,var(--poster-width,220px))));
 gap:18px;
}
.poster-size-control{
 display:flex;
 align-items:center;
 gap:8px;
 color:#dbe7ff;
 font-size:13px;
}
.poster-size-control select{
 padding:8px 10px;
 min-width:125px;
}
body.poster-small{--poster-width:130px}
body.poster-medium{--poster-width:165px}
body.poster-large{--poster-width:220px}
body.poster-extra{--poster-width:280px}

.anime-card{background:#111b2d;border:1px solid #324158;border-radius:9px;overflow:hidden;min-width:0}
.card-poster,.card-empty{width:100%;aspect-ratio:2/3;display:block;object-fit:cover;background:#263143}
.card-empty{display:flex;align-items:center;justify-content:center;color:#9baac5;box-sizing:border-box;padding:12px;text-align:center}
.card-info{padding:12px}
.card-title{font-weight:650;line-height:1.4;overflow-wrap:anywhere}
.card-meta{color:#9baac5;font-size:12px;margin:8px 0}
.card-status{font-size:12px;margin-bottom:10px}
.card-info button{width:100%}
.library-list{display:none}
body.view-list .poster-grid{display:none}
body.view-list .library-list{display:block}
body.view-grid .poster-grid{display:grid}
body.view-grid .library-list{display:none}
@media(max-width:600px){.poster-grid{gap:12px}}
'''
from login import Auth
auth=Auth()


# --- BEGIN THEMEARR POSTER RECOVERY V1 ---
# Poster-only controls. No audio/theme install endpoints are affected.
POSTER_V1_LOCK = threading.Lock()
POSTER_V1_MANUAL_ATTEMPTS = []
POSTER_V1_BULK_LAST = 0.0


# ===== THEMEARR POSTER RECOVERY V2 =====
# Uses existing posters table as the durable eligibility/retry queue.
# The only additional table stores a persistent pause/resume setting.

def poster_v2_init():
    with db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS poster_v2_control ('
                     'key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute("INSERT OR IGNORE INTO poster_v2_control(key,value) VALUES('paused','0')")


def poster_v2_paused():
    with db() as conn:
        row = conn.execute("SELECT value FROM poster_v2_control WHERE key='paused'").fetchone()
    return bool(row and row['value'] == '1')


def poster_v2_snapshot():
    poster_v2_init()
    now = int(time.time())
    with db() as conn:
        rows = conn.execute('''
            SELECT s.path,p.status,p.checked
            FROM shows s LEFT JOIN posters p ON p.path=s.path
        ''').fetchall()
    stats = {'total': len(rows), 'cached': 0, 'missing': 0,
             'never_attempted': 0, 'failed': 0, 'queued': 0,
             'cooling_down': 0, 'ready': 0}
    for row in rows:
        if poster_file(row['path']).is_file():
            stats['cached'] += 1
            continue
        stats['missing'] += 1
        status = row['status']
        checked = row['checked']
        if checked is None:
            stats['never_attempted'] += 1
        elif status in ('none', 'error'):
            stats['failed'] += 1
        elif status == 'queued':
            stats['queued'] += 1
        if checked is None or int(checked) <= now - 7 * 86400:
            stats['ready'] += 1
        else:
            stats['cooling_down'] += 1
    with POSTER_LOCK:
        stats['in_progress'] = len(POSTER_BUSY)
    stats['paused'] = poster_v2_paused()
    stats['delay_seconds'] = POSTER_BACKGROUND_DELAY
    stats['hourly_limit'] = POSTER_BACKGROUND_LIMIT
    stats['percent'] = round(100.0 * stats['cached'] / stats['total'], 1) if stats['total'] else 0.0
    return stats


def poster_v2_control_post(handler, command):
    form = poster_v1_form(handler)
    if form is None or form:
        return handler.send(400, 'Invalid recovery control request', 'text/plain')
    poster_v2_init()
    if command in ('pause', 'resume'):
        with db() as conn:
            conn.execute("UPDATE poster_v2_control SET value=? WHERE key='paused'",
                         ('1' if command == 'pause' else '0',))
        return auth.redirect(handler, '/?view=activity#poster-recovery')
    if command != 'retry-failed':
        return handler.send(400, 'Invalid recovery operation', 'text/plain')
    with db() as conn:
        rows = conn.execute('''
            SELECT p.path FROM posters p JOIN shows s ON s.path=p.path
            WHERE p.status IN ('none','error')
        ''').fetchall()
        retry_paths = [row['path'] for row in rows if not poster_file(row['path']).is_file()]
        with POSTER_LOCK:
            busy = set(POSTER_BUSY)
        retry_paths = [path for path in retry_paths if path not in busy]
        conn.executemany("UPDATE posters SET status='queued',checked=0 WHERE path=?",
                         [(path,) for path in retry_paths])
    return auth.redirect(handler, '/?view=activity#poster-recovery')


def poster_v2_panel():
    s = poster_v2_snapshot()
    esc = html.escape
    labels = [('cached', 'Cached posters'), ('missing', 'Missing'),
              ('never_attempted', 'Not attempted'), ('failed', 'Failed match'),
              ('queued', 'Queued retries'), ('in_progress', 'In progress')]
    output = ['<section class="panel" id="poster-recovery">',
              '<h2>Poster Recovery</h2>',
              '<p id="poster-v2-state" role="status">',
              ('Paused' if s['paused'] else 'Running'),
              ' · ', str(s['delay_seconds']), ' seconds between attempts · ',
              str(s['hourly_limit']), ' attempts/hour maximum</p>',
              '<div class="stats">']
    for key, label in labels:
        output += ['<div class="stat"><strong data-poster-v2="', key, '">',
                   str(s[key]), '</strong>', label, '</div>']
    output += ['</div><p><strong data-poster-v2="percent">', str(s['percent']),
               '</strong>% cached (', '<span data-poster-v2="cached_text">',
               str(s['cached']), '</span> / ', str(s['total']), ')</p>',
               '<progress id="poster-v2-progress" max="',
               str(max(1, s['total'])), '" value="', str(s['cached']),
               '" style="width:100%"></progress>',
               '<div style="display:flex;gap:.75rem;flex-wrap:wrap;margin-top:1rem">']
    for command, title in [('pause', 'Pause recovery'),
                           ('resume', 'Resume recovery'),
                           ('retry-failed', 'Retry failed matches only')]:
        if command == 'pause' and s['paused']:
            continue
        if command == 'resume' and not s['paused']:
            continue
        output += ['<form method="POST" action="/poster-v2-', command,
                   '"><button type="submit"',
                   (' disabled' if command == 'retry-failed' and not s['failed'] else ''),
                   '>', esc(title), '</button></form>']
    output += ['</div><p class="muted">Pause takes effect before the next lookup; '
               'a request already in progress may finish. Retry failed only marks '
               'failed records for normal rate-limited recovery. '
               'Valid cached posters and theme.mp3 files are never replaced.</p>',
               '<script>(function(){async function refresh(){try{',
               'if(document.hidden)return;const r=await fetch("/poster-v2-status",{cache:"no-store"});',
               'if((r.status===401||(r.redirected&&new URL(r.url).pathname===\"/login\"))){clearInterval(v2Timer);return}',
               'if(!r.ok)return;const s=await r.json();',
               'for(const key of ["cached","missing","never_attempted",',
               '"failed","queued","in_progress","percent"]){',
               'const node=document.querySelector("[data-poster-v2=\\\""+key+"\\\"]");',
               'if(node)node.textContent=String(s[key]);}',
               'const t=document.querySelector("[data-poster-v2=\\\"cached_text\\\"]");',
               'if(t)t.textContent=String(s.cached);',
               'const p=document.getElementById("poster-v2-progress");',
               'if(p){p.max=Math.max(1,s.total);p.value=s.cached;}',
               'const state=document.getElementById("poster-v2-state");',
               'if(state)state.textContent=(s.paused?"Paused":"Running")+',
               '" · "+s.delay_seconds+" seconds between attempts · "+',
               's.hourly_limit+" attempts/hour maximum";',
               '}catch(e){}}const v2Timer=setInterval(refresh,5000);})();</script>',
               '</section>']
    return ''.join(output)
# ===== END THEMEARR POSTER RECOVERY V2 =====

def poster_v1_snapshot():
    """Live counts from the poster cache and existing poster attempt table."""
    now = int(time.time())
    with db() as conn:
        items = conn.execute("""
            SELECT s.path,p.status,p.checked
            FROM shows s LEFT JOIN posters p ON p.path=s.path
        """).fetchall()
    stats = dict(total=len(items), recovered=0, missing=0,
                 ready=0, cooling_down=0, failed=0)
    for item in items:
        if poster_file(item['path']).is_file():
            stats['recovered'] += 1
            continue
        stats['missing'] += 1
        if item['status'] in ('error', 'none'):
            stats['failed'] += 1
        checked = item['checked']
        if checked is None or int(checked) <= now-7*86400:
            stats['ready'] += 1
        else:
            stats['cooling_down'] += 1
    return stats


def poster_v1_panel():
    s = poster_v1_snapshot()
    percentage = round(100*s['recovered']/s['total'], 1) if s['total'] else 0
    content = [
        '<div class="panel" id="poster-recovery">',
        '<h2>Poster Recovery</h2>',
        '<p class="muted">Artwork recovery uses the existing rate-limited background worker.</p>',
        '<div class="stats">',
    ]
    for value, label in (
        (s['recovered'], 'Recovered'),
        (s['missing'], 'Missing'),
        (s['ready'], 'Ready / queued'),
        (s['cooling_down'], 'Waiting for retry'),
        (s['failed'], 'Failed matching'),
    ):
        content.extend(['<div class="stat"><strong>', str(value),
                        '</strong>', label, '</div>'])
    content.extend([
        '</div><p><strong>', str(percentage),
        '%</strong> posters cached (', str(s['recovered']), '/',
        str(s['total']), ')</p>',
        '<progress max="', str(max(s['total'], 1)), '" value="',
        str(s['recovered']), '" style="width:100%"></progress>',
        '<form method="POST" action="/poster-retry-missing">',
        '<button type="submit"', (' disabled' if not s['missing'] else ''),
        '>Retry Missing Posters</button></form>',
        '<p class="muted">This re-queues missing posters only; it does not download all at once. '
        'Cached artwork is left untouched. Automatic theme downloading is unchanged.</p>',
        '</div>'
    ])
    return ''.join(content)


def poster_v1_form(handler, maximum=4096):
    raw_size = handler.headers.get('Content-Length', '0')
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        return None
    if size < 0 or size > maximum:
        return None
    try:
        body = handler.rfile.read(size).decode('utf-8', 'strict')
        return urllib.parse.parse_qs(body, keep_blank_values=True,
                                     max_num_fields=3)
    except (UnicodeError, ValueError):
        return None


def poster_v1_retry_single(handler):
    form = poster_v1_form(handler)
    if not form or set(form) != {'path'} or len(form['path']) != 1:
        return handler.send(400, 'Invalid poster request', 'text/plain')
    selected = form['path'][0]
    if not selected or len(selected) > 4096:
        return handler.send(400, 'Invalid anime path', 'text/plain')
    folder = Path(selected)
    if (folder.is_symlink() or not folder.is_dir() or
            not any(folder.parent == root and root.is_dir() and
                    not root.is_symlink() for _, root in ROOTS)):
        return handler.send(400, 'Invalid anime destination', 'text/plain')
    with db() as conn:
        row = conn.execute('SELECT library,name FROM shows WHERE path=?',
                           (selected,)).fetchone()
    if row is None:
        return handler.send(404, 'Unknown anime', 'text/plain')
    if poster_file(selected).is_file():
        return auth.redirect(handler, '/?view=activity#poster-recovery')
    now = time.time()
    with POSTER_V1_LOCK:
        POSTER_V1_MANUAL_ATTEMPTS[:] = [t for t in POSTER_V1_MANUAL_ATTEMPTS
                                      if now-t < 3600]
        if len(POSTER_V1_MANUAL_ATTEMPTS) >= 6:
            return handler.send(429, 'Poster retry limit: 6 per hour', 'text/plain')
        POSTER_V1_MANUAL_ATTEMPTS.append(now)
    # A negative timestamp prioritizes this anime over ordinary background work.
    # queue_poster() starts a bounded daemon lookup if capacity is available.
    with db() as conn:
        conn.execute('INSERT OR REPLACE INTO posters (path,status,checked,provider) '
                     'VALUES (?,?,?,?)', (selected, 'queued', -1, ''))
    not_started = queue_poster(selected, row['library'], row['name'])
    if not_started:
        # The background worker picks queued titles first; say why it is waiting instead of failing silently.
        record_poster_attempt(selected, 'queued', not_started)
    return auth.redirect(handler, '/?view=activity#poster-recovery')


def poster_filtered_submit(paths):
    global POSTER_V1_BULK_LAST
    now=time.time()
    with POSTER_V1_LOCK:
        if now-POSTER_V1_BULK_LAST<900:return ('rate_limit',0,())
        if poster_v2_paused():return ('recovery_paused',0,())
        available=[p for p in paths if not poster_file(p).is_file()]
        if not available:return ('ok',0,())
        with db() as conn:
            conn.executemany('INSERT OR REPLACE INTO posters (path,status,checked,provider) VALUES (?,?,?,?)',
                [(p,'queued',0,'') for p in available])
        POSTER_V1_BULK_LAST=now
        return ('ok',len(available),tuple(available))

def poster_v1_retry_missing(handler):
    global POSTER_V1_BULK_LAST
    form = poster_v1_form(handler)
    if form is None or form:
        return handler.send(400, 'Invalid bulk poster request', 'text/plain')
    now = time.time()
    with POSTER_V1_LOCK:
        if now - POSTER_V1_BULK_LAST < 900:
            return handler.send(429, 'Bulk retry available every 15 minutes',
                                'text/plain')
        POSTER_V1_BULK_LAST = now
    with db() as conn:
        rows = conn.execute('SELECT path FROM shows').fetchall()
        missing_paths = [r['path'] for r in rows
                         if not poster_file(r['path']).is_file()]
        # Only missing artwork is reset, never successfully cached images.
        conn.executemany(
            'INSERT OR REPLACE INTO posters (path,status,checked,provider) '
            'VALUES (?,?,?,?)',
            [(p, 'queued', 0, '') for p in missing_paths]
        )
    return auth.redirect(handler, '/?view=activity#poster-recovery')
# --- END THEMEARR POSTER RECOVERY V1 ---


# --- THEMEARR MANUAL POSTERS V1.6.3 ---
def poster_manual_post(handler):
 """Choose / search / upload / reset a poster for one title (poster_manual)."""
 app=sys.modules[__name__]
 route=handler.path
 back=lambda path,msg,**extra:auth.redirect(handler,'/themes?'+urllib.parse.urlencode({'path':path,'poster_msg':msg,**extra})+'#themearr-poster-panel')
 if route=='/poster-upload':
  try:
   token,path=poster_manual.stage_upload(app,handler)
  except (ValueError,UnicodeError,OSError) as error:
   message='Poster upload rejected: '+(str(error) if type(error) is ValueError else 'the upload could not be read.')
   path=getattr(handler,'poster_upload_path','')
   return back(path,message) if path else handler.send(400,message,'text/plain')
  return auth.redirect(handler,'/poster-upload-review?'+urllib.parse.urlencode({'token':token}))
 try:
  size=int(handler.headers.get('Content-Length','0'))
  if not 0<=size<=8192:raise ValueError
  form=urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8','strict'),keep_blank_values=True,max_num_fields=8)
 except (TypeError,ValueError,UnicodeError):
  form=None
 if form is None or any(len(v)!=1 for v in form.values()):
  return handler.send(400,'Invalid poster request','text/plain')
 field=lambda name:form.get(name,[''])[0]
 if route=='/poster-upload-cancel':
  path=poster_manual.cancel_upload(field('token'))
  return back(path,'Upload cancelled; nothing was changed.') if path else auth.redirect(handler,'/themes')
 if route=='/poster-upload-confirm':
  item=poster_manual.staged(field('token'))
  if item is None:return handler.send(410,'This upload preview expired. Upload the image again.','text/plain')
  try:
   poster_manual.confirm_upload(app,field('token'),field('replace')=='1')
  except (ValueError,OSError) as error:
   return back(item['path'],'Poster not changed: '+(str(error) if isinstance(error,ValueError) else 'it could not be saved.'))
  return back(item['path'],'Your uploaded poster is now in use.')
 path=field('path')
 if not path or len(path)>4096:return handler.send(400,'Invalid anime path','text/plain')
 try:
  if route=='/poster-search':
   token=poster_manual.search(app,path,field('query'))
   return back(path,'',poster_search=token)
  if route=='/poster-reset':
   poster_manual.reset_poster(app,path)
   return back(path,'Poster reset. Preludarr will look for artwork automatically again.')
  index=field('index')
  poster_manual.choose(app,path,field('source'),field('token'),int(index) if index.isdigit() else -1,field('replace')=='1')
  message='The chosen poster is now in use.'
 except ValueError as error:
  message,failed='Poster not changed: '+str(error),True
 except Exception as error:
  print('Manual poster error: '+type(error).__name__,flush=True)
  message,failed='Poster not changed: the provider or image could not be reached. Try again later.',True
 else:
  failed=False
 if field('ajax')=='1':
  return handler.send(200,json.dumps({'ok':not failed,'message':message}),'application/json')
 return back(path,message)
# --- END THEMEARR MANUAL POSTERS V1.6.3 ---


# THEMEARR SMART MATCHING V4 / MEDIA SERVERS V1
import media_servers

class Handler(httpserver.Hardened, BaseHTTPRequestHandler):  # 1.12: timeouts, size limits, bounded concurrency
 def send(self,code,body,ctype='text/html'):
  if ctype=='text/html':body=auth.decorate(self,body)
  body=body.encode();self.send_response(code);self.send_header('Content-Type',ctype+'; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def do_GET(self):
  if not auth.get(self):return
  parsed=urllib.parse.urlsplit(self.path)
  if parsed.path in ('/system','/system/backup/download','/system/backup/verify','/system/backup/status'):
   return system.get_route(self,parsed,CONFIG)
  if parsed.path=='/release-manager':
   return release_notes.route(self,CONFIG)
  if parsed.path=='/preview-custom-upload':
   return custom_upload.preview(self,CONFIG,urllib.parse.parse_qs(parsed.query).get('token',[''])[0])
  if parsed.path in ('/logs','/logs/download'):
   return app_logs.route(self, parsed, CONFIG)
  if parsed.path=='/poster-v2-status':
   return self.send(200,json.dumps(poster_v2_snapshot()),'application/json')
  if parsed.path=='/poster-report':
   return self.send(200,poster_manual.report(sys.modules[__name__]))
  if parsed.path=='/anidb-identify':
   selected=urllib.parse.parse_qs(parsed.query).get('path',[''])[0]
   with db() as c:
    row=c.execute('SELECT name,library FROM shows WHERE path=?',(selected,)).fetchone()
   if not row:return self.send(404,'Unknown anime','text/plain')
   try:
    return self.send(200,anidb.panel_body(sys.modules[__name__],selected,row['name'],row['library']))
   except Exception as error:
    print('AniDB identification error: '+type(error).__name__,flush=True)
    return self.send(500,'AniDB identification failed','text/plain')
  if parsed.path=='/poster-candidates':
   try:
    return self.send(200,json.dumps(poster_manual.candidates_payload(sys.modules[__name__],urllib.parse.parse_qs(parsed.query).get('path',[''])[0])),'application/json')
   except ValueError as error:
    return self.send(404,json.dumps({'error':str(error)}),'application/json')
  if parsed.path in ('/poster-candidate-art','/anime-art'):
   params=urllib.parse.parse_qs(parsed.query)
   try:
    if parsed.path=='/anime-art':
     body=poster_manual.anime_art(sys.modules[__name__],params.get('slug',[''])[0])
    else:
     index=params.get('i',[''])[0]
     body=poster_manual.candidate_art(sys.modules[__name__],params.get('path',[''])[0],int(index) if index.isdigit() else -1)
   except Exception as error:
    if not isinstance(error,ValueError):print('Candidate artwork error: '+type(error).__name__,flush=True)
    return self.send(404,'Artwork unavailable','text/plain')
   self.send_response(200);self.send_header('Content-Type','image/jpeg');self.send_header('Cache-Control','private, max-age=86400');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
  if parsed.path=='/poster-staged':
   item=poster_manual.staged(urllib.parse.parse_qs(parsed.query).get('token',[''])[0])
   if not item:return self.send(404,'Not found','text/plain')
   body=Path(item['file']).read_bytes()
   self.send_response(200);self.send_header('Content-Type','image/jpeg');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
  if parsed.path=='/poster-upload-review':
   page=poster_manual.upload_review(sys.modules[__name__],urllib.parse.parse_qs(parsed.query).get('token',[''])[0])
   if page is None:return self.send(404,'This upload preview expired. Upload the image again.','text/plain')
   return self.send(200,page)
  if parsed.path=='/poster-status':
   selected=urllib.parse.parse_qs(parsed.query).get('path',[''])[0]
   with db() as c:
    known=c.execute('SELECT path FROM shows WHERE path=?',(selected,)).fetchone()
   if not known:
    return self.send(404,json.dumps({'state':'unknown','message':'Unknown anime'}),'application/json')
   return self.send(200,json.dumps(poster_job_status(selected)),'application/json')
  if parsed.path=='/settings/appearance':  # 1.12 Settings → Appearance
   return appearance.page(sys.modules[__name__],self)
  if parsed.path=='/collections/theme':  # 1.12: play a collection's current theme
   return movie_collections.current_theme(sys.modules[__name__],self)
  if parsed.path=='/collections/poster':  # 1.12: collection artwork from the media server (cached)
   return movie_collections.poster(sys.modules[__name__],self)
  if parsed.path=='/collections':  # 1.12: theme songs for movie collections (Plex; Jellyfin/Emby experimental)
   return movie_collections.page(sys.modules[__name__],self)
  if parsed.path=='/queue':
   return theme_queue.page(sys.modules[__name__],self)
  if parsed.path=='/connect/arr-folders':
   return arr.folders_page(sys.modules[__name__],self)
  if parsed.path=='/download-clients':
   import download_clients
   return download_clients.page(sys.modules[__name__],self)
  if parsed.path=='/media-management':
   return root_folders.page(sys.modules[__name__],self)
  if parsed.path=='/library-health':
   return library_health.page(sys.modules[__name__],self)
  if parsed.path=='/library-health/review':
   return theme_review.review_page(sys.modules[__name__],self)
  if parsed.path=='/library-health/trash':
   return theme_review.trash_page(sys.modules[__name__],self)
  if parsed.path=='/library-health/audio':
   return theme_review.audio(sys.modules[__name__],self)
  if parsed.path=='/library-health/status':
   return self.send(200,json.dumps(library_health.status()),'application/json')
  if parsed.path=='/media-management/permissions-status':
   return self.send(200,json.dumps(permissions.status()),'application/json')
  if parsed.path=='/media-management/unmapped':
   return unmapped.page(sys.modules[__name__],self)
  if parsed.path=='/media-management/suggest':
   try:
    return self.send(200,json.dumps(unmapped.suggest(sys.modules[__name__],urllib.parse.parse_qs(parsed.query).get('path',[''])[0])),'application/json')
   except ValueError as error:
    return self.send(400,json.dumps({'error':str(error)}),'application/json')
  if parsed.path=='/media-management/browse':
   try:
    return self.send(200,json.dumps(root_folders.browse(urllib.parse.parse_qs(parsed.query).get('path',[''])[0])),'application/json')
   except (ValueError,OSError) as error:
    return self.send(400,json.dumps({'error':str(error) if isinstance(error,ValueError) else 'Folder could not be read'}),'application/json')
  if parsed.path=='/media-servers/jellyfin-qc/status':
   return jellyfin_quickconnect.get(self,CONFIG)
  if parsed.path=='/media-servers/plex-signin/status':
   return plex_signin.get(self,CONFIG)
  if parsed.path=='/media-servers':
   if not auth.get(self):return
   return poster_connect.render_connect_page(self,CONFIG,media_servers.settings_page,ROOTS,db)

  if parsed.path=='/preview-youtube-theme':
   params=urllib.parse.parse_qs(
    parsed.query,keep_blank_values=True)
   if set(params)!={'token'} or len(params['token'])!=1:
    return self.send(400,'Invalid preview request','text/plain')
   return youtube_manual.preview(
    self,CONFIG,params['token'][0])
  if parsed.path=='/manage-themes':
   return theme_management.page(self,ROOTS,db,CSS)
  if parsed.path in ('/review-remove-theme','/review-restore-theme'):
   operation='remove' if parsed.path=='/review-remove-theme' else 'restore'
   return theme_management.review_action(self,ROOTS,db,CSS,open_safe_theme_directory,verify_theme_directory_identity,auth.token(self),operation)
  if parsed.path=='/current-theme':
   query=urllib.parse.parse_qs(parsed.query)
   selected=query.get('path',[''])[0]
   folder=Path(selected)
   allowed=any(
    folder.parent==root and root.is_dir()
    for _,root in ROOTS
   )
   if not allowed or not folder.is_dir() or folder.is_symlink():
    return self.send(404,'Not found','text/plain')
   with db() as c:
    known=c.execute(
     'SELECT path FROM shows WHERE path=?',(selected,)
    ).fetchone()
   if not known:
    return self.send(404,'Not found','text/plain')
   target=folder/'theme.mp3'
   if target.is_symlink() or not target.is_file():
    return self.send(404,'No theme installed','text/plain')
   with target.open('rb') as audio:
    size=os.fstat(audio.fileno()).st_size
    if size<=0:
     return self.send(404,'Empty theme','text/plain')
    start=0
    end=size-1
    requested=self.headers.get('Range','')
    if requested:
     match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested)
     if not match or (not match.group(1) and not match.group(2)):
      self.send_response(416)
      self.send_header('Content-Range',f'bytes */{size}')
      self.send_header('Content-Length','0')
      self.end_headers()
      return
     if not match.group(1):
      suffix=int(match.group(2))
      start=max(0,size-suffix) if suffix>0 else size
     else:
      start=int(match.group(1))
      if match.group(2):
       end=min(int(match.group(2)),size-1)
     if start>=size or start>end:
      self.send_response(416)
      self.send_header('Content-Range',f'bytes */{size}')
      self.send_header('Content-Length','0')
      self.end_headers()
      return
    length=end-start+1
    self.send_response(206 if requested else 200)
    self.send_header('Content-Type','audio/mpeg')
    self.send_header('Accept-Ranges','bytes')
    self.send_header('Content-Length',str(length))
    self.send_header('Cache-Control','no-store')
    self.send_header('X-Content-Type-Options','nosniff')
    if requested:
     self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
    self.end_headers()
    audio.seek(start)
    while length>0:
     chunk=audio.read(min(262144,length))
     if not chunk:break
     try:
      self.wfile.write(chunk)
     except (BrokenPipeError,ConnectionResetError):
      break
     length-=len(chunk)
   return

  if parsed.path=='/themes':
   query=urllib.parse.parse_qs(parsed.query)
   selected=query.get('path',[''])[0]
   esc=html.escape
   manual_query=query.get('theme_search',[''])[0].strip()[:100]
   manual_slug=query.get('theme_slug',[''])[0].strip()[:120]
   parts=[
    '<!doctype html><html><head>',
    '<meta name="viewport" content="width=device-width,initial-scale=1">',
    '<title>Theme Browser - Preludarr</title><style>',
    CSS,
    '</style>',poster_manual.PICKER_ASSETS,'</head><body>',
    full_sidebar('themes',fixed=True),  # V1.7.3 shared sidebar, Theme Browser highlighted
    '<div class="themearr-content"><main>',
    '<div class="panel"><h2>🎶 Theme Browser</h2>',
    '<p class="muted">Find, preview and install theme songs for your series and movies.</p>',
   ]
   if selected:
    with db() as c:
     row=c.execute('SELECT name,library FROM shows WHERE path=?',(selected,)).fetchone()
    if not row:
     return self.send(404,'Unknown title','text/plain')
    parts+=['<h3>',esc(row['name']),'</h3>']
    if query.get('installed',[''])[0] in ('1','custom'):
     parts+=['<div class="panel" role="status" style="border-color:#3f8f5f"><p>✅ ',
             'Your custom audio was converted and installed as theme.mp3.' if query.get('installed',[''])[0]=='custom' else 'theme.mp3 was installed successfully.',
             '</p></div>']
    parts.append(media_servers.saved_banner(CONFIG,selected))
    parts+=['<p><a href="',esc('/media-servers?'+urllib.parse.urlencode({'path':selected}),quote=True),'"><span style="color:#b8efff;font-weight:700;text-decoration:underline;text-underline-offset:2px">Map media-server item IDs</span></a></p>']
    # V1.9.9: secondary tools are collected here and shown, collapsed, below the available themes
    fragment_mode=query.get('fragment',[''])[0]=='match'
    remember=[]
    if manual_slug and re.fullmatch(r'[A-Za-z0-9_-]{1,120}',manual_slug):
     remember+=['<div class="panel"><form method="post" action="/v4-save-match">',
             '<input type="hidden" name="path" value="',esc(selected,quote=True),'">',
             '<input type="hidden" name="slug" value="',esc(manual_slug,quote=True),'">',
             '<button type="submit">Remember this anime for future matches</button></form></div>']
    folder=Path(selected)
    allowed=any(folder.parent==root and root.is_dir() for _,root in ROOTS)
    target=folder/'theme.mp3'
    if allowed and folder.is_dir() and not folder.is_symlink() and not target.is_symlink() and target.is_file() and target.stat().st_size>0:
     info=target.stat()
     size=info.st_size
     changed=datetime.fromtimestamp(info.st_mtime).strftime('%Y-%m-%d %H:%M')
     url='/current-theme?'+urllib.parse.urlencode({'path':selected})
     parts+=['<div class="panel" id="themearr-current-theme"><h3>Currently Installed Theme</h3>',
             '<p>Installed — theme.mp3 (',f'{size/1048576:.2f}',' MB, last changed ',changed,')</p>',
             '<audio controls preload="none" style="width:100%" src="',
             esc(url,quote=True),'"></audio></div>']
    else:
     parts+=['<div class="panel" id="themearr-current-theme"><h3>Currently Installed Theme</h3>',
             '<p>No theme installed.</p></div>']
    parts.append(library_health.title_box(CONFIG,selected))  # V1.9.3: warning from the last Library Health scan
    extras=[]
    def section(key,title,hint,body,is_open):
     extras.extend(['<details class="tp-sec" id="tp-',key,'"',' open' if is_open else '','><summary><span class="tp-sum-t">',title,
                    '</span><span class="tp-sum-h">',hint,'</span></summary><div class="tp-body">']+body+['</div></details>'])
    if not fragment_mode:
     section('posters','Poster','Search, upload or reset artwork',[poster_manual.panel(sys.modules[__name__],selected,query)],
             bool(query.get('poster_msg',[''])[0] or query.get('poster_search',[''])[0]))
     manual=['<div class="panel"><h3>Search AnimeThemes Manually</h3>',
            '<form method="get" action="/themes">',
            '<input type="hidden" name="path" value="',
            esc(selected,quote=True),'">',
            '<input name="theme_search" maxlength="100" placeholder="Search anime title" value="',
            esc(manual_query,quote=True),'">',
            '<button type="submit">Search</button></form></div>']
     if not kinds.is_regular(row['library']):section('matching','Manual matching','Search AnimeThemes by another title',remember+manual,bool(manual_query or manual_slug))
     upload=['<div class="panel"><h3>Upload Custom Audio</h3>',
            '<p>Choose an MP3, OGG, FLAC, or WAV file (up to 40 MB). Review before replacing theme.mp3.</p>',
            '<form method="post" action="/review-custom-upload" enctype="multipart/form-data">',
            '<input type="hidden" name="path" value="',esc(selected,quote=True),'">',
            '<input type="file" name="audio" accept=".mp3,.ogg,.flac,.wav" required>',
            '<button type="submit">Review Custom Upload</button></form></div>']
     section('upload','Upload custom audio','MP3, OGG, FLAC or WAV — reviewed before install',upload,False)
     youtube=['<div class="panel"><h3>Download from YouTube</h3>',
    '<p>Paste one YouTube URL (10-minute maximum). Review before installing.</p>',
    '<form id="themearr-youtube-form" method="post" action="/review-youtube-theme">',
    '<input type="hidden" name="path" value="',
    esc(selected,quote=True),'">',
    '<input type="url" name="youtube_url" maxlength="2048" '
    'placeholder="https://www.youtube.com/watch?v=..." '
    'style="width:100%" required>',
    '<button type="submit">Download and Review Audio</button>',
    '</form></div>']
     section('youtube','YouTube','Paste one video link — reviewed before install',youtube,False)
    # V1.7.3 faster first visits: the AnimeThemes lookup runs only when the page asks for it
    # (/themes?...&fragment=match); the page itself appears at once with a placeholder.
    match_start=len(parts)
    if fragment_mode:
     if manual_query or manual_slug:
      try:
       if manual_slug:
        params={'include':'animethemes.animethemeentries.videos.audio,animethemes.song'}
        url='https://api.animethemes.moe/anime/'+urllib.parse.quote(manual_slug,safe='')+'?'+urllib.parse.urlencode(params)
        result=request_json(url).get('anime')
        candidates=[result] if isinstance(result,dict) else []
       else:
        params={'q':manual_query,'page[size]':'20'}
        url='https://api.animethemes.moe/anime?'+urllib.parse.urlencode(params)
        candidates=request_json(url).get('anime') or []
       if manual_slug:
        candidates=[s for s in candidates if s.get('slug')==manual_slug]
        if candidates:
         chosen=candidates[0]
         tracks=list_available_themes(chosen)
         parts+=['<h3>Manual selection: ',esc(str(chosen.get('name') or manual_slug)),'</h3>',
                 '<p>',str(len(tracks)),' available audio themes</p>']
         for track in tracks:
          parts+=['<div class="panel"><h3>',
                  esc(str(track['slug'])),' — ',esc(str(track['title'])),
                  '</h3><audio controls preload="none" style="width:100%" src="',
                  esc(track['audio_url'],quote=True),'"></audio>',
                  '<form method="post" action="/install-theme">',
                  '<input type="hidden" name="path" value="',esc(selected,quote=True),'">',
                  '<input type="hidden" name="anime_slug" value="',esc(str(chosen.get('slug') or ''),quote=True),'">',
                  '<input type="hidden" name="video_id" value="',esc(str(track['video_id']),quote=True),'">',
                  '<button type="submit">Install Theme</button>',
                  '</form></div>']
         if not tracks:
          parts+=['<p>No audio themes available for this entry.</p>']
        else:
         parts+=['<p>Selected AnimeThemes entry was not found.</p>']
       else:
        parts+=['<h3>Search Results</h3>']
        if not candidates:
         parts+=['<p>No AnimeThemes entries matched this search.</p>']
        for candidate in candidates[:20]:
         slug=str(candidate.get('slug') or '')
         if not slug:continue
         link='/themes?'+urllib.parse.urlencode({'path':selected,'theme_slug':slug})
         parts+=['<p><a style="color:#9fc1ff" href="',
                 esc(link,quote=True),'">',
                 esc(str(candidate.get('name') or slug)),
                 '</a> — Preview OP/ED tracks</p>']
      except Exception as exc:
       event(row['name'],'error','Theme Browser manual search failed: '+repr(exc)[:600])
       parts+=['<p role="alert">Manual AnimeThemes search failed. The details were recorded in Activity.</p><p><button type="button" data-tp-retry>Try again</button></p>',('<p class="muted">'+html.escape(animethemes.status_note())+'</p>' if animethemes.status_note() else '')]
     if kinds.is_regular(row['library']):  # Preludarr 1.11: Shows / Movies use Plex TV Themes, ThemerrDB and YouTube search
      try:parts.append(__import__('shows_movies').panel(sys.modules[__name__],selected,row['name'],row['library']))
      except Exception as e:
       event(row['name'],'error','Theme suggestions failed: '+repr(e)[:600])
       parts+=['<p role="alert">Theme suggestions could not be loaded. The details were recorded in Activity.</p><p><button type="button" data-tp-retry>Try again</button></p>']
     else:
      try:
       show=(media_servers.resolve(CONFIG,selected,row['name'],{'title':{},'synonyms':[]},anime_theme,request_json) if media_servers.has_override(CONFIG,selected) else anime_theme(row['name'],{'title':{},'synonyms':[]}))
       via='confirmed' if media_servers.has_override(CONFIG,selected) else 'folder'  # V1.9.11: for "Why this match" only
       if (not show and not media_servers.has_override(CONFIG,selected)):
        try:
         media=anilist(row['name'])
        except Exception:
         media=None
        if media:
         show=(media_servers.resolve(CONFIG,selected,row['name'],media,anime_theme,request_json) if media_servers.has_override(CONFIG,selected) else anime_theme(row['name'],media))
         via='anilist'
       if not show:
        parts+=['<p>No matching AnimeThemes entry found.</p>',
                '<p class="muted tp-nomatch">Preludarr only accepts an exact title (or a spelling at least 96% identical) with the same year, and stops when two entries look equally likely. '
                'Use <strong>Manual matching</strong> below to pick the entry yourself.</p>']
        with db() as c:
         st=c.execute('SELECT state,detail FROM shows WHERE path=?',(selected,)).fetchone()
        if st and st['detail']:
         parts+=['<p class="muted">Last automatic attempt: ',esc(str(st['detail'])[:300]),'</p>']
        try:
         parts.append(anidb.panel(sys.modules[__name__],selected,row['name'],row['library']))
        except Exception as error:
         print('AniDB identification error: '+type(error).__name__,flush=True)
        parts.append(media_servers.fallback_panel(CONFIG,selected,row['name'],request_json))
       else:

        themes=list_available_themes(show)
        parts+=['<section class="panel" id="themearr-smart-themes">',
                '<h3>Available Opening &amp; Ending Themes</h3>',
                '<p>',str(len(themes)),' available audio versions</p>',_match_evidence(selected,row,show,via)]
        if themes:
         groups={}
         for track in themes:
          key=str(track.get('slug') or track.get('type') or 'Theme').upper()
          groups.setdefault(key,[]).append(track)
         parts+=['<div class="smart-tools">',
                 '<input id="themearr-smart-search" type="search" placeholder="Search OP, ED or song name" aria-label="Search themes">',
                 '<button type="button" data-smart-filter="all" aria-pressed="true">All</button>',
                 '<button type="button" data-smart-filter="op" aria-pressed="false">Openings</button>',
                 '<button type="button" data-smart-filter="ed" aria-pressed="false">Endings</button>',
                 '<span id="themearr-smart-count" aria-live="polite"></span></div>']
         for key,variants in groups.items():
          kind='op' if key.startswith('OP') else 'ed'
          titles=list(dict.fromkeys(str(t.get('title') or key) for t in variants))
          search=' '.join([key]+titles).lower()
          parts+=['<details data-smart-group="1" data-kind="',kind,
                  '" data-search="',esc(search,quote=True),'">',
                  '<summary><strong>',esc(key),' — ',esc(titles[0]),'</strong>',
                  ' <small>',str(len(variants)),' ',
                  'version' if len(variants)==1 else 'versions',
                  '</small></summary>']
          for number,track in enumerate(variants,1):
           parts+=['<div class="smart-version">',
                   '<p><strong>Version ',str(number),'</strong> — ',
                   esc(str(track.get('title') or key)),'</p>',
                   '<audio controls preload="none" style="width:100%" data-src="',
                   esc(track['audio_url'],quote=True),'" aria-label="Preview ',
                   esc(key,quote=True),' version ',str(number),'"></audio>',
                   '<form method="post" action="/install-theme">',
                   '<input type="hidden" name="path" value="',
                   esc(selected,quote=True),'">',
                   '<input type="hidden" name="anime_slug" value="',
                   esc(str(show.get('slug') or ''),quote=True),'">',
                   '<input type="hidden" name="video_id" value="',
                   esc(str(track['video_id']),quote=True),'">',
                   '<button type="submit">Install Theme</button></form></div>']
          parts+=['</details>']
        else:
         parts+=['<p>No audio themes available.</p>']
        parts.append('\n<style>\n#themearr-smart-themes .smart-tools {\n display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0\n}\n#themearr-smart-themes .smart-tools input {\n flex:1 1 220px;min-width:150px;padding:10px\n}\n#themearr-smart-themes .smart-tools button {\n background:#334362;color:white;border:0;\n border-radius:8px;padding:10px;cursor:pointer\n}\n#themearr-smart-themes .smart-tools button[aria-pressed="true"] {\n background:#5176e8\n}\n#themearr-smart-themes details {\n border:1px solid #3a4864;border-radius:10px;\n margin:10px 0;padding:12px\n}\n#themearr-smart-themes details[hidden] {display:none}\n#themearr-smart-themes summary {cursor:pointer;padding:5px}\n#themearr-smart-themes summary small {\n margin-left:10px;color:#c2cce0\n}\n#themearr-smart-themes .smart-version {\n border-top:1px solid #384760;margin-top:12px;padding-top:12px\n}\n</style>\n<script>\n(() => {\n "use strict";\n const root=document.getElementById("themearr-smart-themes");\n if(!root)return;\n const search=root.querySelector("#themearr-smart-search");\n const count=root.querySelector("#themearr-smart-count");\n const groups=[...root.querySelectorAll("details[data-smart-group]")];\n const buttons=[...root.querySelectorAll("[data-smart-filter]")];\n let filter="all";\n\n function update(){\n   const query=search.value.trim().toLowerCase();\n   let shown=0;\n   for(const group of groups){\n     group.hidden=!((\n       filter==="all"||group.dataset.kind===filter\n     ) && group.dataset.search.includes(query));\n     if(!group.hidden)shown++;\n   }\n   count.textContent=`${shown} of ${groups.length} groups`;\n   for(const button of buttons){\n     button.setAttribute(\n       "aria-pressed",\n       String(button.dataset.smartFilter===filter)\n     );\n   }\n }\n\n search.addEventListener("input",update);\n for(const button of buttons){\n   button.addEventListener("click",()=>{\n     filter=button.dataset.smartFilter;\n     update();\n   });\n }\n\n for(const group of groups){\n   group.addEventListener("toggle",()=>{\n     if(!group.open){\n       group.querySelectorAll("audio").forEach(a=>a.pause());\n       return;\n     }\n     for(const audio of group.querySelectorAll("audio[data-src]")){\n       audio.src=audio.dataset.src;\n       audio.removeAttribute("data-src");\n       audio.load();\n     }\n   });\n }\n\n update();\n})();\n</script>\n')
        parts+=['</section>']
      except Exception as e:
       event(row['name'],'error','Theme Browser lookup failed: '+repr(e)[:600])
       parts+=['<p role="alert">Theme lookup failed. The details were recorded in Activity.</p><p><button type="button" data-tp-retry>Try again</button></p>',('<p class="muted">'+html.escape(animethemes.status_note())+'</p>' if animethemes.status_note() else '')]
    if fragment_mode:
     return self.send(200,''.join(parts[match_start:]))
    match_url='/themes?'+urllib.parse.urlencode({**{k:v[0] for k,v in query.items() if k in ('path','theme_search','theme_slug') and v},'fragment':'match'})
    # V1.9.11: the themes load into a slot that can be reloaded in place (Try again) without reloading the page
    regular=kinds.is_regular(row['library'])
    if query.get('suggest_msg',[''])[0]:parts+=['<p class="notice" role="status">',esc(query.get('suggest_msg',[''])[0][:220]),'</p>']
    parts+=['<div id="tp-match-slot" aria-live="polite" data-match-url="',esc(match_url,quote=True),'"><section class="panel" id="themearr-match" aria-busy="true"><h3>',
            'Theme suggestions' if regular else 'Available Opening &amp; Ending Themes','</h3>',
            '<p class="muted tp-loading" role="status">',('Looking for theme suggestions (Plex TV Themes, ThemerrDB, YouTube search)…' if regular else 'Looking up themes on AnimeThemes…'),
            ' Everything else on this page is ready to use while this loads.</p></section></div>',
            '<script>(function(){"use strict";const slot=document.getElementById("tp-match-slot");if(!slot)return;const loading=slot.innerHTML;',
            'function run(){for(const o of [...slot.querySelectorAll("script")]){const n=document.createElement("script");n.textContent=o.textContent;o.replaceWith(n)}}',
            'function fail(msg){slot.innerHTML="";const s=document.createElement("section");s.className="panel";s.id="themearr-match";s.innerHTML="<h3>Available Opening &amp; Ending Themes</h3>";',
            'const p=document.createElement("p");p.setAttribute("role","alert");p.textContent="Theme lookup could not finish ("+msg+").";',
            'const b=document.createElement("button");b.type="button";b.dataset.tpRetry="1";b.textContent="Try again";s.append(p,b);slot.append(s)}',
            'function load(){slot.innerHTML=loading;fetch(slot.dataset.matchUrl,{credentials:"same-origin",cache:"no-store"}).then(r=>{if(!r.ok)throw new Error("HTTP "+r.status);return r.text()})',
            '.then(h=>{slot.innerHTML=h;run()}).catch(e=>fail(e.message||"network error"))}',
            'slot.addEventListener("click",e=>{if(e.target.closest("[data-tp-retry]")){e.preventDefault();load()}});load()})();</script>']
    parts+=['<section class="tp-more" aria-labelledby="tp-more-h"><h3 class="tp-more-h" id="tp-more-h">More options</h3>']+extras+['</section>',TITLE_PAGE_CSS,
            '<script>(function(){"use strict";function openFor(id){if(!id)return;const t=document.getElementById(id);const d=t&&t.closest("details.tp-sec");if(d&&!d.open)d.open=true}',
            'openFor(decodeURIComponent(location.hash.slice(1)));addEventListener("hashchange",()=>openFor(decodeURIComponent(location.hash.slice(1))))})();</script>']
   else:
    term=query.get('q',[''])[0].strip()[:100]
    library=query.get('library',[''])[0]
    library=library if kinds.valid(library) else ''
    state=query.get('state',[''])[0]
    state=state if state in ('installed','missing') else ''
    try:page=max(1,int(query.get('page',['1'])[0]))
    except ValueError:page=1
    per_page=50
    where=[];args=[]
    if term:
     where.append("name LIKE ? ESCAPE '\\'")
     args.append('%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
    if library:where.append('library=?');args.append(library)
    if state:where.append('state=?');args.append(state)
    clause=(' WHERE '+' AND '.join(where)) if where else ''
    with db() as c:
     total=c.execute('SELECT COUNT(*) FROM shows'+clause,args).fetchone()[0]
     pages=max(1,(total+per_page-1)//per_page);page=min(page,pages)
     rows=c.execute('SELECT path,name,library,state FROM shows'+clause+' ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?',args+[per_page,(page-1)*per_page]).fetchall()
    def option(value,label,current):
     return '<option value="'+esc(value,quote=True)+'"'+(' selected' if value==current else '')+'>'+esc(label)+'</option>'
    parts+=['<form method="get" action="/themes" id="themearr-browser-index">',
            '<input type="search" name="q" maxlength="100" placeholder="Search any series or movie title" aria-label="Search titles" value="',esc(term,quote=True),'">',
            '<select name="library" aria-label="Library">',option('','All libraries',library),*[option(k,kinds.label(k),library) for k in kinds.present(ROOTS)],'</select>',
            '<select name="state" aria-label="Theme status">',option('','Any status',state),option('installed','Installed',state),option('missing','Missing theme',state),'</select>',
            '<button type="submit">Search</button></form>']
    first=(page-1)*per_page+1 if total else 0
    parts+=['<p class="muted">Showing ',str(first),'–',str(min(total,page*per_page)),' of ',str(total),' titles. Select a title to explore its themes.</p>']
    for row in rows:
     link='/themes?'+urllib.parse.urlencode({'path':row['path']})
     parts+=[
      '<p><a style="color:#9fc1ff" href="',
      esc(link,quote=True),'">',
      esc(row['name']),' — ',esc(row['library']),
      '</a> <span class="card-status ',esc(row['state'] or '',quote=True),'">',esc(row['state'] or ''),'</span></p>'
     ]
    if not rows:
     parts+=['<p>No matching titles.</p>']
    if pages>1:
     def page_link(number,label):
      params={k:v for k,v in (('q',term),('library',library),('state',state)) if v}
      params['page']=str(number)
      return '<a style="color:#9fc1ff" href="'+esc('/themes?'+urllib.parse.urlencode(params),quote=True)+'">'+label+'</a>'
     parts+=['<p class="themearr-pager">',
             page_link(page-1,'← Previous') if page>1 else '',
             ' Page ',str(page),' of ',str(pages),' ',
             page_link(page+1,'Next →') if page<pages else '',
             '</p>']
    parts+=['<style>#themearr-browser-index{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}#themearr-browser-index input{flex:1 1 220px;min-width:150px;padding:10px}#themearr-browser-index select,#themearr-browser-index button{padding:10px}.themearr-pager{display:flex;gap:16px;align-items:center}</style>']
   parts += ['\n\n<style>\n#themearr-youtube-dialog {\n  width: min(700px, calc(100vw - 32px));\n  max-height: 88vh;\n  overflow-y: auto;\n  background: #1c263b;\n  color: #f5f7ff;\n  border: 1px solid #43577d;\n  border-radius: 16px;\n  padding: 24px;\n  box-sizing: border-box;\n  box-shadow: 0 24px 70px #0009;\n  font-family: inherit;\n}\n#themearr-youtube-dialog::backdrop {\n  background: rgba(2, 6, 18, .82);\n  backdrop-filter: blur(5px);\n}\n#themearr-youtube-dialog h2,\n#themearr-youtube-dialog h3 {\n  color: #f5f7ff;\n}\n#themearr-youtube-dialog p {\n  line-height: 1.6;\n}\n#themearr-youtube-dialog audio {\n  width: 100%;\n  margin: 8px 0 16px;\n}\n#themearr-youtube-dialog a {\n  color: #a8c4ff;\n}\n#themearr-youtube-dialog button {\n  border: 0;\n  border-radius: 8px;\n  padding: 11px 17px;\n  margin: 5px 8px 5px 0;\n  background: #4e73ed;\n  color: white;\n  font-weight: 700;\n  cursor: pointer;\n}\n#themearr-youtube-dialog\nform[action="/cancel-youtube-theme"] button {\n  background: #36435e;\n}\n#themearr-youtube-dialog button:disabled {\n  opacity: .5;\n  cursor: wait;\n}\n#themearr-youtube-close {\n  float: right;\n  background: #34415b !important;\n}\n#themearr-youtube-status {\n  margin: 12px 0;\n  padding: 10px 12px;\n  background: #263652;\n  border-radius: 8px;\n}\n#themearr-youtube-status[hidden] {\n  display: none;\n}\n#themearr-youtube-status.is-busy::before {\n  content: "";\n  display: inline-block;\n  width: 13px;\n  height: 13px;\n  border: 2px solid #ffffff50;\n  border-top-color: #ffffff;\n  border-radius: 50%;\n  margin-right: 10px;\n  vertical-align: middle;\n  animation: themearr-yt-spin .8s linear infinite;\n}\n@keyframes themearr-yt-spin {\n  to { transform: rotate(360deg); }\n}\n</style>\n\n\n<style>\n#themearr-smart-install-dialog {width:min(620px,calc(100vw - 28px));max-height:88vh;overflow:auto;color:#f4f7ff;background:#1b263a;border:1px solid #506487;border-radius:14px;padding:22px;box-shadow:0 22px 70px #0009}\n#themearr-smart-install-dialog::backdrop {background:rgba(0,0,0,.72)}\n#themearr-smart-install-dialog h2 {margin-top:0}\n#themearr-smart-install-dialog .si-actions {display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}\n#themearr-smart-install-dialog button {border:0;border-radius:9px;padding:12px 17px;cursor:pointer;font-weight:700;color:white}\n#themearr-smart-install-dialog button.si-cancel {background:#52617c}\n#themearr-smart-install-dialog button.si-confirm {background:#3768ee}\n#themearr-smart-install-dialog button:disabled {opacity:.55;cursor:wait}\n#themearr-smart-install-dialog .si-note {padding:12px;border-radius:8px;background:#2b3850;margin:12px 0}\n#themearr-smart-install-dialog .si-status {min-height:24px;margin:10px 0;overflow-wrap:anywhere}\n#themearr-smart-install-dialog .si-status[aria-busy="true"]:before {content:"◌ ";display:inline-block;animation:si-spin 1s linear infinite}\n@keyframes si-spin {to{transform:rotate(360deg)}}\n</style>\n<dialog id="themearr-smart-install-dialog" aria-labelledby="si-title">\n <h2 id="si-title">Review Theme Installation</h2>\n <p id="si-track"></p>\n <p id="si-destination">Destination: theme.mp3</p>\n <audio id="si-preview" controls preload="none" hidden style="width:100%"></audio>\n <p id="si-note" class="si-note"></p>\n <p id="si-status" class="si-status" role="status" aria-live="polite"></p>\n <div class="si-actions">\n  <button id="si-cancel" type="button" class="si-cancel">Cancel / Go Back</button>\n  <button id="si-confirm" type="button" class="si-confirm" disabled>Confirm &amp; Install</button>\n </div>\n</dialog>\n<script>\n(() => {\n "use strict";\n const dialog=document.getElementById("themearr-smart-install-dialog");\n if (!dialog || typeof dialog.showModal!=="function") return;\n const trackLabel=dialog.querySelector("#si-track");\n const note=dialog.querySelector("#si-note");\n const preview=dialog.querySelector("#si-preview");\n const status=dialog.querySelector("#si-status");\n const cancel=dialog.querySelector("#si-cancel");\n const confirm=dialog.querySelector("#si-confirm");\n let pending=null, busy=false, finished=false, selectedPath="";\n function message(value,loading=false){status.textContent=value;status.setAttribute("aria-busy",String(loading));}\n function reset(){pending=null;busy=false;finished=false;selectedPath="";confirm.disabled=true;confirm.hidden=false;confirm.textContent="Confirm & Install";cancel.hidden=false;cancel.disabled=false;note.textContent="";preview.pause();preview.removeAttribute("src");preview.hidden=true;message("");}\n dialog.addEventListener("cancel",event=>{if(busy)event.preventDefault();});\n dialog.addEventListener("close",()=>{preview.pause();if(finished)window.location.reload();});\n cancel.addEventListener("click",()=>{if(!busy)dialog.close();});\n document.addEventListener("submit",async event=>{\n   const form=event.target;\n   if (!(form instanceof HTMLFormElement) || form.getAttribute("action")!=="/install-theme" || busy || dialog.open) return;\n   event.preventDefault();\n   reset();\n   selectedPath=new FormData(form).get("path")||"";\n   const group=form.closest("details[data-smart-group]");\n   const groupTitle=group?.querySelector("summary strong")?.textContent || form.closest(".panel")?.querySelector("h3")?.textContent || "AnimeThemes track";\n   const variant=form.closest(".smart-version")?.querySelector("p")?.textContent || "";\n   trackLabel.textContent=[groupTitle,variant].filter(Boolean).join(" — ");\n   const player=form.closest(".smart-version,.panel")?.querySelector("audio");\n   const audioSource=player?.getAttribute("src") || player?.getAttribute("data-src");\n   if(audioSource){\n     const audioURL=new URL(audioSource,location.href);\n     if(audioURL.protocol==="https:" && ["a.animethemes.moe","v.animethemes.moe"].includes(audioURL.hostname)){\n       preview.src=audioURL.href;preview.hidden=false;\n     }\n   }\n   dialog.showModal();busy=true;cancel.disabled=true;\n   message("Checking this theme and its destination…",true);\n   try {\n     const response=await fetch("/install-theme",{method:"POST",body:new URLSearchParams(new FormData(form)),credentials:"same-origin",redirect:"follow"});\n     if(!response.ok)throw Error((await response.text()).slice(0,200));\n     const page=new DOMParser().parseFromString(await response.text(),"text/html");\n     const approval=page.querySelector(\'form[action="/confirm-theme"]\');\n     if(!approval || !approval.querySelector(\'input[name="token"]\'))throw Error("Confirmation unavailable. Your session may have expired.");\n     pending=new URLSearchParams(new FormData(approval));\n     if(pending.get("path")!==selectedPath)throw Error("The reviewed destination did not match.");\n     note.textContent=page.body.textContent.includes("Existing theme detected") ? "Existing theme.mp3 detected. Confirming will replace it." : "No existing theme.mp3 detected. Confirm to install.";\n     message("Review the selected theme, then confirm or cancel.");\n     confirm.disabled=false;\n   } catch(error){message("Review failed: "+String(error.message||error));}\n   finally {busy=false;cancel.disabled=false;}\n });\n confirm.addEventListener("click",async()=>{\n   if(finished){dialog.close();return;}\n   if(busy || !pending)return;\n   busy=true;confirm.disabled=true;cancel.disabled=true;\n   message("Downloading, verifying and installing audio…",true);\n   try {\n     const response=await fetch("/confirm-theme",{\n     method:"POST",body:pending,credentials:"same-origin",redirect:"follow",\n     headers:{"X-Themearr-Progress":"v2","Accept":"application/x-ndjson"}\n   });\n   if(!response.ok)throw Error((await response.text()).slice(0,200));\n   const type=(response.headers.get("content-type")||"").toLowerCase();\n   if(type.includes("application/x-ndjson")){\n     if(!response.body)throw Error("Install status stream unavailable; refresh to check installed theme before retrying.");\n     const reader=response.body.getReader();\n     const decoder=new TextDecoder();\n     let buffer="",completed=false,bytes=0;\n     function onLine(line){\n       if(!line.trim())return;\n       if(line.length>4096)throw Error("Install status response too large");\n       const event=JSON.parse(line);\n       if(!event || typeof event.phase!=="string" || typeof event.message!=="string")\n         throw Error("Invalid install status response");\n       if(event.phase==="error")throw Error(event.message);\n       if(event.phase==="complete")completed=true;\n       message(event.message,event.phase!=="complete");\n     }\n     while(true){\n       const chunk=await reader.read();\n       if(chunk.done)break;\n       bytes+=chunk.value.byteLength;\n       if(bytes>65536)throw Error("Install status response exceeds limit");\n       buffer+=decoder.decode(chunk.value,{stream:true});\n       if(buffer.length>65536)throw Error("Install status response exceeds limit");\n       let newline;\n       while((newline=buffer.indexOf("\\n"))!==-1){\n         const line=buffer.slice(0,newline);buffer=buffer.slice(newline+1);\n         onLine(line);\n       }\n     }\n     buffer+=decoder.decode();\n     if(buffer.trim())onLine(buffer);\n     if(!completed)throw Error("Installation result not confirmed; refresh Theme Browser before retrying.");\n   }else{\n     // Compatibility if a previously deployed server ignores streamed progress.\n     const destination=new URL(response.url,location.href);\n     if(destination.origin!==location.origin || destination.pathname!=="/themes" ||\n        destination.searchParams.get("installed")!=="1" ||\n        destination.searchParams.get("path")!==selectedPath)\n       throw Error("Installation result not confirmed; refresh Theme Browser before retrying.");\n   }\n   finished=true;pending=null;\n     note.textContent="theme.mp3 was installed successfully.";\n     message("Installation complete. Refresh Theme Browser to view the updated player.");\n     confirm.textContent="Done — Refresh Browser";\n     confirm.disabled=false;cancel.hidden=true;\n   } catch(error){pending=null;message("Installation not confirmed: "+String(error.message||error)+" Close and reopen to review again.");}\n   finally{busy=false;cancel.disabled=false;}\n });\n})();\n</script>\n\n<dialog id="themearr-youtube-dialog"\n        aria-label="Download and install YouTube theme">\n  <button id="themearr-youtube-close" type="button"\n          aria-label="Close popup">✕</button>\n  <div id="themearr-youtube-status" role="status"\n       aria-live="polite" hidden></div>\n  <div id="themearr-youtube-modal-content"></div>\n</dialog>\n\n<script>\n(() => {\n  "use strict";\n\n  const form = document.getElementById("themearr-youtube-form");\n  const dialog = document.getElementById("themearr-youtube-dialog");\n\n  if (!form || !dialog) return;\n\n  const content = document.getElementById(\n    "themearr-youtube-modal-content"\n  );\n  const status = document.getElementById(\n    "themearr-youtube-status"\n  );\n  const closeButton = document.getElementById(\n    "themearr-youtube-close"\n  );\n\n  let busy = false;\n  let token = null;\n\n  function setStatus(message, loading = false) {\n    status.hidden = !message;\n    status.textContent = message;\n    status.classList.toggle("is-busy", loading);\n  }\n\n  function setBusy(value) {\n    busy = value;\n    closeButton.disabled = value;\n\n    for (const button of content.querySelectorAll("button")) {\n      button.disabled = value;\n    }\n  }\n\n  async function post(url, parameters) {\n    const response = await fetch(url, {\n      method: "POST",\n      credentials: "same-origin",\n      cache: "no-store",\n      headers: {\n        "Content-Type":\n          "application/x-www-form-urlencoded;charset=UTF-8"\n      },\n      body: parameters.toString()\n    });\n\n    return {\n      ok: response.ok,\n      status: response.status,\n      body: await response.text()\n    };\n  }\n\n  function show() {\n    if (!dialog.open) dialog.showModal();\n  }\n\n  function showError(message) {\n    setStatus("Error: " + message, false);\n  }\n\n  async function refreshInstalledTheme() {\n    try {\n      const response = await fetch(\n        window.location.pathname + window.location.search,\n        { credentials: "same-origin", cache: "no-store" }\n      );\n\n      if (!response.ok) return;\n\n      const documentCopy = new DOMParser().parseFromString(\n        await response.text(), "text/html"\n      );\n\n      const replacement = documentCopy.getElementById(\n        "themearr-current-theme"\n      );\n      const existing = document.getElementById(\n        "themearr-current-theme"\n      );\n\n      if (replacement && existing) {\n        existing.replaceWith(\n          document.importNode(replacement, true)\n        );\n      }\n    } catch (error) {\n      setStatus(\n        "Installed successfully. Refresh the page to see the updated player."\n      );\n    }\n  }\n\n\n  function playlistVideoUrl(raw) {\n    let url;\n\n    try {\n      url = new URL(raw);\n    } catch {\n      return undefined;\n    }\n\n    const host = url.hostname.toLowerCase();\n\n    const allowed = [\n      "youtube.com",\n      "www.youtube.com",\n      "m.youtube.com",\n      "music.youtube.com",\n      "youtu.be"\n    ];\n\n    if (!allowed.includes(host)) {\n      return undefined;\n    }\n\n    const playlist =\n      url.searchParams.has("list") ||\n      url.searchParams.has("start_radio") ||\n      url.pathname === "/playlist";\n\n    if (!playlist) {\n      return undefined;\n    }\n\n    let id = url.searchParams.get("v");\n\n    if (host === "youtu.be" && !id) {\n      id = url.pathname.slice(1).split("/")[0];\n    }\n\n    if (/^[A-Za-z0-9_-]{11}$/.test(id || "")) {\n      return "https://www.youtube.com/watch?v=" + id;\n    }\n\n    return null;\n  }\n\n  function showPlaylistHelp(videoUrl) {\n    token = null;\n    content.replaceChildren();\n    show();\n    setStatus("");\n\n    const heading = document.createElement("h2");\n    heading.textContent = "You pasted a YouTube playlist link";\n    content.append(heading);\n\n    const explanation = document.createElement("p");\n    explanation.textContent =\n      "This link includes a YouTube Mix or playlist. " +\n      "Preludarr downloads one video at a time, " +\n      "not an entire playlist.";\n    content.append(explanation);\n\n    if (videoUrl) {\n      const guidance = document.createElement("p");\n      guidance.textContent =\n        "Good news! Your link contains a specific video. " +\n        "Use this video-only link instead:";\n      content.append(guidance);\n\n      const link = document.createElement("p");\n      link.textContent = videoUrl;\n      link.style.cssText =\n        "padding:12px;background:#10192c;" +\n        "border-radius:8px;overflow-wrap:anywhere;" +\n        "color:#a8c4ff";\n      content.append(link);\n\n      const useButton = document.createElement("button");\n      useButton.type = "button";\n      useButton.textContent = "Use This Video & Download";\n\n      useButton.addEventListener("click", () => {\n        const input = form.querySelector(\n          \'input[name="youtube_url"]\'\n        );\n\n        if (!input) return;\n\n        input.value = videoUrl;\n        dialog.close();\n        form.requestSubmit();\n      });\n\n      content.append(useButton);\n    } else {\n      const guidance = document.createElement("p");\n      guidance.textContent =\n        "To download one video: open the playlist on YouTube, " +\n        "select the specific video you want, click Share, " +\n        "then Copy link. Paste that individual video link " +\n        "into Preludarr.";\n      content.append(guidance);\n    }\n\n    const tip = document.createElement("p");\n    tip.textContent =\n      "Tip: A regular video link looks like " +\n      "youtube.com/watch?v=VIDEO_ID. " +\n      "Playlist links often include list= or start_radio=.";\n    tip.style.color = "#b8c5dd";\n    content.append(tip);\n\n    const backButton = document.createElement("button");\n    backButton.type = "button";\n    backButton.textContent = "Back to URL Field";\n    backButton.style.background = "#36435e";\n\n    backButton.addEventListener("click", () => {\n      dialog.close();\n      const input = form.querySelector(\n        \'input[name="youtube_url"]\'\n      );\n      if (input) input.focus();\n    });\n\n    content.append(backButton);\n  }\n\n  form.addEventListener("submit", async (event) => {\n    event.preventDefault();\n\n    if (busy) return;\n\n    const input = form.querySelector(\n      \'input[name="youtube_url"]\'\n    );\n\n    if (input) {\n      const corrected = playlistVideoUrl(input.value);\n\n      if (corrected !== undefined) {\n        showPlaylistHelp(corrected);\n        return;\n      }\n    }\n\n    token = null;\n    content.replaceChildren();\n    show();\n    setStatus("Downloading and preparing YouTube audio…", true);\n    setBusy(true);\n\n    try {\n      const result = await post(\n        form.action,\n        new URLSearchParams(new FormData(form))\n      );\n\n      if (!result.ok) {\n        throw new Error(result.body.slice(0, 350));\n      }\n\n      const review = new DOMParser().parseFromString(\n        result.body, "text/html"\n      );\n\n      const reviewMain = review.querySelector("main");\n      const confirmation = review.querySelector(\n        \'form[action="/confirm-youtube-theme"] input[name="token"]\'\n      );\n\n      if (!reviewMain || !confirmation ||\n          !/^[A-Za-z0-9_-]{20,128}$/.test(confirmation.value)) {\n        throw new Error(\n          "Review response was invalid. Sign in again if necessary."\n        );\n      }\n\n      token = confirmation.value;\n\n      const children = Array.from(reviewMain.childNodes)\n        .map(node => document.importNode(node, true));\n\n      content.replaceChildren(...children);\n\n      const message = document.createElement("p");\n      message.textContent =\n        "Convert this audio to MP3 and install it as theme.mp3?";\n\n      const confirmForm = content.querySelector(\n        \'form[action="/confirm-youtube-theme"]\'\n      );\n\n      if (confirmForm) {\n        confirmForm.before(message);\n\n        const button = confirmForm.querySelector("button");\n        if (button) button.textContent = "Confirm & Install (MP3)";\n      }\n\n      setStatus("");\n\n    } catch (error) {\n      showError(error.message || "YouTube download failed.");\n    } finally {\n      setBusy(false);\n    }\n  });\n\n  async function cancelReview() {\n    if (busy) return;\n\n    if (!token) {\n      dialog.close();\n      return;\n    }\n\n    setBusy(true);\n    setStatus("Canceling and discarding downloaded audio…", true);\n\n    try {\n      const result = await post(\n        "/cancel-youtube-theme",\n        new URLSearchParams({ token })\n      );\n\n      if (!result.ok) {\n        throw new Error(result.body.slice(0, 250));\n      }\n\n      token = null;\n      setStatus("");\n      dialog.close();\n\n    } catch (error) {\n      showError(error.message || "Cancellation failed.");\n    } finally {\n      setBusy(false);\n    }\n  }\n\n  dialog.addEventListener("submit", async (event) => {\n    const submitted = event.target;\n\n    if (!(submitted instanceof HTMLFormElement)) return;\n\n    const path = new URL(\n      submitted.action, window.location.href\n    ).pathname;\n\n    if (path === "/cancel-youtube-theme") {\n      event.preventDefault();\n      await cancelReview();\n      return;\n    }\n\n    if (path !== "/confirm-youtube-theme") return;\n\n    event.preventDefault();\n\n    if (busy || !token) return;\n\n    setBusy(true);\n    setStatus(\n      "Converting audio to MP3 and installing your theme…",\n      true\n    );\n\n    try {\n      const result = await post(\n        submitted.action,\n        new URLSearchParams(new FormData(submitted))\n      );\n\n      if (!result.ok) {\n        throw new Error(result.body.slice(0, 300));\n      }\n\n      token = null;\n\n      const heading = document.createElement("h2");\n      heading.textContent = "Theme Installed Successfully!";\n\n      const description = document.createElement("p");\n      description.textContent =\n        "Your YouTube theme was converted to MP3 and installed.";\n\n      const finish = document.createElement("button");\n      finish.type = "button";\n      finish.textContent = "Done";\n      finish.addEventListener("click", () => dialog.close());\n\n      content.replaceChildren(heading, description, finish);\n\n      setStatus("Installation complete.", false);\n\n      await refreshInstalledTheme();\n\n    } catch (error) {\n      showError(error.message || "Installation failed.");\n    } finally {\n      setBusy(false);\n    }\n  });\n\n  closeButton.addEventListener("click", cancelReview);\n\n  dialog.addEventListener("cancel", (event) => {\n    if (busy || token) {\n      event.preventDefault();\n    }\n  });\n})();\n</script>\n\n']
   parts+=['</div></main></div></body></html>']
   return self.send(200,''.join(parts))

  if parsed.path=='/health':return self.send(200,'ok','text/plain')
  if parsed.path=='/api/v1/status':  # V1.9.7 API (sign-in or X-Api-Key)
   with db() as c:
    counts={r['state']:r['total'] for r in c.execute('SELECT state,COUNT(*) AS total FROM shows GROUP BY state')}
   import login as _tl_api
   payload={'app':'Preludarr','formerly':'Splicarr, Themearr','version':_tl_api.APP_VERSION,'instance':_tl_api.instance_name(),'running':STATUS['running'],
            'message':STATUS['message'],'last_scan':STATUS['last_scan'],'titles':sum(counts.values()),'installed':counts.get('installed',0),
            'missing':counts.get('missing',0),'review':counts.get('review',0),'counts':counts,'queue':theme_queue.counts(sys.modules[__name__])}
   return self.send(200,json.dumps(payload),'application/json')
  if parsed.path=='/api/v1/activity':
   try:limit=max(1,min(100,int(urllib.parse.parse_qs(parsed.query).get('limit',['20'])[0])))
   except ValueError:limit=20
   with db() as c:
    rows=[dict(r) for r in c.execute('SELECT at,name,status,detail FROM events ORDER BY id DESC LIMIT ?',(limit,))]
   return self.send(200,json.dumps({'events':rows}),'application/json')
  if parsed.path=='/api/status':
   with db() as c:
    counts={r['state']:r['total'] for r in c.execute('SELECT state,COUNT(*) AS total FROM shows GROUP BY state')}
   payload={'running':STATUS['running'],'message':STATUS['message'],'stage':STATUS['stage'],'active_path':STATUS['active_path'],'last_scan':STATUS['last_scan'],'counts':counts,
            'display':status_display(),'scan_line':scan_line()}  # V1.9.14: display/scan_line added; old fields unchanged
   return self.send(200,json.dumps(payload),'application/json')
  if parsed.path=='/poster-filtered-status':
   return poster_filters.status(self,db,poster_file,parsed.query,POSTER_BUSY,POSTER_LOCK,poster_v2_paused)
  if parsed.path=='/poster':
    query=urllib.parse.parse_qs(parsed.query)
    path=query.get('path',[''])[0]
    with db() as c:known=c.execute('SELECT path FROM shows WHERE path=?',(path,)).fetchone()
    if not known:return self.send(404,'Not found','text/plain')
    file=poster_file(path)
    if not file.is_file():return self.send(404,'Not found','text/plain')
    info=file.stat();tag='"'+format(info.st_mtime_ns,'x')+'-'+format(info.st_size,'x')+'"'
    if self.headers.get('If-None-Match','')==tag:
     self.send_response(304);self.send_header('ETag',tag);self.send_header('Cache-Control','private, no-cache');self.end_headers();return
    body=file.read_bytes()
    self.send_response(200);self.send_header('Content-Type','image/jpeg');self.send_header('ETag',tag);self.send_header('Cache-Control','private, no-cache');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
  if parsed.path!='/':return self.send(404,'Not found')
  query=urllib.parse.parse_qs(parsed.query)
  # Preludarr 1.11: the dashboard no longer lists titles — old bookmarked dashboard searches open the Library with the same filters
  if parsed.path=='/' and not query.get('view') and any(query.get(k,[''])[0] not in ('','all') for k in ('q','library','state','poster','page')):
   return auth.redirect(self,'/?'+urllib.parse.urlencode({'view':'library',**{k:v[0] for k,v in query.items() if k in ('q','library','state','poster','page')}}))
  term=query.get('q',[''])[0].strip()[:120]
  library=query.get('library',['all'])[0]
  state=query.get('state',['all'])[0]
  poster_status=query.get('poster',['all'])[0]
  if poster_status not in ('all','missing','cached','failed'):poster_status='all'
  if library!='all' and not root_folders.valid_scope(library):library='all'
  # Preludarr 1.11.9: Library tabs are per root folder; an older type link opens that type's first root folder tab
  if query.get('view',[''])[0]=='library' and library in kinds.TYPES:
   first=next((r for l,r in ROOTS if l==library),None)
   if first is not None:
    return auth.redirect(self,'/?'+urllib.parse.urlencode({'view':'library','library':root_folders.root_value(first),**{k:query[k][0] for k in ('q','state','poster','page') if query.get(k)}}))
  if state not in ('all','recent','missing','installed','review','searching','downloading','converting'):state='all'
  try:page=max(1,int(query.get('page',['1'])[0]))
  except (ValueError,TypeError):page=1
  page=min(page,1000000)
  clauses=[];params=[]
  if term:
   # Escape LIKE wildcards so searches match literal title text.
   escaped=term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
   clauses.append("name LIKE ? ESCAPE '\\'")
   params.append('%'+escaped+'%')
  scope_sql,scope_params=root_folders.scope_clause(library)
  if scope_sql:clauses.append(scope_sql);params+=scope_params
  if state=='recent':
   recent_sql,recent_params=auto_themes.recent_clause();clauses.append(recent_sql);params+=recent_params
  elif state!='all':clauses.append('state=?');params.append(state)
  extra_poster=poster_filters.filter_clause(poster_status)
  if extra_poster:clauses.append('('+extra_poster+')')
  where=' WHERE '+' AND '.join(clauses) if clauses else ''
  per_page=50
  with db() as c:
   poster_filters.register(c,poster_file)
   total=c.execute('SELECT COUNT(*) FROM shows'+where,params).fetchone()[0]
   pages=max(1,(total+per_page-1)//per_page)
   page=min(page,pages)
   order=" ORDER BY (SELECT added FROM show_added WHERE show_added.path=shows.path) DESC,name" if state=='recent' else " ORDER BY CASE state WHEN 'review' THEN 0 WHEN 'missing' THEN 1 ELSE 2 END,name"
   rows=c.execute('SELECT * FROM shows'+where+order+" LIMIT ? OFFSET ?",params+[per_page,(page-1)*per_page]).fetchall()
   events=c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 20').fetchall()
   page_paths=[r['path'] for r in rows]
   pick_paths={a['path'] for a in c.execute('SELECT path FROM poster_attempts WHERE candidates NOT IN (\'\',\'[]\') AND path IN (SELECT value FROM json_each(?))',(json.dumps(page_paths),))}
   counts={r['state']:r['total'] for r in c.execute('SELECT state,COUNT(*) AS total FROM shows GROUP BY state')}
   poster_preview_count=len(poster_filters.candidates(c,{'q':term,'library':library,'state':state,'poster':poster_status},poster_file))
  esc=html.escape
  view=query.get('view',[''])[0];view=view if view in ('activity','library') else ''
  # V1.7.3: Anime Series / Anime Movies / Missing Themes open a library view; the sidebar highlights where you are.
  # Preludarr 1.11 (user 2026-10-29): one sidebar "Library" entry; inside it a tab per root-folder type.
  lib_root=root_folders.root_of(library)
  lib_tab=view=='library' and bool(lib_root)
  nav_active=('activity' if view=='activity' else 'library' if lib_tab else 'missing' if view=='library' and state=='missing' else 'dashboard')
  library_title='📚 Library' if lib_tab else {'missing':'📥 Missing Themes'}.get(nav_active,'📚 Library')
  def url(p):return '/?'+urllib.parse.urlencode({**({'view':'library'} if view=='library' else {}),'q':term,'library':library,'state':state,'poster':poster_status,'page':p})
  parts=['<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Preludarr</title><style>',CSS,'</style>',poster_manual.PICKER_ASSETS,'<style>body.dashboard-view #activity-heading,body.dashboard-view #poster-recovery,body.dashboard-view #recent-activity,body.dashboard-view #library-search,body.dashboard-view #library-results{display:none!important}body.activity-view main > :not(#activity-heading):not(#poster-recovery):not(#recent-activity){display:none!important}body.activity-view #activity-heading,body.activity-view #poster-recovery,body.activity-view #recent-activity{display:block!important}body.library-view #dashboard,body.library-view #activity-heading,body.library-view #poster-recovery,body.library-view #recent-activity{display:none!important}#library-heading h1{margin:0 0 6px}body.library-view #themearr-poster-queue{display:none!important}.anime-card>a,.anime-card>a:hover{text-decoration:none}</style></head><body class="',{'activity':'activity-view','library':'library-view'}.get(view,'dashboard-view'),'">',full_sidebar(nav_active,fixed=True),'<div class="themearr-content"><main>']
  # Preludarr 1.11 (user 2026-10-29): dashboard = summary + stats; titles and search are on the Library page (a tab per type)
  dash_types=''
  if not view:
   with db() as c:
    per={}  # Preludarr 1.11.9: one card per root folder (same as the Library tabs)
    for r in c.execute('SELECT path,state FROM shows'):
     d=per.setdefault(str(Path(r['path']).parent),{});d[r['state']]=d.get(r['state'],0)+1
    waiting=c.execute("SELECT COUNT(*) FROM theme_queue WHERE state IN ('queued','searching','proposed','approved','installing')").fetchone()[0]
   cards=[]
   for k,rp in ROOTS:
    d=per.get(str(rp),{});tot=sum(d.values());inst=d.get('installed',0);miss=d.get('missing',0)+d.get('review',0)
    pct=round(100*inst/tot) if tot else 0
    cards.append('<a class="stat dash-type" href="'+esc('/?'+urllib.parse.urlencode({'view':'library','library':root_folders.root_value(rp)}),quote=True)+'" title="'+esc(kinds.label(k),quote=True)+'"><span class="dash-type-h">'+kinds.ICONS[k]+' '+esc(root_folders.name_of(rp))+'</span>'
                 '<strong>'+f'{tot:,}'+'</strong>titles<span class="stat-sub">'+f'{inst:,}'+' installed · '+f'{miss:,}'+' missing</span>'
                 '<span class="dash-bar" role="img" aria-label="'+str(pct)+'% have a theme"><span style="width:'+str(pct)+'%"></span></span></a>')
   cards.append('<a class="stat dash-type" href="/queue"><span class="dash-type-h">🧾 Theme Queue</span><strong>'+str(waiting)+'</strong>waiting<span class="stat-sub">to look up, review or install</span></a>')
   dash_types=('<div class="stats dash-types" aria-label="Libraries">'+''.join(cards)+'</div><style>#dashboard .dash-types{margin-top:10px}'
               '#dashboard .dash-type{display:flex;flex-direction:column;min-width:150px;text-decoration:none;color:inherit}#dashboard .dash-type:hover{border-color:#6f8fc7}'
               '#dashboard .dash-type-h{font-size:13px;font-weight:700;color:#9fb0cc;margin-bottom:2px}'
               '#dashboard .dash-bar{display:block;height:5px;margin-top:6px;border-radius:3px;background:#2b3a55;overflow:hidden}#dashboard .dash-bar>span{display:block;height:100%;background:#4f8ff7}'
               '@media(max-width:600px){#dashboard .dash-types{grid-template-columns:repeat(2,minmax(0,1fr))!important}}</style>')
  parts+=['<div class="panel" id="dashboard"><h2>Dashboard</h2><div class="stats"><div class="stat"><strong>',f'{sum(counts.values()):,}','</strong>Total titles</div><div class="stat"><strong>',f"{counts.get('installed',0):,}",'</strong>Installed</div><div class="stat"><strong>',f"{sum(counts.get(k,0) for k in ('missing','review')):,}",'</strong>Missing or needs review<span class="stat-sub">',f"{counts.get('missing',0):,}",' missing · ',f"{counts.get('review',0):,}",' need',('s' if counts.get('review',0)==1 else ''),' review</span></div></div>',dash_types,'<div class="notice" role="status" id="job-status">',esc(status_display()),'</div><p class="muted scan-line" id="scan-line">',esc(scan_line()),'</p><form method="POST" action="/scan"><button>Scan libraries</button></form><p class="auto-line" style="font-size:15px;line-height:1.5;margin:12px 0 0;color:#cfe0f8">',esc(auto_themes.summary()),' · <a href="/media-management#automatic-themes" style="font-weight:600">Change</a></p>',DASHBOARD_CSS,'</div>']
  if view!='library':parts.append(__import__('onboarding').box(sys.modules[__name__]))  # 1.12 #15: next steps on a new install
  parts.append('<section class="panel" id="activity-heading"><h1>Activity</h1><p class="muted">Poster recovery and recent events · <a href="/poster-report">Missing poster report</a></p></section>')
  parts.append(poster_v2_panel())
  parts.append(POSTER_RETRY_SCRIPT)
  if view=='library':
   parts+=['<section class="panel" id="library-heading"><h1>',library_title,'</h1>']
   if lib_tab:  # tabs like Settings → General: Anime TV | Anime Movies | Shows | Movies (only types that have a root folder)
    # switching tabs keeps the search and status filters but always starts at page 1
    keep={k:v for k,v in (('q',term),('state',state),('poster',poster_status)) if v and v!='all'}
    parts+=['<nav class="tm-subnav tm-tabs" aria-label="Library sections" id="library-tabs">']+['<a href="'+esc('/?'+urllib.parse.urlencode({'view':'library','library':root_folders.root_value(r),**keep}),quote=True)+'"'+(' class="on" aria-current="page"' if str(r)==lib_root else '')+' title="'+esc(kinds.label(l)+' · '+str(r),quote=True)+'">'+esc(root_folders.name_of(r))+'</a>' for l,r in ROOTS]+['</nav><style>'
         '#library-tabs{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 14px;padding:8px;border:1px solid #3b4b6b;border-radius:10px;background:#152038;box-sizing:border-box;width:100%}'
         '#library-tabs a{padding:9px 18px;border-radius:8px;color:#cfe0f8;text-decoration:none;font-weight:600;font-size:15px;line-height:1.2}'
         '#library-tabs a:hover{background:#22324f;color:#fff}#library-tabs a.on{background:#2a4a7a;color:#fff;box-shadow:inset 0 0 0 1px #4f6a96}'
         '#library-tabs a:focus-visible{outline:3px solid #8fc1ff;outline-offset:2px}'
         '@media(max-width:600px){#library-tabs{display:grid!important;grid-template-columns:1fr 1fr;gap:6px;padding:6px}#library-tabs a{text-align:center;padding:10px 6px}}</style>']
   parts+=['<p class="muted">',f'{total:,}',' title','' if total==1 else 's',(' in '+esc(root_folders.name_of(lib_root))) if lib_tab else '',' · search and filters below</p></section>']
  # V1.9.8 (user 2026-10-27): Library Search restyled — presentation only. Same forms, field names/values, actions,
  # counts, required Queue checkbox and poster-script hooks (form[action="/poster-filtered-review"], .panel ancestor).
  parts+=['<div class="panel ls-panel" id="library-search"><h2>Library Search</h2>',
   '<form class="filters ls-toolbar',' ls-notype' if lib_tab else '','" method="GET" action="/" role="search" aria-label="Library search">',('<input type="hidden" name="view" value="library">' if view=='library' else ''),
   ('<input type="hidden" name="library" value="'+esc(library,quote=True)+'">' if lib_tab else ''),
   '<div class="ls-field ls-title"><label class="ls-label" for="ls-q">Title</label><input id="ls-q" type="search" name="q" placeholder="Search any series or movie title..." value="',esc(term,quote=True),'" aria-label="Search titles"></div>',
   ]
  if not lib_tab:  # Preludarr 1.11 (user 2026-10-29): on a Library tab the tab already chooses the library
   parts+=['<div class="ls-field"><label class="ls-label" for="ls-library">Library</label><select id="ls-library" name="library" aria-label="Library">']
   for value,label in [('all','All libraries')]+[(root_folders.root_value(r),root_folders.name_of(r)) for _,r in ROOTS]:
    parts+=['<option value="',esc(value,quote=True),'"',(' selected' if library==value else ''),'>',esc(label),'</option>']
   parts+=['</select></div>']
  parts+=['<div class="ls-field"><label class="ls-label" for="ls-state">Theme status</label><select id="ls-state" name="state" aria-label="Theme status">']
  for value,label in [('all','All statuses'),('recent','Recently added'),('missing','Missing themes'),('installed','Installed themes'),('review','Needs review'),('searching','Searching'),('downloading','Downloading'),('converting','Converting')]:
   parts+=['<option value="',value,'"',(' selected' if state==value else ''),'>',esc(label),'</option>']
  parts+=['</select></div><div class="ls-field"><label class="ls-label" for="ls-poster">Poster status</label><select id="ls-poster" name="poster" aria-label="Poster status">']
  for value,label in [('all','All posters'),('missing','Missing posters'),('cached','Posters available'),('failed','Failed / no match')]:
   parts+=['<option value="',value,'"',(' selected' if poster_status==value else ''),'>',esc(label),'</option>']
  parts+=['</select></div><div class="ls-buttons"><button type="submit" class="ls-btn ls-primary">Search</button><a href="',esc('/?'+urllib.parse.urlencode({'view':'library','library':library}) if lib_tab else '/?view=library&state=missing' if view=='library' and state=='missing' else '/',quote=True),'" class="ls-btn ls-quiet">Clear</a></div></form>',
   '<p class="ls-summary">',f'{total:,}',' matching title','' if total==1 else 's',' · Page ',str(page),' of ',str(pages),'</p>',
   # V1.9.14: bulk administration folds away (closed by default) so the results come up; the summary keeps the scope visible.
   '<details class="ls-bulkbox"><summary><span class="ls-bulk-t">Bulk actions</span> <span class="ls-scope">Bulk actions apply to <strong>all ',f'{total:,}',' matching title',('' if total==1 else 's'),'</strong></span></summary>',
   '<div class="ls-bulk" aria-label="Bulk actions"><p class="ls-scope-note">Counts cover every page, not only the ones shown below. Change the filters above to narrow them.</p>']
  parts+=['<form method="POST" action="/poster-filtered-review" class="ls-row">',
   '<input type="hidden" name="q" value="',esc(term,quote=True),'">',
   '<input type="hidden" name="library" value="',esc(library,quote=True),'">',
   '<input type="hidden" name="state" value="',esc(state,quote=True),'">',
   '<input type="hidden" name="poster" value="',esc(poster_status,quote=True),'">',
   '<span class="ls-cat">Posters <span class="ls-count" title="Titles with a missing poster in the current filters">',str(poster_preview_count),'</span></span>',
   '<span class="ls-act"><button type="submit" class="ls-btn ls-outline"',(' disabled' if not poster_preview_count else ''),'>Review ',str(poster_preview_count),' missing poster',('' if poster_preview_count==1 else 's'),' (all pages)</button></span>',
   '<span class="ls-help">Shows the list first; nothing is queued until you confirm. Existing rate limits apply.</span></form>']
  queue_count=len(theme_queue.candidates(sys.modules[__name__],{'q':[term],'library':[library],'state':[state]}))
  parts+=['<div class="ls-rowwrap"><form method="POST" action="/queue/add-filtered" class="ls-row">',
   '<input type="hidden" name="q" value="',esc(term,quote=True),'"><input type="hidden" name="library" value="',esc(library,quote=True),'">',
   '<input type="hidden" name="state" value="',esc(state,quote=True),'">',
   '<span class="ls-cat">Themes <span class="ls-count" title="Missing / needs-review titles in the current filters">',str(queue_count),'</span></span>',
   '<span class="ls-act"><label class="check ls-check" title="Required: confirms the whole filtered set"><input type="checkbox" name="confirm" value="1" required> Confirm all ',str(queue_count),'</label>',
   '<button type="submit" class="ls-btn ls-outline"',(' disabled' if not queue_count else ''),'>Queue themes for ',str(queue_count),' title',('' if queue_count==1 else 's'),'</button></span>',
   '<span class="ls-help">Tick <em>Confirm all ',str(queue_count),'</em>, then Queue. Nothing is installed until you approve it in the Theme Queue.</span></form>',
   '<a href="/queue" id="themearr-theme-queue-link" class="ls-link">View Theme Queue</a></div></div></details>',
   LIBRARY_SEARCH_CSS,'</div>']
  parts.append(poster_filters.modal_script())
  parts+=['<div class="panel" id="library-results"><div class="view-toolbar"><h2>Library Results</h2><label class="poster-size-control">Poster Size <select id="themearr-poster-size" aria-label="Poster Size"><option value="small">Small</option><option value="medium">Medium</option><option value="large">Large</option><option value="extra">Extra Large</option></select></label><div class="view-toggle" id="themearr-view-toggle"><button type="button" data-view="grid">Grid</button><button type="button" data-view="list">List</button></div></div><div class="poster-grid">']
  for r in rows:
   img=poster_file(r['path'])
   theme_url='/themes?'+urllib.parse.urlencode({'path':r['path']})
   poster_url='/poster?'+urllib.parse.urlencode({'path':r['path']})
   poster=('<img class="card-poster" loading="lazy" alt="" src="'+esc(poster_url,quote=True)+'">') if img.is_file() else '<div class="card-empty">No poster yet</div>'
   parts+=['<article class="anime-card"><a href="',esc(theme_url,quote=True),'" aria-label="Explore themes for ',esc(r['name'],quote=True),'">',poster,'</a><div class="card-info"><div class="card-title"><a href="',esc(theme_url,quote=True),'">',esc(r['name']),'</a></div><div class="card-meta">',esc(root_folders.name_of(Path(r['path']).parent)),'</div><div class="card-status ',esc(r['state']),'">',esc(r['state']),'</div>']
   pickable=r['path'] in pick_paths
   if not img.is_file() or pickable:
    parts+=['<div class="poster-tools">']
    if not img.is_file():
     parts+=['<form method="POST" action="/poster-retry">','<input type="hidden" name="path" value="',esc(r['path'],quote=True),'">','<button type="submit">Retry Poster</button></form>']
    if pickable:parts.append(poster_manual.pick_button(r['path'],r['name']))
    parts+=['<p data-poster-retry-status role="status" aria-live="polite"></p></div>']
   if r['state']!='installed':
    parts+=['<form method="POST" action="/retry"><input type="hidden" name="path" value="',esc(r['path'],quote=True),'"><input type="hidden" name="return_to" value="',esc(self.path,quote=True),'"><button',(' disabled' if STATUS['running'] else ''),'>Download / Retry</button></form>']
   parts+=['</div></article>']
  if not rows:parts+=['<p>No matching titles.</p>']
  parts+=['</div>']
  parts+=['<div class="library-list"><table><tr><th>Anime</th><th>Library</th><th>Status</th><th>Action</th></tr>']
  for r in rows:
   img=poster_file(r['path'])
   theme_url='/themes?'+urllib.parse.urlencode({'path':r['path']})
   poster='<img class="poster" loading="lazy" alt="" src="/poster?'+urllib.parse.urlencode({'path':r['path']})+'">' if img.is_file() else '<span class="poster poster-empty">No poster</span>'
   parts+=['<tr><td><div class="anime-cell"><a href="',esc(theme_url,quote=True),'" aria-label="Explore themes for ',esc(r['name'],quote=True),'">',poster,'</a><div class="anime-info"><a href="',esc(theme_url,quote=True),'">',esc(r['name']),'</a><br><small>',esc(r['detail'] or ''),'</small></div></div></td><td>',esc(r['library']),'</td><td class="',esc(r['state']),'">',esc(r['state']),'</td><td>']
   if r['state']!='installed':parts+=['<form method="POST" action="/retry"><input type="hidden" name="path" value="',esc(r['path'],quote=True),'"><input type="hidden" name="return_to" value="',esc(self.path,quote=True),'"><button',(' disabled' if STATUS['running'] else ''),'>',('Working…' if r['state'] in ('searching','downloading','converting') else 'Download / Retry'),'</button></form>']
   parts+=['</td></tr>']
  if not rows:parts+=['<tr><td colspan="4">No matching titles. Try another search or scan your libraries.</td></tr>']
  parts+=['</table></div><div class="pager">']
  if page>1:parts+=['<a href="',esc(url(page-1),quote=True),'">← Previous</a>']
  parts+=['<span>Page ',str(page),' / ',str(pages),'</span>']
  if page<pages:parts+=['<a href="',esc(url(page+1),quote=True),'">Next →</a>']
  parts+=['</div></div><div class="panel" id="recent-activity"><h2>Recent activity</h2><style>#recent-activity td:first-child,#recent-activity td:nth-child(3){white-space:nowrap}</style><table>']
  for e in events:parts+=['<tr><td title="',esc(e['at'],quote=True),'">',esc(activity_time(e['at'])),'</td><td>',esc(e['name']),'</td><td>',esc(e['status']),'</td><td>',esc(e['detail']),'</td></tr>']
  parts+=['</table></div><script>let wasRunning=',('true' if STATUS['running'] else 'false'),';let pollTimer=0;async function poll(){if(document.hidden)return;try{const r=await fetch("/api/status",{cache:"no-store"});if((r.status===401||(r.redirected&&new URL(r.url).pathname==="/login"))){clearInterval(pollTimer);const se=document.getElementById("job-status");if(se)se.textContent="You were signed out — reload the page to sign in again.";return}if(!r.ok)return;const s=await r.json();const el=document.getElementById("job-status");if(el)el.textContent=s.display||(s.message+(s.running?" (in progress…)":""));const sl=document.getElementById("scan-line");if(sl&&s.scan_line)sl.textContent=s.scan_line;if(wasRunning&&!s.running){location.reload();return;}wasRunning=s.running;}catch(e){}}pollTimer=setInterval(poll,2000);document.addEventListener("visibilitychange",()=>{if(!document.hidden&&pollTimer)poll()});const buttons=document.querySelectorAll("#themearr-view-toggle button");function setView(v){v=v==="list"?"list":"grid";document.body.classList.remove("view-grid","view-list");document.body.classList.add("view-"+v);buttons.forEach(b=>b.classList.toggle("active",b.dataset.view===v));try{localStorage.setItem("themearr-library-view",v)}catch(e){}}let saved="grid";try{saved=localStorage.getItem("themearr-library-view")||"grid"}catch(e){}setView(saved);buttons.forEach(b=>b.addEventListener("click",()=>setView(b.dataset.view)));const posterSize=document.getElementById("themearr-poster-size");const posterSizes=["small","medium","large","extra"];/* Preludarr 1.11.1 (user 2026-10-29): phones start with Small posters. A size is remembered only when the user picks one *//* (preludarr-poster-size, older splicarr-poster-size still read); the themearr key was written on every visit, so it is only the desktop fallback. */function setPosterSize(v){v=posterSizes.includes(v)?v:"large";document.body.classList.remove(...posterSizes.map(x=>"poster-"+x));document.body.classList.add("poster-"+v);posterSize.value=v}let savedPosterSize=null;try{savedPosterSize=localStorage.getItem("preludarr-poster-size")||localStorage.getItem("splicarr-poster-size")}catch(e){}if(!posterSizes.includes(savedPosterSize)){let legacy=null;try{legacy=localStorage.getItem("themearr-poster-size")}catch(e){}savedPosterSize=matchMedia("(max-width:600px)").matches?"small":(legacy||"large")}setPosterSize(savedPosterSize);posterSize.addEventListener("change",()=>{setPosterSize(posterSize.value);try{localStorage.setItem("preludarr-poster-size",posterSize.value)}catch(e){}});</script></main></div></body></html>'];self.send(200,''.join(parts))
 def do_POST(self):
  if not auth.post(self):return
  if self.path=='/api/v1/command/scan':  # V1.9.7 API: start a library scan (X-Api-Key header or signed-in)
   started=job(False)
   return self.send(202 if started else 409,json.dumps({'started':bool(started),'message':STATUS['message']}),'application/json')
  if self.path.split('?',1)[0] in ('/system/backup/create','/system/backup/prepare','/system/backup/upload','/system/backup/schedule','/system/backup/download','/system/backup/cancel'):
   return system.post_route(self,CONFIG)
  if self.path=='/settings/appearance':  # 1.12 Settings → Appearance
   return appearance.post(sys.modules[__name__],self,auth.redirect)
  if self.path=='/collections/upload':  # 1.12 Collections: your own audio
   return movie_collections.post_upload(sys.modules[__name__],self,auth.redirect)
  if self.path in ('/collections/refresh','/collections/install','/collections/remove','/collections/folder','/collections/youtube','/collections/search'):  # 1.12 Collections
   return movie_collections.post(sys.modules[__name__],self,auth.redirect)
  if self.path in ('/queue/action','/queue/add-filtered'):
   return theme_queue.post(sys.modules[__name__],self,auth.redirect)
  if self.path=='/download-clients/sources':
   return sources.post(CONFIG,self,auth.redirect)
  if self.path in ('/system/updates/check','/system/updates/settings'):  # Preludarr 1.12: update-available check
   return __import__('updates').post(self,auth.redirect,APP_VERSION)
  if self.path=='/download-clients/youtube-search':  # Preludarr 1.11
   return __import__('youtube_search').post(self,auth.redirect)
  if self.path=='/download-clients/youtube-search-test':  # Preludarr 1.11.3: test the API key (never saves it)
   return __import__('youtube_search').post_test(self,auth.redirect)
  if self.path=='/regular/suggest' or self.path=='/regular/install':  # Preludarr 1.11 Shows / Movies
   return __import__('shows_movies').post(sys.modules[__name__],self,auth.redirect)
  if self.path=='/download-clients/limits':  # V1.9.14 per-source rate limits
   return __import__('download_clients').limits_post(self,auth.redirect)
  if self.path=='/connect/arr':
   return arr.post(sys.modules[__name__],self,auth.redirect)
  if self.path=='/connect/arr-create':
   return arr.create_post(sys.modules[__name__],self,auth.redirect)
  if self.path in ('/library-health/keep','/library-health/trash'):
   return theme_review.post(sys.modules[__name__],self,auth.redirect)
  if self.path=='/library-health/scan':
   return library_health.post(sys.modules[__name__],self,auth.redirect)
  if self.path in ('/media-management/permissions','/media-management/permissions-preview','/media-management/permissions-fix'):
   return permissions.post(sys.modules[__name__],self,auth.redirect)
  if self.path=='/media-management/match':
   return unmapped.post_match(sys.modules[__name__],self,auth.redirect)
  if self.path=='/media-management/auto-run':
   return auto_themes.run_now(sys.modules[__name__],self,auth.redirect)
  if self.path=='/media-management/auto':
   return auto_themes.post(sys.modules[__name__],self,auth.redirect)
  if self.path.startswith('/media-management/'):
   return root_folders.post(sys.modules[__name__],self,auth.redirect)
  if self.path=='/anidb-settings':
   return anidb.settings_post(self,CONFIG,auth.redirect)
  if self.path=='/poster-connect-action':
   return poster_connect.post(self,CONFIG)
  if self.path in ('/poster-v2-pause','/poster-v2-resume','/poster-v2-retry-failed'):
   return poster_v2_control_post(self,self.path.removeprefix('/poster-v2-'))
  if self.path in ('/v4-save-match','/v4-remove-match'):
   return media_servers.save_override(self,CONFIG,ROOTS,db,request_json,auth.redirect,remove=self.path=='/v4-remove-match')
  if self.path=='/media-servers/emby-signin':  # 1.12: sign in to Emby
   return emby_signin.post(self,CONFIG,auth.redirect)
  if self.path=='/media-servers/jellyfin-qc/start':  # 1.12: Jellyfin Quick Connect
   return jellyfin_quickconnect.post(self,CONFIG)
  if self.path in ('/media-servers/plex-signin/start','/media-servers/plex-signin/choose','/media-servers/plex-extra/remove'):  # 1.12: Sign in with Plex
   return plex_signin.post(self,CONFIG,auth.redirect)
  if self.path in ('/media-server-save','/media-server-map','/media-server-test'):
   return media_servers.server_post(self,CONFIG,ROOTS,db,auth.redirect)
  if self.path=='/poster-retry':
   return poster_v1_retry_single(self)
  if self.path in ('/poster-choose','/poster-search','/poster-reset','/poster-upload','/poster-upload-confirm','/poster-upload-cancel'):
   return poster_manual_post(self)
  if self.path=='/poster-filtered-review':
   return poster_filters.review(self,db,poster_file)
  if self.path=='/poster-filtered-confirm':
   return poster_filters.outcome(self,db,poster_file,poster_v2_paused,poster_filtered_submit)
  if self.path=='/poster-retry-missing':
   return poster_v1_retry_missing(self)
  if self.path=='/review-youtube-theme':
   return youtube_manual.review(self,CONFIG,ROOTS,db,issue_theme_confirmation)
  if self.path=='/confirm-youtube-theme':
   return youtube_manual.confirm(self,CONFIG,ROOTS,db,take_theme_confirmation,manual_install_mp3)
  if self.path=='/cancel-youtube-theme':
   return youtube_manual.cancel(self,CONFIG)
  if self.path=='/review-custom-upload':
   return custom_upload.review(self,CONFIG,ROOTS,db,issue_theme_confirmation,CSS)
  if self.path=='/confirm-custom-upload':
   return custom_upload.confirm(self,CONFIG,ROOTS,db,take_theme_confirmation,manual_install_mp3,CSS)
  if self.path=='/cancel-custom-upload':
   return custom_upload.cancel(self,CONFIG)
  size=int(self.headers.get('Content-Length','0'))
  if size>8192:return self.send(413,'Too large','text/plain')
  form=urllib.parse.parse_qs(self.rfile.read(size).decode())
  if self.path in ('/confirm-remove-theme','/confirm-restore-theme'):
   operation='remove' if self.path=='/confirm-remove-theme' else 'restore'
   return theme_management.perform_action(self,ROOTS,db,open_safe_theme_directory,verify_theme_directory_identity,_manual_install_lock,auth.token(self),form,operation,auth.redirect,event)
  if self.path=='/install-theme':
   selected=form.get('path',[''])[0]
   anime_slug=form.get('anime_slug',[''])[0]
   video_id=form.get('video_id',[''])[0]
   with db() as c:
    row=c.execute('SELECT name FROM shows WHERE path=?',(selected,)).fetchone()
   folder=Path(selected)
   allowed=any(folder.parent==root and root.is_dir() and not root.is_symlink() for _,root in ROOTS)
   if not row or not allowed or not folder.is_dir() or folder.is_symlink():
    return self.send(400,'Invalid anime destination','text/plain')
   if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',anime_slug):
    return self.send(400,'Invalid AnimeThemes selection','text/plain')
   if not re.fullmatch(r'[0-9]{1,20}',video_id):
    return self.send(400,'Invalid track selection','text/plain')
   target=folder/'theme.mp3'
   if target.is_symlink():
    return self.send(400,'Unsafe theme destination','text/plain')
   existing=target.exists()
   try:
    token=issue_theme_confirmation(folder,anime_slug,video_id)
   except (OSError,ValueError):
    return self.send(409,'Theme state could not be verified','text/plain')
   esc=html.escape
   page=[
    '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">',
    '<title>Review Theme Installation</title><style>',CSS,'</style></head><body>',
    '<main><div class="panel"><h2>Review Theme Installation</h2>',
    '<p><strong>Anime:</strong> ',esc(row['name']),'</p>',
    '<p><strong>AnimeThemes entry:</strong> ',esc(anime_slug),'</p>',
    '<p><strong>Track ID:</strong> ',esc(video_id),'</p>',
    '<p><strong>Destination:</strong> theme.mp3</p>',
    '<p><strong>Current status:</strong> ',
    'Existing theme detected — replacement requires confirmation.' if existing else 'No existing theme detected.',
    '</p>',
    '<p>Confirming will download the selected AnimeThemes track and install it as theme.mp3. An existing theme.mp3 will be replaced.</p>',
    '<form method="post" action="/confirm-theme">',
    '<input type="hidden" name="path" value="',esc(selected,quote=True),'">',
    '<input type="hidden" name="anime_slug" value="',esc(anime_slug,quote=True),'">',
    '<input type="hidden" name="video_id" value="',esc(video_id,quote=True),'">',
    '<input type="hidden" name="token" value="',esc(token,quote=True),'">',
    '<button type="submit">Confirm and Install Theme</button>',
    '</form>',
    '<a href="/themes?',urllib.parse.urlencode({'path':selected}),'">Cancel and return</a>',
    '</div></main></body></html>'
   ]
   return self.send(200,''.join(page),'text/html')
  if self.path=='/confirm-theme':
   # 1.12 #15 fresh-install test: AnimeThemes installs from the Theme Browser always work for a signed-in user (CSRF + the review
   # token below protect them). PRELUDARR_MANUAL_INSTALL_ENABLED only governs your own uploads and YouTube links, as documented.
   selected=form.get('path',[''])[0]
   anime_slug=form.get('anime_slug',[''])[0]
   video_id=form.get('video_id',[''])[0]
   token=form.get('token',[''])[0]
   folder=Path(selected)
   allowed=any(folder.parent==root and root.is_dir() and not root.is_symlink() for _,root in ROOTS)
   if not allowed or not folder.is_dir() or folder.is_symlink():
    return self.send(400,'Invalid anime destination','text/plain')
   with db() as c:
    known=c.execute('SELECT path FROM shows WHERE path=?',(selected,)).fetchone()
   if not known:
    return self.send(400,'Unknown anime destination','text/plain')
   fingerprint=take_theme_confirmation(token,folder,anime_slug,video_id)
   if fingerprint is None:
    return self.send(409,'Confirmation expired, used, or theme changed','text/plain')
   stream_progress=self.headers.get('X-Themearr-Progress')=='v2'
   if stream_progress:
    self.send_response(200)
    self.send_header('Content-Type','application/x-ndjson; charset=utf-8')
    self.send_header('Cache-Control','no-store, no-cache, max-age=0')
    self.send_header('X-Accel-Buffering','no')
    self.send_header('X-Content-Type-Options','nosniff')
    self.send_header('Connection','close')
    self.end_headers()
    self.close_connection=True
   def report(phase,message):
    if not stream_progress:return
    data=(json.dumps({'phase':phase,'message':message},separators=(',',':'))+'\n').encode('utf-8')
    try:
     self.wfile.write(data)
     self.wfile.flush()
    except (BrokenPipeError,ConnectionResetError,OSError):
     pass
   report('verifying','Verifying selected AnimeThemes track')
   try:
    track=resolve_animethemes_track(anime_slug,video_id)
    report('downloading','Downloading verified audio')
    with stage_verified_theme_audio(track,CONFIG) as audio:
     report('converting','Converting audio to MP3')
     installed=manual_install_mp3(
      folder,audio,
      replace=True,
      expected_fingerprint=fingerprint,
      anime_slug=anime_slug,
      video_id=video_id,
      progress=report if stream_progress else None,
      activity_label=' — '.join(str(x) for x in (track.get('slug'),track.get('title')) if x)
     )
   except (ValueError,OSError,TimeoutError,subprocess.CalledProcessError,subprocess.TimeoutExpired) as error:
    if stream_progress:
     report('error','Theme installation failed. Refresh the page to verify whether theme.mp3 changed before retrying.')
     return
    return self.send(409,'Theme installation failed: '+str(error),'text/plain')
   with db() as c:
    c.execute(
     "UPDATE shows SET state=?,detail=?,updated=? WHERE path=?",
     ('installed','Manual AnimeThemes installation',now(),selected)
    )
   if stream_progress:
    report('complete','Installation complete. theme.mp3 is ready.')
    return
   return auth.redirect(self,'/themes?'+urllib.parse.urlencode({'path':selected,'installed':'1'}))
  if self.path=='/scan':ok=job(False)
  elif self.path=='/retry':
   selected=form.get('path',[''])[0]
   with db() as c: known=c.execute('SELECT path FROM shows WHERE path=?',(selected,)).fetchone()
   if not known:return self.send(400,'Unknown library item','text/plain')
   ok=job(one=selected)
  else:return self.send(404,'Not found','text/plain')
  if not ok:return self.send(409,'Another scan or download is already running. Try again shortly.','text/plain')
  destination=form.get('return_to',['/'])[0]
  if not destination.startswith('/') or destination.startswith('//') or '\r' in destination or '\n' in destination:destination='/'
  self.send_response(303);self.send_header('Location',destination);self.end_headers()

def scheduler():
 # V1.7.3: interval and automatic installs follow Settings → Media Management → Automatic Themes.
 job(download=auto_themes.settings()['mode']!='off')
 while True:
  started=time.monotonic()
  while time.monotonic()-started<auto_themes.interval_seconds():time.sleep(60)
  job(download=auto_themes.settings()['mode']!='off')


def _limit_theme_ffmpeg_memory():
 """Restrict FFmpeg child virtual address space to 1 GiB."""
 import resource
 limit=1024*1024*1024
 resource.setrlimit(resource.RLIMIT_AS,(limit,limit))

@contextlib.contextmanager
def open_safe_theme_directory(folder):
 """Open a registered anime directory without following symlinks."""
 import stat

 folder=Path(folder)

 matching=[
  root for _,root in ROOTS
  if folder.parent==root
 ]

 if len(matching)!=1:
  raise ValueError('Destination is outside allowed libraries')

 root=matching[0]

 root_fd=os.open(
  root,
  os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
 )

 try:
  folder_fd=os.open(
   folder.name,
   os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,
   dir_fd=root_fd
  )

  try:
   root_stat=os.fstat(root_fd)
   folder_stat=os.fstat(folder_fd)

   if not stat.S_ISDIR(root_stat.st_mode):
    raise ValueError('Library root is not a directory')

   if not stat.S_ISDIR(folder_stat.st_mode):
    raise ValueError('Anime destination is not a directory')

   with db() as conn:
    known=conn.execute(
     'SELECT path FROM shows WHERE path=?',
     (str(folder),)
    ).fetchone()

   if not known:
    raise ValueError('Anime destination is not registered')

   current=os.stat(
    folder.name,
    dir_fd=root_fd,
    follow_symlinks=False
   )

   if (
    current.st_dev!=folder_stat.st_dev
    or current.st_ino!=folder_stat.st_ino
   ):
    raise ValueError('Anime destination changed during validation')

   yield folder_fd

  finally:
   os.close(folder_fd)

 finally:
  os.close(root_fd)

def verify_theme_directory_identity(folder, folder_fd):
 """Reject a destination that no longer matches its opened directory."""
 import stat

 folder=Path(folder)

 if not isinstance(folder_fd,int) or folder_fd<0:
  raise ValueError('Invalid destination directory descriptor')

 opened=os.fstat(folder_fd)

 if not stat.S_ISDIR(opened.st_mode):
  raise ValueError('Destination descriptor is not a directory')

 matching=[
  root for _,root in ROOTS
  if folder.parent==root
 ]

 if len(matching)!=1:
  raise ValueError('Destination is outside allowed libraries')

 root=matching[0]

 root_fd=os.open(
  root,
  os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
 )

 try:
  current=os.stat(
   folder.name,
   dir_fd=root_fd,
   follow_symlinks=False
  )

  if (
   not stat.S_ISDIR(current.st_mode)
   or current.st_dev!=opened.st_dev
   or current.st_ino!=opened.st_ino
  ):
   raise ValueError('Anime destination changed; installation cancelled')

 finally:
  os.close(root_fd)

 return True

if __name__=='__main__':
 db().close();auto_themes.init(sys.modules[__name__]);theme_queue.wake(sys.modules[__name__]);threading.Thread(target=scheduler,daemon=True).start()
 __import__('youtube_search').start_background(sys.modules[__name__])  # Preludarr 1.11: idles unless background search is on
 __import__('updates').start_background(APP_VERSION)  # Preludarr 1.12: once a day, can be turned off
 if __import__('general_settings').migrate_api_scan(EXISTING_INSTALL):event('API','info','Kept "API key may start library scans" for this existing install (new installs are read-only since 1.12). Change it in Settings → General.')
 if auth.needs_setup():auth.ensure_setup_code()  # 1.12 first run: the setup code is in the container log from the start
 theme_review.start(sys.modules[__name__])  # V1.9.4: theme trash age limit + cron schedule
 auto_backup.start(sys.modules[__name__])  # V1.9.13: automatic backups (every N days, retention)
 if POSTER_BACKGROUND_ENABLED:
  threading.Thread(
   target=background_poster_worker,
   daemon=True
  ).start()
 print(f'Preludarr listening on {PORT}, auto-download={AUTO}',flush=True)
 httpserver.Server(('0.0.0.0',PORT),Handler).serve_forever()
