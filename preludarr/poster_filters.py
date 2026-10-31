# SPDX-License-Identifier: GPL-3.0-or-later
"""Scoped poster search and two-step retry confirmation for Themearr.

No downloader threads created here: approved paths are persisted in the existing
poster retry table and processed by the unchanged V2 rate-limited worker.
"""
import hashlib
import json
import html
import secrets
import threading
import time
import urllib.parse

_ALLOWED_LIBRARIES = ('all', 'Anime TV', 'Anime Movies', 'Shows', 'Movies')  # Preludarr 1.11
_ALLOWED_STATES = ('all', 'recent', 'missing', 'installed', 'review', 'searching', 'downloading', 'converting')
_ALLOWED_POSTERS = ('all', 'missing', 'cached', 'failed')
_PEND = {}
_PEND_LOCK = threading.Lock()
_PENDING_SECONDS = 300
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_SECONDS = 12 * 3600


def _log(event, **values):
    # Do not log titles, API URLs, saved keys, PINs, or full filesystem paths.
    allowed = {'query_id', 'library', 'theme', 'poster', 'matched', 'queued',
               'skipped', 'count', 'reason', 'provider', 'poster_id', 'status'}
    safe = [(key, str(value).replace(' ', '_')[:90]) for key, value in values.items() if key in allowed]
    print('POSTER_FILTERS_V1_4 event=' + event + ''.join(' %s=%s' % entry for entry in safe), flush=True)


def selection(q='', library='all', state='all', poster='all'):
    if not isinstance(q, str) or len(q) > 120 or any(ord(c) < 32 for c in q):
        raise ValueError('Invalid title search')
    rf_ok = __import__('root_folders').valid_scope(library)  # Preludarr 1.11.9: 'root:<path>' tabs
    if not (library in _ALLOWED_LIBRARIES or rf_ok) or state not in _ALLOWED_STATES or poster not in _ALLOWED_POSTERS:
        raise ValueError('Invalid library, theme or poster filter')
    return {'q': q.strip(), 'library': library, 'state': state, 'poster': poster}


def from_query(query):
    values = {}
    for key, default in [('q',''),('library','all'),('state','all'),('poster','all')]:
        item = query.get(key, [default])
        if len(item) != 1:
            raise ValueError('Duplicate search filter')
        values[key] = item[0]
    return selection(**values)


def filter_clause(poster):
    if poster == 'missing':
        return 'themearr_poster_exists(path)=0'
    if poster == 'cached':
        return 'themearr_poster_exists(path)=1'
    if poster == 'failed':
        return "themearr_poster_exists(path)=0 AND EXISTS (SELECT 1 FROM posters p WHERE p.path=shows.path AND p.status IN ('error','none'))"
    if poster == 'all':
        return None
    raise ValueError('Invalid poster filter')


def register(conn, poster_file):
    conn.create_function('themearr_poster_exists', 1,
                         lambda path: int(bool(path and poster_file(path).is_file())))


def candidates(conn, filters, poster_file):
    """Return every currently matching missing title, not just one page."""
    term = filters['q']; library = filters['library']; state = filters['state']; poster = filters['poster']
    clauses = []; params = []
    if term:
        escaped = term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        clauses.append("name LIKE ? ESCAPE '\\'")
        params.append('%' + escaped + '%')
    if library != 'all':
        sql, extra = __import__('root_folders').scope_clause(library)
        if sql:
            clauses.append(sql); params += extra
    if state == 'recent':  # V1.7.3 'Recently added'
        import auto_themes
        sql, extra = auto_themes.recent_clause()
        clauses.append(sql); params.extend(extra)
    elif state != 'all':
        clauses.append('state=?'); params.append(state)
    extra = filter_clause(poster)
    if extra:
        clauses.append('(' + extra + ')')
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    register(conn, poster_file)
    paths = [row[0] for row in conn.execute('SELECT path FROM shows' + where + ' ORDER BY path', params)]
    return [path for path in paths if not poster_file(path).is_file()]


def query_id(filters):
    canonical = '\x1f'.join(filters[key] for key in ('q','library','state','poster'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]


def _parse_form(handler, expected):
    ctype = handler.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    if ctype != 'application/x-www-form-urlencoded':
        raise ValueError('Expected form submission')
    try:
        length = int(handler.headers.get('Content-Length', '0'))
    except (ValueError, TypeError):
        raise ValueError('Invalid form length') from None
    if length < 1 or length > 2048:
        raise ValueError('Invalid form length')
    body = handler.rfile.read(length).decode('utf-8', 'strict')
    parsed = urllib.parse.parse_qs(body, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
    if set(parsed) != set(expected) or any(len(v)!=1 for v in parsed.values()):
        raise ValueError('Invalid form fields')
    return {k: v[0] for k, v in parsed.items()}


def _page(handler, content, status=200):
    return handler.send(status, content, 'text/html; charset=utf-8')


def review(handler, db, poster_file):
    try:
        filters = selection(**_parse_form(handler, ('q','library','state','poster')))
        with db() as conn:
            paths = candidates(conn, filters, poster_file)
    except (ValueError, UnicodeError) as exc:
        _log('preview_rejected', reason=type(exc).__name__)
        return handler.send(400, 'Invalid poster filter request', 'text/plain')
    count = len(paths)
    ident = query_id(filters)
    _log('preview', query_id=ident, library=filters['library'], theme=filters['state'], poster=filters['poster'], matched=count)
    if count == 0:
        if handler.headers.get("X-Themearr-Modal") == "1":
            return handler.send(200, json.dumps({"count": 0, "details": "No matching missing posters"}), "application/json")
        return _page(handler, '<!doctype html><html><body style="background:#19253a;color:white;font-family:Arial;padding:2rem"><h1>No missing posters match your filters</h1><p>No changes were made.</p><a style="color:#afceff" href="/">Return to Library Search</a></body></html>')
    token = secrets.token_urlsafe(32)
    with _PEND_LOCK:
        now=time.monotonic()
        for key, value in list(_PEND.items()):
            if now - value['issued'] > _PENDING_SECONDS:
                del _PEND[key]
        _PEND[token]={'issued':now,'filters':filters,'paths':tuple(paths)}
    name=lambda value: html.escape(str(value),quote=True)
    detail=' · '.join([f"Search: {filters['q'] or 'Any title'}", f"Library: {filters['library']}",
                       f"Theme status: {filters['state']}", f"Poster status: {filters['poster']}"])
    page = '''<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Confirm poster retry | Preludarr</title><style>body{background:#111c30;color:#f5f8ff;font:16px system-ui;margin:0;padding:40px 20px}.panel{max-width:740px;margin:auto;background:#1d2a42;border:1px solid #476186;border-radius:14px;padding:30px}p{line-height:1.5}button,.back{display:inline-block;background:#3867e7;color:white;border:0;border-radius:7px;padding:12px 18px;text-decoration:none;font-weight:700;cursor:pointer}.back{background:#44556c}small{color:#bfcde8}</style></head><body><main class="panel"><h1>Review filtered poster retry</h1><p><strong>%s missing posters</strong> match your filters across all pages.</p><p><small>%s</small></p><p>This only queues missing artwork. Downloading remains subject to Poster Recovery's existing rate limits; cached artwork and theme files will not be changed.</p><form method="post" action="/poster-filtered-confirm"><input type="hidden" name="token" value="%s"><button type="submit">Confirm: Queue %s missing posters</button></form><p><a class="back" href="/">Cancel — no changes</a></p></main></body></html>''' % (name(count),name(detail),name(token),name(count))
    if handler.headers.get("X-Themearr-Modal") == "1":
        return handler.send(200, json.dumps({"count": count, "details": detail, "token": token}), "application/json")
    return _page(handler,page)


def take(handler):
    try:
        posted = _parse_form(handler, ('token',))
    except (ValueError,UnicodeError):
        return None
    token=posted['token']
    if len(token)>128 or not token:
        return None
    with _PEND_LOCK:
        item=_PEND.pop(token,None)  # one use, including on downstream error
    if item is None or time.monotonic()-item['issued'] > _PENDING_SECONDS:
        return None
    return item


def outcome(handler, db, poster_file, paused, submit):
    """submit(paths) must enforce the app's shared 15-min bulk gate atomically."""
    item=take(handler)
    if item is None:
        _log('confirm_rejected',reason='expired_or_invalid')
        return handler.send(400,'Poster confirmation expired or already used. Review again.','text/plain')
    f=item['filters']; ref=query_id(f)
    if paused():
        _log('confirm_rejected',query_id=ref,reason='recovery_paused')
        return handler.send(409,'Poster Recovery is paused. Resume before queuing.','text/plain')
    with db() as conn:
        current=set(candidates(conn,f,poster_file))
    originally=item['paths']
    safe_paths=[path for path in originally if path in current and not poster_file(path).is_file()]
    skipped=len(originally)-len(safe_paths)
    if not safe_paths:
        _log('confirm_skipped',query_id=ref,matched=len(originally),skipped=skipped,reason='already_cached_or_changed')
        return handler.send(409,'No matching missing posters remain. No changes made.','text/plain')
    result,queued,queued_paths=submit(safe_paths)
    if result != 'ok':
        _log('confirm_rejected',query_id=ref,reason=result,matched=len(originally))
        code=429 if result == 'rate_limit' else 409
        return handler.send(code,'Retry not queued: '+ ('bulk retry is limited to once every 15 minutes' if result=='rate_limit' else result), 'text/plain')
    _log('bulk_queued',query_id=ref,library=f['library'],theme=f['state'],poster=f['poster'],matched=len(originally),queued=queued,skipped=len(originally)-queued)
    job_id = secrets.token_urlsafe(24)
    created = int(time.time())
    # A durable, narrowly-scoped ledger in the already-persisted library database.
    # Poster/queue workers remain independent and preserve existing retry limits.
    tracked = True
    try:
        with db() as conn:
            _prepare_jobs(conn)
            conn.execute(
                'INSERT INTO themearr_poster_batches_v143 '
                '(job,created,matched,queued,skipped,filter_id,library,theme,poster) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (job_id,created,len(originally),queued,len(originally)-queued,ref,
                 f['library'],f['state'],f['poster']))
            conn.executemany(
                'INSERT INTO themearr_poster_batch_paths_v143 (job,path) VALUES (?,?)',
                ((job_id,path) for path in queued_paths))
            # Keep the latest 100 requests; remove only V1.4.3's own old tracking.
            old = [r[0] for r in conn.execute(
                'SELECT job FROM themearr_poster_batches_v143 '
                'ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET 100')]
            if old:
                conn.executemany('DELETE FROM themearr_poster_batch_paths_v143 WHERE job=?',
                                 ((name,) for name in old))
                conn.executemany('DELETE FROM themearr_poster_batches_v143 WHERE job=?',
                                 ((name,) for name in old))
    except Exception as exc:
        tracked = False
        _log('history_unavailable',reason=type(exc).__name__)
    with _JOBS_LOCK:
        now = time.monotonic()
        for key, value in list(_JOBS.items()):
            if now - value['issued'] > _JOB_SECONDS:
                del _JOBS[key]
        _JOBS[job_id] = {'issued': now, 'created': created, 'paths': tuple(queued_paths),
                         'queued': queued, 'skipped': len(originally)-queued,
                         'matched': len(originally), 'filter_id': ref,
                         'library': f['library'], 'theme': f['state'],
                         'poster': f['poster']}
    _log('job_created',query_id=ref,queued=queued,status=('persisted' if tracked else 'memory_only'))
    if handler.headers.get('X-Themearr-Modal') == '1':
        return handler.send(200, json.dumps({'queued': queued, 'skipped': len(originally) - queued, 'job': job_id, 'tracked': tracked}), 'application/json')
    return handler.send(200,'''<!doctype html><html><body style="background:#16243c;color:#eaf1ff;font-family:Arial;padding:2rem"><h1>Poster retry queued</h1><p><strong>%d</strong> missing posters queued for the existing rate-limited Poster Recovery worker.</p><p>%d skipped because their status or cache changed.</p><p><a style="color:#b7d7ff" href="/?view=activity#poster-recovery">View Poster Recovery</a></p></body></html>'''%(queued,len(originally)-queued),'text/html; charset=utf-8')


def log_result(path, status, provider='', reason=None):
    ident=hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:12]
    _log('poster_result',poster_id=ident,status=status,provider=provider or 'none',reason=reason or 'none')


def _prepare_jobs(conn):
    # Tables hold tracking metadata only; no modifications to shows/posters schemas.
    conn.execute('''CREATE TABLE IF NOT EXISTS themearr_poster_batches_v143 (
       job TEXT PRIMARY KEY, created INTEGER NOT NULL, matched INTEGER NOT NULL,
       queued INTEGER NOT NULL, skipped INTEGER NOT NULL, filter_id TEXT NOT NULL,
       library TEXT NOT NULL, theme TEXT NOT NULL, poster TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS themearr_poster_batch_paths_v143 (
       job TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(job,path))''')
    conn.execute('CREATE INDEX IF NOT EXISTS poster_batch_paths_job_v143 '
                 'ON themearr_poster_batch_paths_v143(job)')


def _has_job_tables(conn):
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='themearr_poster_batches_v143'").fetchone()
    return bool(row)


def _job_record(job, conn):
    if _has_job_tables(conn):
        row=conn.execute('SELECT * FROM themearr_poster_batches_v143 WHERE job=?',
                         (job,)).fetchone()
        if row is not None:
            paths=[r[0] for r in conn.execute(
                'SELECT path FROM themearr_poster_batch_paths_v143 WHERE job=?',(job,))]
            return {'job':job, 'created':row['created'], 'matched':row['matched'],
                    'queued':row['queued'],'skipped':row['skipped'],
                    'library':row['library'],'theme':row['theme'],'poster':row['poster'],
                    'paths':tuple(paths), 'tracked':True}
    with _JOBS_LOCK:
        item=_JOBS.get(job)
        if item is None or time.monotonic()-item['issued']>_JOB_SECONDS:
            return None
        return dict(item,job=job,tracked=False)


def _summary(record, poster_file, recorded, busy, paused):
    counts={'waiting':0,'processing':0,'completed':0,'no_match':0,'failed':0}
    for path in record['paths']:
        if poster_file(path).is_file():
            counts['completed']+=1
        elif path in busy:
            counts['processing']+=1
        elif recorded.get(path)=='none':
            counts['no_match']+=1
        elif recorded.get(path)=='error':
            counts['failed']+=1
        else:
            counts['waiting']+=1
    counts['total']=len(record['paths'])
    counts['queued']=counts['waiting']  # old V1.4.2 client compatibility
    counts['error']=counts['failed']
    counts['paused']=bool(paused)
    counts['finished']=not counts['waiting'] and not counts['processing']
    counts['job']=record['job']
    counts['created']=record['created']
    counts['matched']=record.get('matched',counts['total'])
    counts['submitted']=record.get('queued',counts['total'])
    counts['skipped']=record.get('skipped',0)
    counts['library']=record.get('library','all')
    counts['theme']=record.get('theme','all')
    counts['poster']=record.get('poster','all')
    counts['tracked']=record['tracked']
    return counts


def status(handler, db, poster_file, query, busy_set, busy_lock, paused):
    """Read-only authenticated job status and server-persisted recent batch history."""
    values=urllib.parse.parse_qs(query,keep_blank_values=True)
    recent = set(values)=={'recent'} and values['recent']==['1']
    specific = (set(values)=={'job'} and len(values['job'])==1
                and bool(values['job'][0]) and len(values['job'][0])<=128)
    if not recent and not specific:
        return handler.send(400,'Invalid poster queue status request','text/plain')
    try:
        with db() as conn:
            if recent:
                records=[]
                if _has_job_tables(conn):
                    for row in conn.execute('SELECT job FROM themearr_poster_batches_v143 '
                                            'ORDER BY created DESC, rowid DESC LIMIT 6'):
                        obj=_job_record(row['job'],conn)
                        if obj is not None:
                            records.append(obj)
                with _JOBS_LOCK:
                    in_memory=sorted(_JOBS.items(), key=lambda x:x[1]['issued'],reverse=True)
                for job,entry in in_memory:
                    if len(records)>=6: break
                    if not any(obj['job']==job for obj in records):
                        records.append(dict(entry,job=job,tracked=False))
            else:
                obj=_job_record(values['job'][0],conn)
                if obj is None:
                    return handler.send(404,'Poster batch not found or expired','text/plain')
                records=[obj]
            if records:
                paths=set(path for record in records for path in record['paths'])
                # Fetch statuses once; no writing or queue processing occurs here.
                recorded={row['path']:row['status'] for row in
                          conn.execute('SELECT path,status FROM posters') if row['path'] in paths}
            else:
                recorded={}
        with busy_lock:
            busy=set(busy_set)
        is_paused=bool(paused()) if records else False
        summaries=[_summary(record,poster_file,recorded,busy,is_paused) for record in records]
    except Exception as exc:
        _log('history_read_error',reason=type(exc).__name__)
        return handler.send(503,'Poster queue history unavailable','text/plain')
    if recent:
        return handler.send(200,json.dumps({'jobs':summaries}), 'application/json')
    return handler.send(200,json.dumps(summaries[0]),'application/json')


def modal_script():
    """Same-page popup, persistent Activity history, and reopenable queue progress."""
    return r'''<style>
#themearr-poster-modal{position:fixed;inset:0;z-index:9999;background:rgba(3,9,22,.78);display:none;align-items:center;justify-content:center;padding:18px;box-sizing:border-box}
#themearr-poster-modal[aria-hidden="false"]{display:flex}
#themearr-poster-modal .poster-modal-card{background:#1b2942;color:#f2f6ff;border:1px solid #52709d;border-radius:14px;width:min(650px,100%);padding:24px;max-height:88vh;overflow:auto;box-shadow:0 20px 70px rgba(0,0,0,.6)}
#themearr-poster-modal button{background:#386ce9;color:white;border:none;border-radius:6px;padding:11px 15px;font-weight:650;cursor:pointer;margin:6px 8px 4px 0}
#themearr-poster-modal button.secondary{background:#42536b}
#themearr-poster-modal button:disabled{opacity:.5;cursor:wait}
#themearr-poster-modal .muted{color:#bacbe7;line-height:1.5}
#themearr-poster-modal [role=status]{padding:10px 0;font-weight:600}
#themearr-poster-queue{margin:18px 0}
body.dashboard-view #themearr-poster-queue{display:none!important}
#themearr-poster-queue .poster-job{padding:12px 0;border-top:1px solid #394966;line-height:1.5}
#themearr-poster-queue .poster-job small{color:#b9c9df}
#themearr-poster-queue button,#themearr-queue-reopen{background:#3468de;color:#fff;border:0;border-radius:6px;padding:9px 12px;font-weight:600;cursor:pointer}
/* V1.4.4: Responsive control alignment based on the actual Library Search panel,
   not the device screen (works for split desktop windows and expanded sidebars). */
.themearr-poster-search-layout{container-type:inline-size}
#themearr-poster-actionbar{display:flex;flex-direction:column;align-items:flex-start;gap:6px;width:100%;box-sizing:border-box}
#themearr-poster-actionbar>form{min-width:0;max-width:100%}
#themearr-queue-reopen{flex:0 0 auto;white-space:nowrap;margin:0}
@container (min-width:950px){
  #themearr-poster-actionbar{flex-direction:row;align-items:center;justify-content:space-between;gap:18px}
  #themearr-poster-actionbar>form{flex:1 1 auto;max-width:calc(100% - 190px)}
  #themearr-queue-reopen{margin-left:auto;margin-top:15px}
}
</style><div id="themearr-poster-modal" aria-hidden="true"><div class="poster-modal-card" role="dialog" aria-modal="true" aria-labelledby="poster-modal-title"><h2 id="poster-modal-title">Review poster retry</h2><p id="poster-modal-description" class="muted"></p><p id="poster-modal-progress" role="status" aria-live="polite"></p><div id="poster-modal-actions"><button id="poster-modal-confirm" type="button">Confirm retry</button><button id="poster-modal-close" class="secondary" type="button">Cancel — no changes</button></div></div></div>
<script>
(function(){'use strict';
document.addEventListener('DOMContentLoaded',function(){
const form=document.querySelector('form[action="/poster-filtered-review"]');
const modal=document.getElementById('themearr-poster-modal');
if(!form||!modal)return;
const title=document.getElementById('poster-modal-title');
const description=document.getElementById('poster-modal-description');
const progress=document.getElementById('poster-modal-progress');
const confirm=document.getElementById('poster-modal-confirm');
const close=document.getElementById('poster-modal-close');
let pendingToken=null,polling=null,activeJob=null,lastFocus=null,busy=false,historyPolling=null;
const HEADERS={'X-Themearr-Modal':'1','Accept':'application/json'};
const historyPanel=document.createElement('section');historyPanel.id='themearr-poster-queue';historyPanel.className='panel';
const historyTitle=document.createElement('h2');historyTitle.textContent='Poster Queue';historyPanel.appendChild(historyTitle);
const historyNote=document.createElement('p');historyNote.className='muted';historyNote.textContent='Recent filtered poster retries. Closing the popup does not cancel processing. History is shared across devices and restarts.';historyPanel.appendChild(historyNote);
const historyEntries=document.createElement('div');historyEntries.id='themearr-poster-queue-jobs';historyPanel.appendChild(historyEntries);
const recovery=document.getElementById('poster-recovery');
const activity=document.getElementById('activity-heading');
if(recovery)recovery.insertAdjacentElement('afterend',historyPanel);
else if(activity)activity.insertAdjacentElement('afterend',historyPanel);
const reopen=document.createElement('button');reopen.type='button';reopen.id='themearr-queue-reopen';reopen.textContent='View Poster Queue';
reopen.title='Reopen the latest poster retry without queuing another request';
// Keep both existing controls in one responsive row; moving the original form
// preserves its data, submit listener, and single-use confirmation workflow.
const searchPanel=form.closest('.panel');
if(searchPanel)searchPanel.classList.add('themearr-poster-search-layout');
const actionbar=document.createElement('div');actionbar.id='themearr-poster-actionbar';
form.parentNode.insertBefore(actionbar,form);
actionbar.appendChild(form);
actionbar.appendChild(reopen);
function clearPoll(){if(polling){clearInterval(polling);polling=null;}}
function dismiss(){if(busy)return;clearPoll();modal.setAttribute('aria-hidden','true');pendingToken=null;activeJob=null;if(lastFocus)lastFocus.focus();refreshHistory();}
function open(){lastFocus=document.activeElement;modal.setAttribute('aria-hidden','false');close.focus();}
function ui(t,d,p,canConfirm){title.textContent=t;description.textContent=d;progress.textContent=p;confirm.hidden=!canConfirm;confirm.disabled=false;close.textContent=canConfirm?'Cancel — no changes':'Close';}
let signedOut=false;async function jsonResponse(res){if(res.status===401||(res.redirected&&new URL(res.url).pathname==='/login')){signedOut=true;throw Error('You were signed out — reload the page to sign in again.')}const text=await res.text();let obj;try{obj=JSON.parse(text)}catch(_){obj=null}if(!res.ok)throw Error((obj&&obj.error)||text.slice(0,160)||('HTTP '+res.status));if(!obj)throw Error('Unexpected response from Preludarr.');return obj;}
async function post(route,body){return jsonResponse(await fetch(route,{method:'POST',body,headers:HEADERS,credentials:'same-origin'}));}
async function getStatus(query){return jsonResponse(await fetch('/poster-filtered-status?'+query,{credentials:'same-origin',headers:{'Accept':'application/json'},cache:'no-store'}));}
function message(d){return 'Completed: '+d.completed+' · No match: '+d.no_match+' · Failed: '+d.failed+' · Processing: '+d.processing+' · Waiting: '+d.waiting+(d.paused?' · Recovery paused':'');}
async function poll(){if(!activeJob||modal.getAttribute('aria-hidden')==='true')return;try{let d=await getStatus('job='+encodeURIComponent(activeJob));progress.textContent=message(d);if(d.finished){clearPoll();activeJob=null;title.textContent='Poster retry finished';description.textContent='Selected attempts are finished. Check Library Search for any new posters; see Settings → Logs for diagnostics.';}refreshHistory();}catch(e){progress.textContent='Cannot refresh batch: '+e.message+'. Queued work is unaffected.';clearPoll();}}
function track(job){clearPoll();pendingToken=null;activeJob=job;open();ui('Poster retry status','Tracking an existing request. No new work is queued.','Loading saved progress…',false);poll();polling=setInterval(poll,15000);}
async function refreshHistory(){if(!historyPanel.parentNode)return;try{const data=await getStatus('recent=1');historyEntries.replaceChildren();const jobs=data.jobs||[];
if(!jobs.length){const p=document.createElement('p');p.className='muted';p.textContent='No tracked poster retry batches yet. New requests will appear here, including after a restart.';historyEntries.appendChild(p);return;}
for(const item of jobs){const entry=document.createElement('div');entry.className='poster-job';
const strong=document.createElement('strong');const date=new Date(item.created*1000);strong.textContent=(Number.isFinite(date.getTime())?date.toLocaleString():'Poster batch')+' — '+item.submitted+' submitted'+(item.finished?' · Finished':' · Active');entry.appendChild(strong);
const note=document.createElement('div');note.textContent=message(item);entry.appendChild(note);
const meta=document.createElement('small');meta.textContent='Scope: '+item.library+' · Theme: '+item.theme+' · Poster: '+item.poster+' · Skipped: '+item.skipped+(item.tracked?'':' · Current process only');entry.appendChild(meta);
const row=document.createElement('div');const button=document.createElement('button');button.type='button';button.textContent='Track this request';button.addEventListener('click',()=>track(item.job));row.appendChild(button);entry.appendChild(row);
historyEntries.appendChild(entry);}
}catch(e){historyEntries.textContent='Queue history temporarily unavailable: '+e.message;}}
reopen.addEventListener('click',async function(){try{const data=await getStatus('recent=1');if(data.jobs&&data.jobs.length)track(data.jobs[0].job);else{open();ui('Poster Queue','No tracked requests yet.','Newly confirmed retries will appear here and on Activity.',false);}}catch(e){open();ui('Poster Queue unavailable',e.message,'No new request was started.',false);}});
form.addEventListener('submit',async function(e){e.preventDefault();if(busy)return;clearPoll();activeJob=null;open();ui('Review poster retry','Checking all matching titles across the selected filters…','',false);busy=true;try{const data=await post('/poster-filtered-review',new URLSearchParams(new FormData(form)));pendingToken=data.token||null;if(!data.count){ui('No missing posters','No missing artwork matches these filters. No changes made.','',false);return;}ui('Review filtered poster retry',data.count+' missing posters match the filters. '+data.details,'Only missing artwork is queued. Existing rate limits apply.',true);confirm.textContent='Confirm: Queue '+data.count+' missing posters';}catch(e){ui('Unable to review',e.message,'Nothing queued.',false)}finally{busy=false;}});
confirm.addEventListener('click',async function(){if(!pendingToken||busy)return;busy=true;confirm.disabled=true;close.disabled=true;progress.textContent='Queuing approved request…';try{const data=await post('/poster-filtered-confirm',new URLSearchParams({token:pendingToken}));pendingToken=null;activeJob=data.job;ui('Poster retry queued',data.queued+' missing posters queued; '+data.skipped+' skipped. Downloading is not finished.'+(data.tracked?'':' Queue history could not be saved; this browser session can still track it.'),'Waiting for the rate-limited Poster Recovery worker.',false);await refreshHistory();if(activeJob){await poll();if(activeJob&&!polling)polling=setInterval(poll,15000);}}catch(e){pendingToken=null;ui('Poster retry not queued',e.message,'No new batch confirmed.',false)}finally{busy=false;close.disabled=false;}});
close.addEventListener('click',dismiss);
modal.addEventListener('click',function(e){if(e.target===modal)dismiss();});
document.addEventListener('keydown',function(e){if(e.key==='Escape'&&modal.getAttribute('aria-hidden')==='false')dismiss();});
refreshHistory();historyPolling=setInterval(function(){if(signedOut){clearInterval(historyPolling);return}if(!document.hidden && historyPanel.getClientRects().length)refreshHistory();},30000);
});})();
</script>'''
