# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → Download Clients (Themearr V1.8.0): a real list of download clients, Sonarr-style cards.

Today there is one client type (YouTube via yt-dlp). The list and the "+ Add Download Client" dialog are built so
more sources can be added later without changing the page; types that don't exist yet are shown as "planned".
"""
import html
import shutil
import subprocess
import time
import urllib.parse

# key, display name, status
CLIENT_TYPES = (
    ('youtube', 'YouTube', 'available'),
)
_VERSION = {'value': None, 'at': 0.0}


def _e(value):
    return html.escape(str(value), quote=True)


def ytdlp_version():
    """yt-dlp version string (cached 10 min), or '' when missing."""
    if time.monotonic() - _VERSION['at'] < 600 and _VERSION['value'] is not None:
        return _VERSION['value']
    value = ''
    exe = shutil.which('yt-dlp')
    if exe:
        try:
            value = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=8).stdout.strip()[:40]
        except (OSError, subprocess.SubprocessError):
            value = ''
    _VERSION.update(value=value, at=time.monotonic())
    return value


YT_LOGO = ('<span class="brand-mark" aria-hidden="true" style="color:#ff3d3d"><svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
           '<path d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.5 3.6 12 3.6 12 3.6s-7.5 0-9.4.5A3 3 0 0 0 .5 6.2 31 31 0 0 0 0 12a31 31 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.9.5 9.4.5 9.4.5s7.5 0 9.4-.5a3 3 0 0 0 2.1-2.1A31 31 0 0 0 24 12a31 31 0 0 0-.5-5.8ZM9.6 15.6V8.4l6.3 3.6-6.3 3.6Z"/></svg></span>')


def limits_post(handler, redirect):
    """V1.9.14: save per-source rate limits (always clamped to the safe range)."""
    import ratelimit as rl
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 4096:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=20)
        if form.get('reset', [''])[0] == '1':
            rl.save({})
            msg = 'Rate limits reset to the safe defaults.'
        else:
            wanted = {k: form.get(k, [''])[0] for k in rl.SOURCES}
            saved = rl.save(wanted)
            lowered = [rl.SOURCES[k][0] for k in rl.SOURCES if str(wanted[k]).strip().isdigit() and int(wanted[k]) != saved[k]]
            msg = 'Rate limits saved.' + (' Adjusted to the safe range: ' + ', '.join(lowered) + '.' if lowered else '')
    except (ValueError, UnicodeError, OSError) as error:
        msg = 'Not saved: ' + (str(error) if isinstance(error, ValueError) else 'settings could not be written.')
    return redirect(handler, '/download-clients?' + urllib.parse.urlencode({'tab': 'limits', 'notice': msg[:200]}))


def page(app, handler):
    import media_servers
    import youtube_settings as cookies
    import youtube_ui
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    notice = q.get('notice', [''])[0][:200]
    version = ytdlp_version()
    try:
        st = cookies.status(app.CONFIG)
        cookie_text = ('Configured' + ('' if st['private'] else ' — permissions need attention')) if st['configured'] else 'Not configured (required for YouTube search)'
    except (OSError, ValueError):
        cookie_text = 'Unavailable'
    ready = bool(version)
    parts = [media_servers.settings_shell('Download Clients', 'downloads'),
             '</aside><main class="main"><div class="topline"><div class="crumb">Settings / <strong>Download Clients</strong></div></div>',
             '<div class="heading-row"><div><h1 class="heading">Download Clients</h1>',
             '<p class="muted">Sources Preludarr can use to fetch theme audio. Every download is reviewed before it is installed as theme.mp3.</p></div>',
             '<button type="button" class="btn primary" data-dc-open>＋ Add Download Client</button></div>']
    if notice:
        parts += ['<p class="notice" role="status">', _e(notice), '</p>']
    import sources as _src
    _on = _src.settings(app.CONFIG)
    dtab = q.get('tab', [''])[0] if q.get('tab', [''])[0] in ('engines', 'sources', 'limits') else 'engines'
    parts += ['<nav class="tm-subnav tm-tabs" aria-label="Download Clients">',
              ''.join('<a href="/download-clients?tab=' + k + '"' + (' class="on" aria-current="page"' if k == dtab else '') + '>' + v + '</a>'
                      for k, v in (('engines', 'Download engines'), ('sources', 'Theme sources'), ('limits', 'Rate limits'))), '</nav>',
              '<style>main .dc-sec{display:none}main #dc-sec-', dtab, '{display:block}</style>',
              '<section class="dc-sec" id="dc-sec-engines"><h2 id="dc-engines" class="dc-h">Download engines</h2><p class="muted">Programs that fetch audio for you. They only run when you choose a theme; every download is reviewed first.</p>',
              '<div class="grid dc-grid">',
              '<section class="connection" id="dc-youtube"><div class="conn-head">', YT_LOGO,
              '<div><div class="conn-name">YouTube</div><span class="tag ', 'on' if ready else '', '">', 'Ready' if ready else 'Not available', '</span></div></div>',
              '<div class="stats"><div>Downloads audio from a YouTube link you paste in the Theme Browser.</div>',
              '<div>Engine: <strong>', _e('yt-dlp ' + version if version else 'yt-dlp missing'), '</strong></div>',
              '<div>Cookies: <strong>', _e(cookie_text), '</strong></div>',
              '<div>Used by: <strong>Theme Browser → Download from YouTube</strong></div></div>',
              '<details class="dc-config"', ' open' if q.get('cookies') else '', '><summary class="btn ghost">Configure</summary>', youtube_ui.panel(app.CONFIG), '</details>',
              '</section></div>',
              '</section><section class="dc-sec" id="dc-sec-sources"><h2 id="dc-sources" class="dc-h">Theme sources</h2>',
              '<p class="muted">A root folder&#8217;s <strong>type</strong> decides where Preludarr looks for a theme, in this order. Each source is only asked when the ones before it '
              'found nothing. Plex TV Themes, ThemerrDB and YouTube search only suggest — you approve every install.</p>',
              '<div class="scroll"><table class="dc-order" id="dc-by-type"><thead><tr><th>Type</th><th>Sources, in order</th></tr></thead><tbody>',
              '<tr><td>Anime TV</td><td>AnimeThemes → Plex TV Themes → ThemerrDB</td></tr>',
              '<tr><td>Anime Movies</td><td>AnimeThemes → ThemerrDB</td></tr>',
              '<tr><td>Shows</td><td>Plex TV Themes → ThemerrDB → YouTube search (preview only)</td></tr>',
              '<tr><td>Movies</td><td>ThemerrDB → YouTube search (preview only)</td></tr>',
              '</tbody></table></div>',
              '<div class="scroll"><table class="dc-order"><thead><tr><th>Source</th><th>Finds</th><th>Needs</th><th>Status</th></tr></thead><tbody>',
              '<tr><td><a href="#dc-animethemes">AnimeThemes</a></td><td>Anime openings &amp; endings</td><td>Title match (Original Matching)</td><td><span class="tag on">Always on</span></td></tr>',
              '<tr><td><a href="#dc-plex">Plex TV Themes</a></td><td>TV series themes (MP3)</td><td>Exact TVDB id</td><td><span class="tag ', 'on' if _on['plex'] else 'off', '">', 'On' if _on['plex'] else 'Off', '</span></td></tr>',
              '<tr><td><a href="#dc-themerrdb">ThemerrDB</a></td><td>TV &amp; movie themes (YouTube links)</td><td>Exact TMDB id (movies: or IMDb) + YouTube engine</td><td><span class="tag ', 'on' if _on['themerrdb'] else 'off', '">', 'On' if _on['themerrdb'] else 'Off', '</span></td></tr>',
              '<tr><td><a href="#dc-youtube-search">YouTube search</a></td><td>Show &amp; movie themes — preview only</td><td>Your API key + YouTube cookies.txt</td><td><span class="tag ', 'on' if __import__('youtube_search').ready() else 'off', '">', 'On' if __import__('youtube_search').ready() else 'Off', '</span></td></tr>',
              '</tbody></table></div>',
              '<details class="tm-help"><summary>Where do the TVDB / TMDB ids come from?</summary><p class="muted">From Sonarr/Radarr (Settings → Connect) when connected and mapped, '
              'otherwise from an exact TMDB title + year match with a single result. Without an exact id these sources stay silent — a wrong id would mean another show’s music.</p></details>',
              '<div class="grid dc-grid">', __import__('sources').cards_html(app.CONFIG), '</div></section>',
              __import__('ratelimit').panel_html(),  # V1.9.14 (the page shows the save notice at the top)
              '<dialog id="dc-add" aria-labelledby="dc-add-title" style="width:min(560px,94vw);padding:0;border:1px solid #4a6181;border-radius:12px;background:#111b2d;color:#eaf2ff">',
              '<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;background:#14233e;border-bottom:1px solid #324158">',
              '<h2 id="dc-add-title" style="margin:0;font-size:1.05rem">Add Download Client</h2><button type="button" class="btn ghost" data-dc-close aria-label="Close">✕</button></div>',
              '<div style="padding:16px"><p class="muted" style="margin-top:0">Choose a download client type.</p><div class="dc-types">']
    for key, name, status in CLIENT_TYPES:
        added = key == 'youtube'  # YouTube is built in; one instance
        parts += ['<div class="dc-type">', YT_LOGO if key == 'youtube' else '', '<div><strong>', _e(name), '</strong><br><small class="muted">',
                  'Already added' if added else 'Available', '</small></div></div>']
    parts += ['<div class="dc-type dc-planned"><span class="brand-mark" aria-hidden="true">＋</span><div><strong>More sources</strong><br>',
              '<small class="muted">Planned — new download client types will appear here as they are added.</small></div></div>',
              '</div><div style="display:flex;justify-content:flex-end;margin-top:14px"><button type="button" class="btn ghost" data-dc-close>Close</button></div></div></dialog>',
              '<style>.dc-config{margin-top:12px}.dc-config summary{list-style:none;display:inline-block;cursor:pointer}',
              '.dc-config #youtube-settings{margin-top:12px}.dc-types{display:flex;flex-direction:column;gap:8px}',
              '.dc-type{display:flex;gap:12px;align-items:center;padding:10px;border:1px solid #3b4b6b;border-radius:9px}.dc-planned{opacity:.7}',
              '.dc-grid{grid-template-columns:repeat(auto-fill,minmax(300px,1fr))!important}.dc-grid .connection{min-height:0}.dc-grid .stats{margin:12px 0;line-height:1.6}',
              '.dc-h{margin:24px 0 4px}.dc-order th,.dc-order td{text-align:left;padding:8px 12px 8px 0;border-bottom:1px solid #2b3a55}.dc-order td:first-child{font-weight:800;width:60px}</style>',
              '<script>(function(){const d=document.getElementById("dc-add");document.querySelectorAll("[data-dc-open]").forEach(b=>b.addEventListener("click",()=>d.showModal()));',
              'd.querySelectorAll("[data-dc-close]").forEach(b=>b.addEventListener("click",()=>d.close()));})();</script>',
              '</main></div></body></html>']
    return handler.send(200, ''.join(parts), 'text/html')
