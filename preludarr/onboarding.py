# SPDX-License-Identifier: GPL-3.0-or-later
"""Dashboard "Getting started" box (1.12, pre-release #15 fresh-install test: a new install showed "0 titles" and no next step).
Shown while there is no root folder; afterwards only a short tip if Shows/Movies root folders exist but no free theme source is on."""


def box(app):
    kinds = __import__('kinds')
    roots = list(app.ROOTS)
    if not roots:
        return ('<section class="panel" id="getting-started"><h2>Getting started</h2><ol>'
                '<li><strong>Add your library folders</strong> in <a href="/media-management">Settings → Media Management</a> → <em>Add Root Folder</em>. '
                'Pick the folder that holds one sub-folder per series or movie, and choose its type: Anime, Anime Movies, Shows or Movies.</li>'
                '<li><strong>Shows and Movies:</strong> turn on the free sources <em>Plex TV Themes</em> and <em>ThemerrDB</em> in '
                '<a href="/download-clients?tab=sources">Settings → Download Clients</a>, and add your own TMDB key in <a href="/media-servers">Settings → Connect</a> '
                'so Preludarr can find each title’s ID. Anime needs nothing extra: it uses AnimeThemes.</li>'
                '<li><strong>Install themes:</strong> open a title in the Library to preview and install, use the <a href="/queue">Theme Queue</a> for many titles, '
                'or turn on <em>Automatic Themes</em> in Media Management.</li></ol>'
                '<p class="muted">Optional: connect Plex, Jellyfin or Emby in <a href="/media-servers">Settings → Connect</a> for collections and refreshes.</p>'
                + CSS + '</section>')
    s = __import__('sources').settings(app.CONFIG)
    if any(kinds.is_regular(lib) for lib, _ in roots) and not (s['plex'] or s['themerrdb']):
        return ('<section class="panel" id="getting-started"><p><strong>Tip:</strong> your Shows and Movies have no theme source yet. Turn on '
                '<em>Plex TV Themes</em> and <em>ThemerrDB</em> in <a href="/download-clients?tab=sources">Settings → Download Clients</a> '
                '(and add a TMDB key in <a href="/media-servers">Settings → Connect</a>).</p>' + CSS + '</section>')
    return ''


CSS = ('<style>#getting-started{border-color:#4f8cff}#getting-started ol{margin:8px 0 0;padding-left:22px;line-height:1.55}'
       '#getting-started li{margin:6px 0}#getting-started p{margin:8px 0 0}</style>')
