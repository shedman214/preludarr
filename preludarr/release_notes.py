# SPDX-License-Identifier: GPL-3.0-or-later
"""System → Releases: the running version, what's new (from CHANGELOG.md) and how to update or go back.

Read-only. Works the same on any Docker host; the old server-specific release lineage and host audit panels were removed in 1.12.
"""
from pathlib import Path
import html
import re
import sqlite3
from contextlib import closing
from urllib.parse import quote

from login import APP_VERSION

_HERE = Path(__file__).resolve().parent
CHANGELOG = next((p for p in (_HERE / 'CHANGELOG.md', _HERE.parent / 'CHANGELOG.md') if p.is_file()), _HERE / 'CHANGELOG.md')  # image: /app; source tree: repo root
SHOWN = 6  # newest sections shown open; older ones sit under "Older releases"


def _stats(config):
    path = Path(config) / 'library.db'
    if not path.is_file():
        return {'library': 'Not found', 'posters': 'Not found'}
    try:
        with closing(sqlite3.connect('file:' + quote(str(path)) + '?mode=ro', uri=True, timeout=2)) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {key: (str(con.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]) if table in tables else 'Unavailable')
                    for table, key in (('shows', 'library'), ('posters', 'posters'))}
    except (sqlite3.Error, OSError, ValueError):
        return {'library': 'Unavailable', 'posters': 'Unavailable'}


def _inline(text):
    text = html.escape(text, quote=True)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    return re.sub(r'\[([^\]]+)\]\((https://[^)\s"]+)\)', r'<a href="\2" rel="noopener noreferrer" target="_blank">\1</a>', text)


def changelog_sections(text=None):
    """[(title, [bullet, …])] from the '## [x]' sections of CHANGELOG.md, newest first."""
    if text is None:
        try:
            text = CHANGELOG.read_text(encoding='utf-8')
        except OSError:
            return []
    sections = []
    for line in text.splitlines():
        if line.startswith('## '):
            sections.append((line[3:].strip().replace('[', '').replace(']', ''), []))
        elif sections and line.startswith('- '):
            sections[-1][1].append(line[2:].strip())
        elif sections and line.strip() and not line.startswith('#'):
            sections[-1][1].append(line.strip())
    return sections


def _section_html(title, items):
    return ('<h3>' + _inline(title) + '</h3><ul class="rl-notes">' + ''.join('<li>' + _inline(i) + '</li>' for i in items) + '</ul>')


def body(config):
    """Panels for System → Releases (below the Updates panel)."""
    info = _stats(config)
    sections = changelog_sections()
    notes = ''.join(_section_html(t, i) for t, i in sections[:SHOWN])
    older = ''.join(_section_html(t, i) for t, i in sections[SHOWN:])
    if older:
        notes += '<details class="tm-help"><summary>Older releases (' + str(len(sections) - SHOWN) + ')</summary>' + older + '</details>'
    return ('<section class="panel"><h2>Current release</h2><p class="rm-version">V' + html.escape(APP_VERSION) + '</p>'
            '<table><tr><th scope="row">Library titles</th><td>' + html.escape(info['library']) + '</td></tr>'
            '<tr><th scope="row">Poster records</th><td>' + html.escape(info['posters']) + '</td></tr></table></section>'
            '<section class="panel" id="whats-new"><h2>What’s new</h2>' + (notes or '<p class="muted">The changelog is not available in this build.</p>') + '</section>'
            '<section class="panel"><h2>Updating and going back</h2>'
            '<ol class="rl-steps"><li>On <a href="/system?tab=backup">Backup &amp; Restore</a>, choose <strong>Create backup now</strong>.</li>'
            '<li>Change the image tag of the Preludarr container to the new version (or pull the new image) and restart the container. '
            'Your settings, library and backups are kept in the <code>/config</code> folder.</li>'
            '<li>To go back, use the previous version’s image tag. If the newer version changed your data, restore the backup you made in step 1.</li></ol>'
            '<p class="muted">Preludarr never updates itself. The update notice above only tells you that a new version exists.</p></section>'
            '<style>.rm-version{font-size:2rem;font-weight:800;margin:4px 0 10px}.rl-notes{margin:6px 0 16px;padding-left:22px;line-height:1.6}'
            '.rl-notes code,.rl-steps code{font-size:12.5px}.rl-steps{padding-left:22px;line-height:1.7}#whats-new h3{margin:16px 0 4px;font-size:1.02rem}</style>')


def route(handler, config):
    """The old stand-alone /release-manager page now lives in System → Releases."""
    handler.send_response(303)
    handler.send_header('Location', '/system?tab=releases')
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Content-Length', '0')
    handler.end_headers()
