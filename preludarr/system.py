# SPDX-License-Identifier: GPL-3.0-or-later
"""Themearr System V1: authenticated System hub, safe online backup and log viewer.

Caller MUST pass through Themearr's existing auth.get/auth.post guards before routing.
Restore intentionally requires stopped-container host CLI, never web execution.
No Docker socket, shell calls, archive extraction, or media writes.
"""
from __future__ import annotations
import hashlib
import hmac
import html
import io
import json
import os
from pathlib import Path
from urllib.parse import urlencode
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit
import zipfile
from datetime import datetime, timezone
from backup_upload import upload_backup as _upload_backup
import restore  # 1.12: restore on restart, applied inside the container
from restore import SCHEMA, MAX_ZIP_BYTES, MAX_DB_BYTES, FILENAME, EXTRA_NAME, verify_archive
TOKENS = {}
LOCK = threading.RLock()


def _esc(value): return html.escape(str(value), quote=True)


def _send(handler, status, text, ctype='text/html; charset=utf-8'):
    nonce=secrets.token_urlsafe(16)
    if ctype.startswith('text/html'):
        text=__import__('login').add_icons(text)  # V1.9.7 site emblem
        text=__import__('ui').apply(text,getattr(handler,'path','/system'))  # V1.9.8 shared design
        text=re.sub(r'<script(?=[\s>])','<script nonce="'+nonce+'"',text)  # V1.9.8: only our own inline scripts may run
    data=text.encode('utf-8')
    handler.send_response(status)
    for k,v in [('Content-Type',ctype),('Content-Length',str(len(data))),('Cache-Control','no-store'),
                ('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),('Referrer-Policy','same-origin'),
                ('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; manifest-src 'self'; connect-src 'self'; script-src 'nonce-"+nonce+"'; form-action 'self'; base-uri 'none'")]:
        handler.send_header(k,v)
    handler.end_headers()
    handler.wfile.write(data)


def _safe_cfg(config):
    path=Path(config)
    if path.is_symlink() or not path.is_dir(): raise ValueError('Persistent /config is unavailable')
    return path


def _backup_dir(config, create=False):
    root=_safe_cfg(config)/'system_backups'
    if root.is_symlink(): raise ValueError('Backup directory link refused')
    if create:
        root.mkdir(mode=0o700,exist_ok=True)
        os.chmod(root,0o700)
    if not root.is_dir() or root.is_symlink(): raise ValueError('Backup directory is unavailable')
    return root


def _files(config):
    try: root=_backup_dir(config)
    except ValueError: return []
    out=[]
    for p in root.iterdir():
        if not FILENAME.fullmatch(p.name) or not p.is_file() or p.is_symlink(): continue
        st=p.stat()
        if st.st_size<=MAX_ZIP_BYTES:
            out.append((p.name,st.st_size,st.st_mtime))
    return sorted(out,key=lambda x:x[2],reverse=True)


def _new_token():
    value=secrets.token_urlsafe(32)
    with LOCK:
        now=time.monotonic()
        for key,exp in list(TOKENS.items()):
            if exp<now: TOKENS.pop(key,None)
        if len(TOKENS)>=64:
            TOKENS.pop(next(iter(TOKENS)))
        TOKENS[value]=now+900
    return value


def _consume_token(token):
    if not isinstance(token,str) or len(token)>128: return False
    with LOCK:
        for key,exp in list(TOKENS.items()):
            if hmac.compare_digest(key,token):
                TOKENS.pop(key,None)
                return exp>=time.monotonic()
    return False


from contextlib import closing

def create_backup(config):
    """SQLite online backup + small root configuration files. Excludes media, posters and logs."""
    cfg=_safe_cfg(config)
    root=_backup_dir(cfg,True)
    if len(_files(cfg))>=20:
        raise ValueError('Backup limit (20) reached. Download and archive old backups before creating more.')
    db=cfg/'library.db'
    if db.is_symlink() or not db.is_file() or db.stat().st_size>MAX_DB_BYTES:
        raise ValueError('SQLite database unavailable or exceeds safe snapshot size')
    free=os.statvfs(root)
    if free.f_bavail*free.f_frsize < max(MAX_DB_BYTES//3,3*db.stat().st_size):
        raise ValueError('Not enough free space to create a safe backup')
    name='tsbackup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(4)+'.zip'
    staged=None
    try:
        with tempfile.TemporaryDirectory(prefix='.themearr-backup-',dir=root) as tdir:
            tmp=Path(tdir)
            dbcopy=tmp/'library.db'
            source=sqlite3.connect('file:'+quote(str(db))+'?mode=ro',uri=True,timeout=15)
            try:
                target=sqlite3.connect(dbcopy)
                try: source.backup(target,pages=300,sleep=.05)
                finally: target.close()
            finally: source.close()
            with closing(sqlite3.connect('file:'+quote(str(dbcopy))+'?mode=ro',uri=True)) as check:
                if check.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                    raise ValueError('Snapshot database failed integrity check')
            files=[('library.db',dbcopy)]
            for p in sorted(cfg.iterdir(),key=lambda x:x.name):
                if EXTRA_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink() and p.stat().st_size<=2*1024*1024:
                    files.append(('config/'+p.name,p))
            hashes={name:hashlib.sha256(p.read_bytes()).hexdigest() for name,p in files}
            manifest={'schema':SCHEMA,'created_utc':datetime.now(timezone.utc).isoformat(),
                      'contents':'Database and root-level configuration only; excludes media, poster cache and logs',
                      'files':hashes}
            dest=tmp/'backup.zip'
            with zipfile.ZipFile(dest,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=False) as z:
                for entry,p in files: z.write(p,arcname=entry)
                z.writestr('manifest.json',json.dumps(manifest,sort_keys=True))
            if dest.stat().st_size>MAX_ZIP_BYTES: raise ValueError('Snapshot archive size limit exceeded')
            verify_archive(dest)
            final=root/name
            staged=root/('.pending-'+name)
            if final.exists() or final.is_symlink() or staged.exists() or staged.is_symlink():
                raise ValueError('Backup filename collision')
            os.replace(dest,staged)
            os.chmod(staged,0o600)

        if final.exists() or final.is_symlink():
            raise ValueError('Backup filename collision')
        os.replace(staged,final)
        staged=None
        return name
    finally:
        if staged is not None and staged.exists() and not staged.is_symlink():
            staged.unlink()

def _nav(current):
    labels=[('overview','Overview'),('activity','Activity'),('backup','Backup & Restore'),('logs','Logs'),('releases','Releases'),('health','Library Health'),('advanced','Advanced Settings')]  # order: user 2026-10-27  # V1.9.5: Library Health lives in System
    bits=[]
    for tab,label in labels:
        cls=' class="active" aria-current="page"' if tab==current else ''
        bits.append(f'<a href="/system?tab={tab}"{cls}>{_esc(label)}</a>')
    return '<nav class="tabs" aria-label="System navigation">'+''.join(bits)+'</nav>'


ACTIVITY_PAGE=50


def _activity_page(app,query,where='system'):
    """Full activity log: every event, newest first, search + type filter + paging (read-only).
    1.12.2: also on the Dashboard (where='dashboard'): own parameter names (aq, atype, apage) and a remembered page size."""
    dash=where=='dashboard'
    P=(lambda n:'a'+n) if dash else (lambda n:n)
    term=(query.get(P('q'),[''])[0]).strip()[:100]
    kind=(query.get(P('type'),[''])[0]).strip()[:40]
    try:page_no=max(1,int(query.get(P('page'),['1'])[0]))
    except ValueError:page_no=1
    size=__import__('library_prefs').activity_size(app.CONFIG,query) if dash else ACTIVITY_PAGE
    where,args=[],[]
    if term:
        where.append("(name LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\')")
        like='%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        args+=[like,like]
    if kind:
        where.append('status=?');args.append(kind)
    sql=(' WHERE '+' AND '.join(where)) if where else ''
    with app.db() as c:
        kinds=[r[0] for r in c.execute('SELECT status FROM events GROUP BY status ORDER BY COUNT(*) DESC, status')]
        total=c.execute('SELECT COUNT(*) FROM events'+sql,args).fetchone()[0]
        pages=max(1,(total+size-1)//size);page_no=min(page_no,pages)
        rows=c.execute('SELECT at,name,status,detail FROM events'+sql+' ORDER BY id DESC LIMIT ? OFFSET ?',args+[size,(page_no-1)*size]).fetchall()
        # Preludarr 1.11.1 (user 2026-10-29): titles link to their Theme Browser page. Events store the folder name, so a name that
        # matches exactly one library title links to it; a name shared by several titles links to a Library search for it.
        found={}
        names=sorted({str(r['name']) for r in rows})
        if names:
            for s in c.execute('SELECT name,path FROM shows WHERE name IN (SELECT value FROM json_each(?))',(json.dumps(names),)):
                found.setdefault(s['name'],[]).append(s['path'])
    def url(p):
        from urllib.parse import urlencode
        if dash:
            return '/?'+urlencode({k:v for k,v in (('aq',term),('atype',kind),('apage',p if p>1 else '')) if v})+'#dash-activity'
        return '/system?'+urlencode({k:v for k,v in (('tab','activity'),('q',term),('type',kind),('page',p)) if v})
    zone=_esc(__import__('ui').when(time.time()).split(' ')[-1])
    if dash:
        parts=['<section class="panel" id="dash-activity"><h2>Activity</h2>',
               '<p class="muted">Latest events, newest first, times in '+zone+'. Full history with the same filters: <a href="/system?tab=activity">System → Activity</a>.</p>',
               '<form method="get" action="/#dash-activity" class="act-filter">',
               '<input name="aq" maxlength="100" placeholder="Search title or details" value="',_esc(term),'" aria-label="Search activity">',
               '<select name="atype" aria-label="Event type"><option value="">All events</option>']
    else:
        parts=['<section class="panel" id="activity"><h2>Activity</h2>',
           '<p class="muted">Everything Preludarr did: installs, failures, reviews, recycle bin, scans and settings — newest first, times in '+zone+'. '
           'The sidebar’s <strong>Activity</strong> item opens this same tab. Related: <a href="/?view=activity#poster-recovery">Poster recovery</a> · <a href="/poster-report">Missing poster report</a></p>',
           '<form method="get" action="/system" class="act-filter"><input type="hidden" name="tab" value="activity">',
           '<input name="q" maxlength="100" placeholder="Search title or details" value="',_esc(term),'" aria-label="Search activity">',
           '<select name="type" aria-label="Event type"><option value="">All events</option>']
    for k in kinds:
        parts+=['<option value="',_esc(k),'"',' selected' if k==kind else '','>',_esc(k),'</option>']
    parts+=['</select>']
    if dash:  # page size, remembered (library_prefs.activity_size)
        sizes=__import__('library_prefs').ACTIVITY_SIZES
        parts+=['<label class="act-size">Show <select name="asize" aria-label="Events per page" onchange="this.form.submit()">',
                ''.join('<option value="'+str(n)+'"'+(' selected' if n==size else '')+'>'+str(n)+'</option>' for n in sizes),'</select> per page</label>']
    parts+=['<button type="submit">Filter</button>',
            ('<a class="button act-clear" href="'+('/#dash-activity' if dash else '/system?tab=activity')+'">Clear</a>' if term or kind else ''),'</form>',
            '<p class="muted">',str(total),' event',('' if total==1 else 's'),('' if not (term or kind) else ' match'),'.</p>']
    if rows:
        ui=__import__('ui')
        def tone(k):
            k=(k or '').lower()
            return ('bad' if any(w in k for w in ('fail','error')) else 'ok' if any(w in k for w in ('install','download','kept','fixed','saved')) else
                    'warn' if any(w in k for w in ('review','undone','expired','emptied')) else 'info')
        def details(text):
            text=str(text or '')
            if len(text)<=140:return _esc(text)
            return '<details class="act-more"><summary>'+_esc(text[:120])+'…</summary>'+_esc(text)+'</details>'
        parts+=['<div class="scroll"><table class="act-table"><colgroup><col class="c-when"><col class="c-title"><col class="c-kind"><col></colgroup>'
                '<thead><tr><th>When</th><th>Title</th><th>Event</th><th>Details</th></tr></thead><tbody>']
        for r in rows:
            try:stamp=ui.when(datetime.fromisoformat(str(r['at']).replace('Z','+00:00')))
            except ValueError:stamp=str(r['at'])
            paths=found.get(str(r['name']),[])
            from urllib.parse import urlencode
            link=('/themes?'+urlencode({'path':paths[0]})) if len(paths)==1 else ('/?'+urlencode({'view':'library','q':r['name']})) if paths else ''
            title=('<a href="'+_esc(link)+'" title="'+('Open in the Theme Browser' if len(paths)==1 else 'Several titles share this name — show them in the Library')+'">'+_esc(r['name'])+'</a>') if link else _esc(r['name'])
            parts+=['<tr><td title="Recorded '+_esc(r['at'])+'">',_esc(stamp),'</td><td class="act-title">',title,'</td>',
                    '<td><span class="act-kind ',tone(r['status']),'">',_esc(r['status']),'</span></td><td>',details(r['detail']),'</td></tr>']
        parts+=['</tbody></table></div>']
    else:
        parts+=['<p>No activity',' matches this filter' if term or kind else ' yet','.</p>']
    if pages>1:
        parts+=['<p class="act-pager">']
        if page_no>1:parts+=['<a href="',_esc(url(page_no-1)),'">← Newer</a> ']
        parts+=['Page ',str(page_no),' / ',str(pages)]
        if page_no<pages:parts+=[' <a href="',_esc(url(page_no+1)),'">Older →</a>']
        parts+=['</p>']
    parts+=['<style>.act-filter{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0}.act-filter input{flex:1 1 240px}',
            '.act-filter select{padding:10px;background:#0d1728;border:1px solid #52668b;border-radius:6px;color:#fff}',
            '.act-table{table-layout:fixed;min-width:720px}.act-table .c-when{width:190px}.act-table .c-title{width:30%}.act-table .c-kind{width:150px}',
            '.act-table td{vertical-align:top;overflow-wrap:anywhere}.act-table td:first-child{font-size:13.5px;color:#cfe0f8}.act-title{font-weight:600}',
            '.act-title a{color:#eef4ff;text-decoration:underline;text-decoration-color:#5c7398;text-underline-offset:3px}.act-title a:hover{color:#9fd0ff;text-decoration-color:#9fd0ff}',
            '.act-title a:focus-visible{outline:3px solid #8fc1ff;outline-offset:2px;border-radius:3px}',
            '.act-kind{display:inline-block;padding:2px 9px;border-radius:999px;background:#294569;color:#e4edfb;font-size:12.5px;font-weight:700;white-space:nowrap}',
            '.act-kind.ok{background:#1d4a39;color:#b9f3d5}.act-kind.bad{background:#4a2430;color:#ffc4cc}.act-kind.warn{background:#4a3a1c;color:#ffe2a8}',
            '.act-more summary{cursor:pointer;color:#cfe0f8}.act-pager{margin-top:12px}',
            '.act-size{display:flex;gap:6px;align-items:center;white-space:nowrap;color:#cfe0f8}.act-size select{padding:10px}',
            '#dash-activity .scroll{overflow-x:auto;max-width:100%;scrollbar-color:#3b4b6b #101c30}#dash-activity table{width:100%;border-collapse:collapse}</style></section>']
    return ''.join(parts)


def _health_text(config):
    """V1.9.3: one-line summary of the last Library Health scan (read-only)."""
    try:
        import json as _j
        data=_j.loads((Path(config)/'themearr_library_health.json').read_text())
        n=sum((data.get('counts') or {}).values())
        return ('No problems found' if not n else str(n)+' finding(s)')+' · last scan '+time.strftime('%Y-%m-%d %H:%M',time.localtime(data['at']))
    except Exception:
        return 'Not scanned yet'


def _about_html():
    """1.12: licence and data-source credits (TMDB's API terms require the attribution sentence)."""
    return ('<section class="panel" id="about"><h2>About Preludarr</h2>'
            '<p>Preludarr is free software under the <strong>GNU General Public License, version 3 or later</strong> (GPL-3.0-or-later), '
            'with no warranty. The licence, notices and the list of bundled third-party packages are in the '
            '<code>LICENSE</code>, <code>NOTICE.md</code> and <code>THIRD_PARTY_PACKAGES.txt</code> files of the project and inside the image (<code>/app</code>).</p>'
            '<h3>Data sources</h3><ul class="ab-src">'
            '<li><strong>AnimeThemes</strong> (animethemes.moe): anime openings and endings.</li>'
            '<li><strong>AniList</strong> and <strong>AniDB</strong>: anime titles and ids.</li>'
            '<li><strong>TMDB</strong>: This product uses the TMDB API but is not endorsed or certified by TMDB.</li>'
            '<li><strong>TheTVDB</strong>, <strong>Plex TV Themes</strong> and <strong>ThemerrDB</strong> (LizardByte): show and movie themes.</li>'
            '<li><strong>YouTube</strong>: optional search through the YouTube Data API (preview only).</li></ul>'
            '<p class="muted">Preludarr is not affiliated with or endorsed by these services. Finding a song through a source does not mean you are allowed to download it.</p>'
            '<style>#about{margin-top:18px}.ab-src{line-height:1.7;padding-left:22px}#about h3{margin:14px 0 4px;font-size:1.02rem}</style></section>')


def _cards(config):
    """System Overview (V1.9.8): real information already known to Themearr; every card links to its tab and says how fresh it is."""
    import sys as _sys
    ui=__import__('ui')
    app=_sys.modules.get('app') or _sys.modules.get('__main__')
    cards=[]
    def card(title,href,state,main,sub=''):
        cards.append('<a class="ov-card" href="'+_esc(href)+'"><span class="ov-title">'+_esc(title)+ui.label(state)+'</span>'
                     '<span class="ov-main">'+main+'</span>'+('<span class="ov-sub">'+sub+'</span>' if sub else '')+'</a>')
    try:
        from login import APP_VERSION
        card('Version','/system?tab=releases','current','V'+_esc(APP_VERSION),'What\u2019s new and how to update')
    except Exception:
        card('Version','/system?tab=releases','unavailable','Unknown')
    db=Path(config)/'library.db'
    try:
        with closing(sqlite3.connect('file:'+quote(str(db))+'?mode=ro',uri=True)) as c:
            total,installed=c.execute("SELECT COUNT(*),SUM(state='installed') FROM shows").fetchone()
        card('Database','/system?tab=backup','current','Available',f'{total or 0} title folders · {installed or 0} with a theme')
    except Exception:
        card('Database','/system?tab=backup','unavailable','Not readable','The library database could not be opened read-only.')
    files=_files(config)
    if files:
        ab=__import__('auto_backup').load(config)
        card('Backups','/system?tab=backup','current',f'{len(files)} in-app backup'+('' if len(files)==1 else 's'),'Newest '+_esc(ui.when(files[0][2]))
             +('<br>Automatic every '+str(ab['interval_days'])+' days' if ab['enabled'] else '<br>Automatic backups off'))
    else:
        card('Backups','/system?tab=backup','current','None yet','Create one on Backup &amp; Restore'+('; automatic backups are on.' if __import__('auto_backup').load(config)['enabled'] else '.'))
    try:
        h=json.loads((Path(config)/'themearr_library_health.json').read_text())
        counts=h.get('counts') or {}
        lh=__import__('library_health')
        top=sorted(counts.items(),key=lambda kv:(lh.SEV_ORDER[lh.FINDINGS[kv[0]][0]],-kv[1]))[:3]
        sub=' · '.join(_esc(lh.FINDINGS[k][1])+' '+str(n) for k,n in top) or 'No problems found'
        card('Library Health','/system?tab=health','cached',f'{sum(counts.values())} finding'+('' if sum(counts.values())==1 else 's'),sub+'<br>Last scan '+_esc(ui.when(h['at'])))
    except Exception:
        card('Library Health','/system?tab=health','unavailable','Not scanned yet','Run a health scan to check your folders.')
    try:
        with app.db() as c:
            last=c.execute('SELECT at FROM events ORDER BY id DESC LIMIT 1').fetchone()
            n=c.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        when=datetime.fromisoformat(str(last[0]).replace('Z','+00:00')) if last else None
        card('Activity','/system?tab=activity','current',f'{n} event'+('' if n==1 else 's'),('Latest '+_esc(ui.when(when))) if when else 'Nothing recorded yet')
    except Exception:
        card('Activity','/system?tab=activity','unavailable','Unknown')
    try:
        tr=__import__('theme_review')
        b=tr.batches(app); st=tr.settings(config)
        card('Recycle bin','/library-health/trash','current',f'{len(b)} entr'+('y' if len(b)==1 else 'ies'),f"Kept {st['days']} days"+(' · emptied on schedule' if st['cron'] else ''))
    except Exception:
        pass
    return ('<div class="ov-wrap"><div class="ov-grid">'+''.join(cards)+'</div></div>'
            '<p class="muted">'+ui.label('current')+' read now · '+ui.label('cached')+' from the last scan · '+ui.label('unavailable')+' not known. '
            'Opening this page never starts scans, checks or backups.</p>'
            +_about_html()+
            # V1.9.9: 3 columns x 2 rows on desktop (six cards, none stranded), then 2, then 1 as the page narrows; every card in a row the same height
            '<style>.ov-wrap{container-type:inline-size;container-name:ov}'
            '.ov-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));grid-auto-rows:1fr;gap:14px}'
            '@container ov (max-width:820px){.ov-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}'
            '@container ov (max-width:500px){.ov-grid{grid-template-columns:1fr;grid-auto-rows:auto}}'
            '.ov-card{box-sizing:border-box;display:flex;flex-direction:column;gap:6px;padding:18px;border:1px solid #36486a;border-radius:12px;background:#1b2840;color:#eef4ff!important;text-decoration:none!important;min-height:130px}'
            '.ov-card:focus-visible{outline:3px solid #8fc1ff;outline-offset:2px}'
            '.ov-card:hover{border-color:#5f86c8;background:#1f3050}.ov-title{font-weight:700;color:#cfe0f8;display:flex;justify-content:space-between;align-items:center;gap:6px}'
            '.ov-main{font-size:22px;font-weight:800}.ov-sub{color:#b4c4dc;font-size:13.5px;line-height:1.5}.ov-sub code{font-size:12px;overflow-wrap:anywhere}</style>')


# 1.12: a confirmed restore is applied by restore.apply_pending() on the next start, before the database opens.
def _restore_allowed(config, name):
    if not FILENAME.fullmatch(name):
        raise ValueError('Invalid backup filename')
    archive = _backup_dir(config) / name
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > MAX_ZIP_BYTES:
        raise ValueError('Backup unavailable or too large')
    return archive


def _restore_check(config, name):
    return verify_archive(_restore_allowed(config, name))


def _restore_preview(config, query):
    name = query.get('name', [''])[0]
    try:
        details = _restore_check(config, name)
    except Exception:
        return '<section class="panel"><h2>Cannot verify backup</h2><p>Archive is missing, invalid, or incompatible.</p><a href="/system?tab=backup">Back to backups</a></section>'
    token = _new_token()
    return ('<section class="panel"><h2>Verify / Restore</h2>'
            '<p class="message">The backup is valid: every checksum matches and the database passed its integrity check.</p>'
            '<p><strong>Archive:</strong> '+_esc(name)+'</p>'
            '<p><strong>Created (UTC):</strong> '+_esc(details['created_utc'])+'</p>'
            '<p><strong>Files:</strong> '+_esc(details['files'])+' (library database and settings)</p>'
            '<p><strong>SHA256:</strong> <code>'+_esc(details['sha256'])+'</code></p>'
            '<p class="warn">Restoring replaces the current library database and every settings file in this backup. Media files are never changed.</p>'
            '<form method="post" action="/system/backup/prepare">'
            '<input type="hidden" name="csrf" value="'+_esc(token)+'">'
            '<input type="hidden" name="name" value="'+_esc(name)+'">'
            '<input type="hidden" name="sha256" value="'+_esc(details['sha256'])+'">'
            '<label>Type RESTORE: <input name="confirm" maxlength="7" required autocomplete="off"></label> '
            '<button type="submit">Restore on next restart</button></form>'
            '<p class="muted">Nothing changes yet. The restore runs the next time the container starts: Preludarr first saves a safety copy of your current data, '
            'then restores and checks the backup, and puts your previous data back automatically if anything fails.</p></section>')


def _prepare_restore(config, fields):
    if fields.get('confirm', [''])[0] != 'RESTORE':
        raise ValueError('Exact RESTORE confirmation required')
    if not _consume_token(fields.get('csrf', [''])[0]):
        raise ValueError('Expired or replayed confirmation')
    name = fields.get('name', [''])[0]
    _restore_allowed(config, name)
    with LOCK:
        restore.schedule(_safe_cfg(config), name, fields.get('sha256', [''])[0])
    return name


def _restore_status_panel(config):
    """Pending restore (with Cancel) or the result of the last one."""
    req = restore.pending(config)
    if req:
        return ('<section class="panel" id="restore-status"><h2>Restore scheduled</h2>'
                '<p class="warn"><strong>Restart the Preludarr container to restore</strong> <code>'+_esc(req.get('archive', ''))+'</code>. '
                'Until then nothing has changed.</p><p class="muted">Restart it from your Docker tool (for example Unraid: Docker → Preludarr → Restart, '
                'or <code>docker restart preludarr</code>). This page shows the result after the restart.</p>'
                '<form method="post" action="/system/backup/cancel"><input type="hidden" name="csrf" value="'+_esc(_new_token())+'">'
                '<button type="submit">Cancel restore</button></form></section>')
    res = restore.last_result(config)
    if not res:
        return ''
    ui = __import__('ui')
    return ('<section class="panel" id="restore-status"><h2>Last restore</h2>'
            '<p class="'+('message' if res.get('ok') else 'warn')+'">'+('✓ ' if res.get('ok') else '✗ ')+_esc(res.get('message', ''))+'</p>'
            '<p class="muted">Finished '+_esc(ui.when(_iso(res.get('finished_utc'))))+'</p></section>')


def _iso(text):
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None


def _backup_page(config,message=''):
    ui=__import__('ui')
    files=_files(config)
    auto=set(__import__('auto_backup').load(config)['auto'])  # V1.9.13
    rows=[]
    for name,size,modified in files[:100]:
        link='/system/backup/download?name='+quote(name)
        rows.append('<tr><td><strong>'+_esc(ui.when(modified))+'</strong> <span class="bk-kind'+(' auto' if name in auto else '')+'">'+('Automatic' if name in auto else 'Manual')+'</span><details class="bk-name"><summary>File name</summary><code>'+_esc(name)+'</code></details></td>'
                    '<td>'+f'{size/1024/1024:.2f} MiB'+'</td>'
                    '<td class="bk-actions"><a class="btn" href="'+_esc(link)+'">Download</a> '
                    '<a class="btn" href="/system/backup/verify?name='+quote(name)+'">Verify archive</a></td></tr>')
    if not rows:rows=['<tr><td colspan="3">No in-app backups yet. Use <strong>Create backup now</strong> above.</td></tr>']
    token=_new_token()
    msg='<p class="message">'+_esc(message)+'</p>' if message else ''
    return (msg+_restore_status_panel(config)+'<section class="panel"><h2>Create a backup</h2><p>Saves the library database and Preludarr settings. '
            '<strong>Not included:</strong> media, posters, logs and the Docker template. '
            '<strong>Backups may contain credentials — keep downloads private.</strong> Up to 20 backups. Manual backups are never deleted automatically; '
            'automatic ones are removed after their retention period (below).</p>'
            '<form action="/system/backup/create" method="post">'
            '<input type="hidden" name="csrf" value="'+_esc(token)+'">'
            '<button type="submit">Create backup now</button></form></section>'
            +__import__('auto_backup').panel_html(config,_new_token(),ui)+
            '<section class="panel"><h2>Backup archives</h2>'
            '<p class="muted"><strong>Verify archive</strong> only checks the file (read-only). Restoring is a separate, confirmed step shown after a successful check.</p>'
            '<div class="scroll"><table><thead><tr><th>Created</th><th>Size</th><th>Actions</th></tr></thead><tbody>'
            +''.join(rows)+'</tbody></table></div></section>'
            '<section class="panel"><h2>Upload backup ZIP</h2><p>Check a Preludarr backup from your computer (16 MiB maximum). Uploading never starts a restore.</p>'
            '<form method="post" action="/system/backup/upload" enctype="multipart/form-data" class="bk-upload">'
            '<input type="hidden" name="csrf" value="'+_esc(_new_token())+'">'
            '<input type="file" name="archive" accept=".zip,application/zip" required aria-label="Backup ZIP file"> '
            '<button type="submit">Upload &amp; verify</button></form></section>'
            '<section class="panel"><h2>Restore</h2><p><strong>Restore is protected:</strong> it always needs a verified archive and your confirmation. '
            'Your media files are never changed; the library database (themes, posters, queue) and settings are replaced.</p>'
            '<details class="tm-help"><summary>How restore works</summary>'
            '<ol><li><strong>Verify archive</strong>, type RESTORE and choose <strong>Restore on next restart</strong>. Nothing changes yet, and you can still cancel.</li>'
            '<li>Restart the Preludarr container.</li>'
            '<li>Before anything else starts, Preludarr saves a safety copy of your current data (<code>system_backups/pre-restore-….tar</code>), '
            'restores the database and settings, and checks them. If any step fails, your previous data is put back automatically.</li>'
            '<li>The result is shown at the top of this page.</li></ol>'
            '<p>The running database is never overwritten; no helper outside the container and no access to Docker are needed.</p></details></section>'
            '<style>.bk-name{margin-top:4px;font-size:13px}.bk-name summary{cursor:pointer;color:#b4c4dc}.bk-name code{overflow-wrap:anywhere;font-size:12px}'
            '.bk-kind{display:inline-block;margin-left:6px;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:700;background:#33415c;color:#dce9ff}.bk-kind.auto{background:#1f4532;color:#bff0d2}'
            '.bk-actions{white-space:nowrap}.bk-actions .btn{margin:2px 4px 2px 0}.bk-upload{display:flex;gap:10px;flex-wrap:wrap;align-items:center}</style>')


_ROUTINE=re.compile(r'"(?:GET|HEAD) [^"]*" (?:[23]\d\d) ')
_LEVELS=('all','info','warning','error')


def _logs_page(config,query):
    try:
        import app_logs as logger
        files=logger._files(config)
        if not files:return '<section class="panel"><h2>Application logs</h2><p>No log files yet — Preludarr writes them as it runs.</p></section>'
        file=(query.get('file',['themearr.log'])[0])[:64]
        if not logger.permitted(file) or file not in {f[0] for f in files}:file=files[0][0]
        text=logger._read(config,file,limit=512*1024)
        q=(query.get('q',[''])[0])[:100]
        level=(query.get('level',['all'])[0]); level=level if level in _LEVELS else 'all'
        hide=query.get('routine',['hide'])[-1]!='show'  # form sends 'show' (hidden) + 'hide' (ticked box): last one wins
        auto=query.get('auto',['0'])[0]=='1'
        lines=text.splitlines()
        total=len(lines)
        if hide:lines=[ln for ln in lines if not _ROUTINE.search(ln)]
        if level!='all':
            want={'info':(' INFO ',' WARNING ',' ERROR ',' CRITICAL '),'warning':(' WARNING ',' ERROR ',' CRITICAL '),'error':(' ERROR ',' CRITICAL ')}[level]
            lines=[ln for ln in lines if any(w in ln for w in want)]
        if q:lines=[ln for ln in lines if q.lower() in ln.lower()]
        hidden=total-len(lines)
        lines=lines[-300:]
        def url(**kw):
            base={'tab':'logs','file':file,'q':q,'level':level,'routine':'hide' if hide else 'show','auto':'1' if auto else '0'}
            base.update(kw)
            return '/system?'+urlencode({k:v for k,v in base.items() if v not in ('',None)})
        nav=''.join('<a class="lg-file'+(' on' if n==file else '')+'" href="'+_esc(url(file=n))+'"'+(' aria-current="page"' if n==file else '')+'>'+_esc(n)+'</a>' for n,_,_ in files)
        body=('\n'.join(lines))
        refresh=('<meta http-equiv="refresh" content="30">' if auto else '')
        return (refresh+'<section class="panel"><h2>Application logs</h2>'
                '<p class="muted">Redacted rotating logs. Downloads may still show private paths — keep them private.</p>'
                '<nav class="lg-files" aria-label="Log files">'+nav+'</nav>'
                '<form method="get" action="/system" class="lg-filter"><input type="hidden" name="tab" value="logs"><input type="hidden" name="file" value="'+_esc(file)+'">'
                '<input type="hidden" name="auto" value="'+('1' if auto else '0')+'">'
                '<input name="q" type="search" maxlength="100" placeholder="Search log lines" value="'+_esc(q)+'" aria-label="Search log lines">'
                '<select name="level" aria-label="Severity">'+''.join('<option value="'+l+'"'+(' selected' if l==level else '')+'>'+{'all':'All levels','info':'Info and above','warning':'Warnings and errors','error':'Errors only'}[l]+'</option>' for l in _LEVELS)+'</select>'
                '<input type="hidden" name="routine" value="show"><label class="lg-check"><input type="checkbox" name="routine" value="hide"'+(' checked' if hide else '')+'> Hide routine successful requests</label>'
                '<button type="submit">Filter</button></form>'
                '<p class="lg-meta">Showing the last '+str(len(lines))+' matching line'+('' if len(lines)==1 else 's')+(' · '+str(hidden)+' hidden by filters' if hidden else '')+' · '
                '<a href="'+_esc(url(auto='0' if auto else '1'))+'">Auto-refresh: <strong>'+('On (every 30 s) — turn off' if auto else 'Off — turn on')+'</strong></a> · '
                '<a href="/logs/download?file='+quote(file)+'">Download redacted log</a></p>'
                +('<pre class="lg-pre" tabindex="0" aria-label="Log lines">'+_esc(body)+'</pre>' if lines else
                  '<p class="lg-empty">No log lines match these filters'+('' if not hide else ' — routine successful requests are hidden')+'.</p>')+
                '<style>.lg-files{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.lg-file{padding:6px 12px;border:1px solid #4a5f84;border-radius:8px;color:#cfe0f8!important;text-decoration:none!important}'
                '.lg-file.on{background:#2a3c5e;color:#fff!important;border-color:#6f8fc7}.lg-filter{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0}'
                '.lg-filter input[type=search]{flex:1 1 220px}.lg-check{display:flex;gap:6px;align-items:center;color:#cfe0f8}.lg-meta{color:#b4c4dc;font-size:14px}'
                '.lg-pre{font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;max-height:620px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;'
                'background:#0b1424;border:1px solid #344968;border-radius:8px;padding:14px}.lg-empty{padding:14px;border:1px dashed #4a5f84;border-radius:8px;color:#cfe0f8}</style>'
                '</section>')
    except (OSError,ValueError,ImportError):
        return '<section class="panel"><h2>Application logs</h2><p>Logs are unavailable right now. Try the <a href="/logs">classic log viewer</a>.</p></section>'


def _body(tab,config,query):
    if tab=='overview':return _cards(config)
    if tab=='activity':  # V1.9.6 (user): Activity as a System tab
        import sys as _sys
        app=_sys.modules.get('app') or _sys.modules.get('__main__')
        return _activity_page(app,query)
    if tab=='health':  # V1.9.5 (user): Library Health as a System tab
        import sys as _sys
        app=_sys.modules.get('app') or _sys.modules.get('__main__')
        return __import__('library_health').body(app,query)
    if tab=='backup':
        if query.get('_restore')==['1']:return _restore_preview(config,query)
        if query.get('_download'):return _download_confirm(config,query['_download'][0],(query.get('message',[''])[0])[:160])
        return _backup_page(config,(query.get('message',[''])[0])[:110])
    if tab=='logs':return _logs_page(config,query)
    if tab=='releases':
        # Preludarr 1.12: "Updates" (update-available check), then current release, what's new and how to update
        upd=__import__('updates').panel_html(__import__('login').APP_VERSION,(query.get('update_msg',[''])[0])[:200])
        return upd+__import__('release_notes').body(config)
    return ('<section class="panel"><h2>Advanced Settings</h2>'
            '<p class="muted">Preludarr has no separate advanced options yet. These existing settings cover the system-level choices:</p>'
            '<ul class="adv-links">'
            '<li><a href="/settings?section=host">Host</a> — instance name and allowed LAN addresses</li>'
            '<li><a href="/settings?section=remote">Application URL</a> — public HTTPS address (Cloudflare tunnel / reverse proxy)</li>'
            '<li><a href="/settings?section=security">API key &amp; security</a> — API key, trusted proxies</li>'
            '<li><a href="/settings?section=logging">Logging</a> — log level, size limit, old logs kept</li>'
            '<li><a href="/settings?section=recycle">Recycle bin</a> — how long removed theme files are kept</li>'
            '<li><a href="/media-management#permissions">Permissions</a> — owner, group and modes for files Preludarr creates</li></ul>'
            '<style>.adv-links{line-height:2}</style></section>')


def page(config,query=None):
    query=query or {}
    tab=(query.get('tab',['overview'])[0])[:20]
    if tab not in {'overview','backup','logs','releases','advanced','health','activity'}:tab='overview'
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Preludarr — System</title>''' + ('<meta http-equiv="refresh" content="5">' if query.get('_status')==['1'] else '') + '''<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{background:#101827;color:#eff4ff;font:15px system-ui,-apple-system,Segoe UI,sans-serif;margin:0}main{max-width:1100px;margin:0 auto;padding:26px 20px 60px}
h1{font-size:1.8rem}h2{font-size:1.25rem}.muted{color:#b7c7df;line-height:1.55}a{color:#aacaff;text-decoration:none}a:hover{text-decoration:underline}
.tabs{display:flex;flex-wrap:wrap;gap:6px;border-bottom:1px solid #39496b;padding:15px 0;margin-bottom:20px}.tabs a{padding:12px 16px;border-radius:8px 8px 0 0;color:#c5d8f6}.tabs .active{background:#294569;color:#fff;font-weight:750}
.panel,.tile,.card{background:#1b2942;border:1px solid #3a5279;border-radius:12px;padding:21px;margin:16px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}.tile{margin:0}.tile h3{margin-top:0}.card{margin:17px 0}.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.step{background:#172137;border:1px solid #3d5172;border-radius:8px;padding:13px}.warn{border-left:3px solid #dfa64a;background:#2d2930;padding:11px 14px;border-radius:6px}.table-scroll{overflow-x:auto}.card code{overflow-wrap:anywhere}
button,.button{background:#427bea;color:#fff;border:0;border-radius:7px;padding:11px 17px;cursor:pointer;font-weight:700;display:inline-block}button:hover,.button:hover{background:#5a8fff;text-decoration:none}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px;border-bottom:1px solid #40506d}.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:620px;overflow:auto;background:#0d1729;border:1px solid #344968;border-radius:8px;padding:16px;font-size:12px;line-height:1.6}
.logfiles{display:flex;flex-wrap:wrap;gap:10px}input{padding:10px;background:#0d1728;border:1px solid #52668b;border-radius:6px;color:white;max-width:100%}.message{padding:12px;border-left:3px solid #79b7ff;background:#263751;border-radius:6px}
@media(max-width:600px){main{padding:15px}.panel{padding:14px}.tabs a{padding:10px 11px}}
body{display:flex}@media(max-width:700px){body{display:block}}body>main{flex:1;min-width:0}
</style></head><body>''' + __import__('login').full_sidebar('activity' if tab=='activity' else 'system') + '''<main><h1>System</h1>
<p class="muted">Backups, diagnostics, releases and system configuration in one place.</p>''' + _nav(tab) + _body(tab,config,query) + '</main></body></html>'


def get_route(handler,parsed,config):
    if parsed.path == '/system/backup/status':  # old link from 1.11 and earlier
        return _redirect(handler,'/system?tab=backup')
    if parsed.path == '/system/backup/verify':
        try:
            q = parse_qs(parsed.query, max_num_fields=3)
        except ValueError:
            return _send(handler, 400, 'Invalid backup query', 'text/plain; charset=utf-8')
        return _send(handler, 200, page(config, {'tab': ['backup'], '_restore': ['1'], 'name': q.get('name', [''])}))
    if parsed.path=='/system/backup/download':
        # V1.9.14: backups hold tokens/keys → ask for the password again before the file is sent (old links show this form).
        query=parse_qs(parsed.query,max_num_fields=3)
        name=query.get('name',[''])[0]
        if not FILENAME.fullmatch(name):return _send(handler,400,'Invalid backup name','text/plain; charset=utf-8')
        return _send(handler,200,page(config,{'tab':['backup'],'_download':[name]}))
    try: q=parse_qs(parsed.query,keep_blank_values=True,max_num_fields=8)
    except ValueError:return _send(handler,400,'Bad query','text/plain; charset=utf-8')
    return _send(handler,200,page(config,q))


_DL_FAILS=[]


def _download_confirm(config,name,message=''):
    """V1.9.14: password step before a backup download (backups contain tokens and API keys)."""
    ok=bool(FILENAME.fullmatch(name)) and any(f[0]==name for f in _files(config))
    if not ok:return '<section class="panel"><h2>Backup not found</h2><p><a href="/system?tab=backup">Back to Backup &amp; Restore</a></p></section>'
    return ('<section class="panel" id="bk-download"><h2>Download backup</h2>'
            +('<p class="message" role="alert">'+_esc(message)+'</p>' if message else '')+
            '<p><code>'+_esc(name)+'</code></p>'
            '<p>This file contains Preludarr\u2019s settings, including saved tokens and API keys. Enter your password to download it, and keep the file private.</p>'
            '<form method="post" action="/system/backup/download" autocomplete="off"><input type="hidden" name="csrf" value="'+_esc(_new_token())+'">'
            '<input type="hidden" name="name" value="'+_esc(name)+'">'
            '<label>Password <input type="password" name="password" required autocomplete="current-password" maxlength="256"></label> '
            '<button type="submit">Download</button> <a class="btn" href="/system?tab=backup">Cancel</a></form></section>'
            '<style>#bk-download label{display:inline-flex;gap:8px;align-items:center;flex-wrap:wrap}#bk-download input[type=password]{padding:9px 10px;'
            'background:#0d1626;color:#eef4ff;border:1px solid #3e5174;border-radius:8px}</style>')


def _download_post(handler,config,body):
    name=body.get('name',[''])[0]
    if not FILENAME.fullmatch(name):return _send(handler,400,'Invalid backup name','text/plain; charset=utf-8')
    back=lambda msg:_redirect(handler,'/system/backup/download?'+urlencode({'name':name,'message':msg}))
    if not _consume_token(body.get('csrf',[''])[0]):return back('The form expired. Enter your password again.')
    now=time.monotonic()
    with LOCK:
        _DL_FAILS[:]=[t for t in _DL_FAILS if now-t<300]
        if len(_DL_FAILS)>=5:return back('Too many wrong passwords. Try again in five minutes.')
    app=sys.modules.get('app') or sys.modules.get('__main__')
    auth=getattr(app,'auth',None)
    if auth is None or not auth.verify(body.get('password',[''])[0]):
        with LOCK:_DL_FAILS.append(now)
        return back('Wrong password — the backup was not downloaded.')
    return _stream_backup(handler,config,name)


def _redirect(handler,location):
    handler.send_response(303);handler.send_header('Location',location);handler.send_header('Cache-Control','no-store')
    handler.send_header('Content-Length','0');handler.end_headers()


def _stream_backup(handler,config,name):
        try:
            root=_backup_dir(config)
            path=root/name
            if path.is_symlink() or not path.is_file() or path.stat().st_size>MAX_ZIP_BYTES:
                raise ValueError('Archive missing')
            # Avoid following symlinks at open time.
            flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
            with os.fdopen(os.open(path,flags),'rb') as src:
                size=os.fstat(src.fileno()).st_size
                handler.send_response(200)
                for k,v in [('Content-Type','application/zip'),('Content-Length',str(size)),
                            ('Content-Disposition','attachment; filename="'+name+'"'),('Cache-Control','no-store'),
                            ('X-Content-Type-Options','nosniff'),('Referrer-Policy','no-referrer')]:handler.send_header(k,v)
                handler.end_headers()
                shutil.copyfileobj(src,handler.wfile,128*1024)
                return
        except (OSError,ValueError):return _send(handler,404,'Archive unavailable','text/plain; charset=utf-8')


def post_route(handler,config):
    if handler.path.split('?',1)[0] == '/system/backup/upload':
        try:
            name = _upload_backup(config, handler)
            handler.send_response(303)
            handler.send_header('Location','/system/backup/verify?name='+quote(name))
            handler.send_header('Cache-Control','no-store')
            handler.send_header('Content-Length','0')
            handler.end_headers()
        except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, UnicodeError):
            _send(handler,400,'Backup upload rejected','text/plain; charset=utf-8')
        return
    try:
        if handler.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/x-www-form-urlencoded':
            raise ValueError('Invalid content type')
        size=int(handler.headers.get('Content-Length','0'))
        if size<=0 or size>4096:raise ValueError('Invalid form length')
        body=parse_qs(handler.rfile.read(size).decode('utf-8'),max_num_fields=5,strict_parsing=True)
        if handler.path.split('?',1)[0] == '/system/backup/download':  # V1.9.14 password step
            return _download_post(handler,config,body)
        if handler.path.split('?',1)[0] == '/system/backup/prepare':
            name = _prepare_restore(config, body)
            message = 'Restore scheduled: restart the container to restore ' + name + '.'
        elif handler.path.split('?',1)[0] == '/system/backup/cancel':
            if not _consume_token(body.get('csrf',[''])[0]):raise ValueError('Expired or invalid confirmation token')
            with LOCK:restore.cancel(_safe_cfg(config))
            message = 'Restore cancelled. Nothing was changed.'
        elif handler.path.split('?',1)[0] == '/system/backup/schedule':  # V1.9.13 automatic backups
            if not _consume_token(body.get('csrf',[''])[0]):raise ValueError('Expired or invalid confirmation token')
            ab=__import__('auto_backup')
            d=ab.save_settings(config, body.get('enabled',[''])[0]=='1', body.get('interval_days',[''])[0], body.get('retention_days',[''])[0])
            message=('Automatic backups on: every %d day%s, kept %d days.'%(d['interval_days'],'' if d['interval_days']==1 else 's',d['retention_days'])) if d['enabled'] else 'Automatic backups are off.'
        else:
            token=body.get('csrf',[''])[0]
            if not _consume_token(token):raise ValueError('Expired or invalid confirmation token')
            name=create_backup(config)
            message='Backup created: '+name
        handler.send_response(303)
        handler.send_header('Location','/system?tab=backup&message='+quote(message))
        handler.send_header('Cache-Control','no-store')
        handler.send_header('Content-Length','0')
        handler.end_headers()
    except (ValueError,OSError,sqlite3.Error,zipfile.BadZipFile) as e:
        _send(handler,400,'Backup not created: '+_esc(str(e)),'text/plain; charset=utf-8')
