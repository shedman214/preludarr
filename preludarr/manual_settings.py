# SPDX-License-Identifier: GPL-3.0-or-later
"""Live opt-in for user-supplied audio; environment remains the initial default."""
import json
import os
import tempfile
from pathlib import Path

FILE = 'preludarr_manual_installs.json'
URL = '/download-clients?tab=engines&cookies=1#manual-installs'


def enabled():
    path = Path(os.getenv('CONFIG_DIR', '/config')) / FILE
    try:
        if not path.is_symlink():
            value = json.loads(path.read_text()).get('enabled')
            if isinstance(value, bool):
                return value
    except (OSError, ValueError, AttributeError):
        pass
    return os.getenv('THEMEARR_MANUAL_INSTALL_ENABLED',
                     os.getenv('SPLICARR_MANUAL_INSTALL_ENABLED',
                               os.getenv('PRELUDARR_MANUAL_INSTALL_ENABLED', '0'))) == '1'


def save(value):
    directory = Path(os.getenv('CONFIG_DIR', '/config'))
    fd, name = tempfile.mkstemp(prefix='.manual-installs-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'enabled': bool(value)}, stream)
        os.replace(name, directory / FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def notice():
    return ['<div class="panel"><p><strong>Manual installs are disabled.</strong></p>',
            '<p>To use YouTube links or upload your own audio, open ',
            '<a href="/download-clients?tab=engines&amp;cookies=1#manual-installs">',
            'Settings → Download Clients → Download engines → YouTube → Configure</a> ',
            'and turn on <strong>Allow manual installs</strong>. Save the setting, then reload this page. ',
            'No container restart is needed. You will still review and confirm before installing audio.</p></div>']


def panel():
    return ('<section id="manual-installs"><h3>Manual installs</h3>'
            '<p>Allow YouTube links and custom audio uploads in the Theme Browser and Collections. '
            'Each install still requires your review and confirmation. This does not turn on automatic downloads.</p>'
            '<form method="post" action="/download-clients/manual-installs">'
            '<input type="hidden" name="save" value="1">'
            '<label><input type="checkbox" name="enabled" value="1"' +
            (' checked' if enabled() else '') + '> Allow manual installs</label>'
            '<p><button type="submit">Save manual installs</button></p></form>'
            '<p class="muted">Applies immediately. The saved choice overrides the Docker manual-install default.</p></section>')


def post(handler, redirect):
    import youtube_manual
    try:
        form = youtube_manual.read_form(handler)
        if (set(form) - {'enabled', 'save'} or form.get('save') != '1'
                or form.get('enabled', '0') not in ('0', '1')):
            raise ValueError('Invalid setting')
        save(form.get('enabled') == '1')
    except (OSError, ValueError):
        return handler.send(400, 'Manual-install setting could not be saved.', 'text/plain')
    return redirect(handler, URL)
