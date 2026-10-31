# SPDX-License-Identifier: GPL-3.0-or-later
"""Plain-language connection errors (Themearr V1.9.2).

User request 2026-10-26: "add error messages so the user knows why it wont connect. there was no error
messages and i did not know it did not connect". explain() turns any exception from a Sonarr / Radarr /
Plex / Jellyfin / Emby request into (reason, what_to_do). Never includes API keys or response bodies.
"""
import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse

KEY_HELP = {
    'Sonarr': 'Copy it from Sonarr → Settings → General → Security → API Key.',
    'Radarr': 'Copy it from Radarr → Settings → General → Security → API Key.',
    'Plex': 'Use a current Plex token (X-Plex-Token) for an account that can see this server.',
    'Jellyfin': 'Create one in Jellyfin → Dashboard → API Keys.',
    'Emby': 'Create one in Emby → Settings → Advanced → API Keys.',
}


class WrongApp(ValueError):
    """The address answered, but it is a different program (e.g. Radarr at the Sonarr address)."""


def _host(url):
    try:
        p = urllib.parse.urlsplit(url or '')
        return p.netloc or (url or '')
    except ValueError:
        return url or ''


def _root(error):
    """Unwrap URLError → the underlying socket / SSL error."""
    seen = 0
    while isinstance(error, urllib.error.URLError) and not isinstance(error, urllib.error.HTTPError) and seen < 4:
        reason = error.reason
        if not isinstance(reason, BaseException):
            break
        error, seen = reason, seen + 1
    return error


def explain(error, app='Sonarr', url=''):
    """Return (reason, what_to_do) — both short, plain sentences safe to show in the page."""
    host = _host(url)
    where = (' at ' + host) if host else ''
    if isinstance(error, WrongApp):
        return str(error), 'Check the port — each program has its own (Sonarr usually 8989, Radarr 7878).'
    if isinstance(error, urllib.error.HTTPError):
        code = error.code
        if code in (401, 403):
            return app + ' rejected the API key (HTTP %d).' % code, KEY_HELP.get(app, 'Check the API key.')
        if code == 404:
            return ('Something answered' + where + ', but not the ' + app + ' API (HTTP 404).',
                    'Check the port, and add the URL base if ' + app + ' uses one (e.g. http://host:port/' + app.lower() + ').')
        if code in (301, 302, 303, 307, 308):
            return ('The address' + where + ' redirects somewhere else (HTTP %d).' % code,
                    'Use the final address directly — often https:// or a URL base — or the plain LAN address.')
        if code == 407:
            return 'A proxy asked for a login (HTTP 407).', 'Connect to ' + app + ' directly on your LAN.'
        if 500 <= code < 600:
            return app + ' answered with a server error (HTTP %d).' % code, 'Check that ' + app + ' is healthy (its own logs), then try again.'
        return app + ' answered with HTTP %d.' % code, 'Check the address and API key.'
    root = _root(error)
    if isinstance(root, ssl.SSLCertVerificationError):
        return ('The HTTPS certificate' + where + ' is not trusted (' + (getattr(root, 'verify_message', '') or 'verification failed') + ').',
                'Use an address with a real certificate (e.g. via your reverse proxy), or plain http:// with the LAN IP.')
    if isinstance(root, ssl.SSLError):
        return ('HTTPS failed' + where + ' — the port does not seem to speak HTTPS.',
                'Use http:// for this port, or the HTTPS port/address of ' + app + '.')
    if isinstance(root, (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionResetError)):
        return ('The connection' + where + ' was closed without a proper answer.',
                'If the address starts with http://, the port may expect https:// (or the reverse).')
    if isinstance(root, ConnectionRefusedError):
        return ('Nothing is answering' + where + ' (connection refused).',
                'Check the port number and that ' + app + ' is running.')
    if isinstance(root, (socket.timeout, TimeoutError)):
        return ('No answer' + where + ' — the connection timed out.',
                'Check the IP address, that the server is switched on, and that it is on the same network.')
    if isinstance(root, socket.gaierror):
        return 'The host name' + where + ' could not be found.', 'Check the spelling, or use the server’s IP address.'
    if isinstance(root, OSError) and getattr(root, 'errno', None) in (113, 101, 65, 51):  # host/network unreachable
        return 'The server' + where + ' cannot be reached from Preludarr.', 'Check the IP address and that both are on the same network.'
    if isinstance(root, json.JSONDecodeError):
        return ('Something answered' + where + ', but it is not the ' + app + ' API.',
                'Check the port — it may be another program’s web page.')
    if isinstance(root, ValueError) and str(root):
        return str(root), 'Check the address.'
    return ('Could not connect' + where + ' (' + type(root).__name__ + ').',
            'Check the address, port and API key, then use Test again.')


def text(error, app='Sonarr', url=''):
    reason, todo = explain(error, app, url)
    return reason + ' ' + todo
