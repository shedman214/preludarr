# SPDX-License-Identifier: GPL-3.0-or-later
"""Fake Plex / Jellyfin / Emby for the Collections tests. Usage: python3 fake_media_server.py PORT LOGFILE

Plex token 'plextoken'; Jellyfin token 'jftoken' (Authorization header); Emby token 'embytoken' (X-Emby-Token, under /emby).
Every write (theme upload, theme delete, refresh) is appended to LOGFILE as one JSON line.
"""
import hashlib
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG = int(sys.argv[1]), sys.argv[2]
MOVIES_TAIL = '/'.join([x for x in (sys.argv[3] if len(sys.argv) > 3 else '/data/movies').split('/') if x][-2:])  # Plex sees the same folder under its own mount
PIN = {'checks': 0}
QC = {'checks': 0, 'enabled': True}


def plex_movie(title, year, folder):
    return {'title': title, 'year': year, 'Media': [{'Part': [{'file': '/data/movies/' + folder + '/' + title + '.mkv'}]}]}


PLEX = {
    '/library/sections': {'MediaContainer': {'Directory': [{'key': '1', 'type': 'movie', 'title': 'Movies', 'Location': [{'path': '/plexmedia/' + MOVIES_TAIL}]},
                                                         {'key': '2', 'type': 'show', 'title': 'TV'},
                                                         {'key': '3', 'type': 'movie', 'title': '4K Movies', 'Location': [{'path': '/plexmedia/movies/4k_movies'}]}]}},
    '/library/sections/3/collections': {'MediaContainer': {'Metadata': [{'ratingKey': '601', 'title': 'The Matrix Collection', 'childCount': 1}]}},
    '/library/collections/601/children': {'MediaContainer': {'Metadata': [plex_movie('The Matrix', 1999, 'The Matrix (1999) 4K')]}},
    '/library/sections/1/collections': {'MediaContainer': {'Metadata': [
        {'ratingKey': '501', 'title': 'Bleach Collection', 'childCount': 3, 'thumb': '/library/collections/501/composite/1700000000'},
        {'ratingKey': '502', 'title': 'The Matrix Collection', 'childCount': 2, 'theme': '/library/metadata/502/theme/1700000000'},
        {'ratingKey': '503', 'title': 'Unknown Collection', 'childCount': 1}]}},
    '/library/collections/501/children': {'MediaContainer': {'Metadata': [
        plex_movie('Bleach the Movie: Fade to Black', 2008, 'Bleach - Fade to Black (2008)'),
        plex_movie('Bleach the Movie: Memories of Nobody', 2006, 'Bleach - Memories of Nobody (2006)'),
        plex_movie('Bleach the Movie: The DiamondDust Rebellion', 2007, 'Bleach - The DiamondDust Rebellion (2007)')]}},
    '/library/collections/502/children': {'MediaContainer': {'Metadata': [
        plex_movie('The Matrix', 1999, 'The Matrix (1999)'), plex_movie('The Matrix Reloaded', 2003, 'The Matrix Reloaded (2003)')]}},
    '/library/collections/503/children': {'MediaContainer': {'Metadata': [plex_movie('Not In Library', 2010, 'Not In Library (2010)')]}},
}
JF_SETS = {'Items': [{'Id': 'abc123', 'Name': 'Bleach Collection', 'Path': '/config/data/collections/Bleach Collection [boxset]', 'ImageTags': {'Primary': 'tag1'}}]}
JPEG = b'\xff\xd8\xff\xe0' + b'J' * 2000
PNG = b'\x89PNG\r\n\x1a\n' + b'P' * 2000
JF_KIDS = {'Items': [{'Name': 'Bleach the Movie: Memories of Nobody', 'ProductionYear': 2006, 'Path': '/movies/Bleach - Memories of Nobody (2006)/m.mkv'},
                     {'Name': 'Bleach the Movie: Fade to Black', 'ProductionYear': 2008, 'Path': '/movies/Bleach - Fade to Black (2008)'}]}


def log(entry):
    with open(LOG, 'a') as f:
        f.write(json.dumps(entry) + '\n')


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _who(self):
        if self.headers.get('X-Plex-Token') in ('plextoken', 'plextoken2'):
            return 'plex'
        if 'Token="jftoken"' in (self.headers.get('Authorization') or ''):
            return 'jellyfin'
        if self.headers.get('X-Emby-Token') == 'embytoken':
            return 'emby'
        return None

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body))); self.end_headers()
        self.wfile.write(body)

    def _raw(self, data, ctype):
        self.send_response(200); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    def _ok(self):
        self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()

    def _plextv(self, method, path):
        """Fake plex.tv sign-in: the PIN is approved on the second status check."""
        if not self.headers.get('X-Plex-Client-Identifier') or self.headers.get('X-Plex-Product') != 'Preludarr':
            return self._json({'error': 'client headers missing'}, 400)
        if method == 'POST' and path == '/api/v2/pins':
            log({'server': 'plextv', 'op': 'pin'})
            return self._json({'id': 4242, 'code': 'abcd1234wxyz'})
        if method == 'GET' and path == '/api/v2/pins/4242':
            PIN['checks'] += 1
            return self._json({'id': 4242, 'authToken': 'accounttoken' if PIN['checks'] >= 2 else None})
        if method == 'GET' and path == '/api/v2/resources':
            if self.headers.get('X-Plex-Token') != 'accounttoken':
                return self._json({'error': 'unauthorized'}, 401)
            return self._json([
                {'name': 'Living Room Plex', 'clientIdentifier': 'machine-main', 'provides': 'server', 'owned': True, 'accessToken': 'plextoken', 'connections': [
                    {'uri': 'https://10-0-0-5.abcdef.plex.direct:32400', 'local': False, 'address': '10.0.0.5', 'port': 32400, 'protocol': 'https'},
                    {'uri': f'http://127.0.0.1:{PORT}', 'local': True, 'address': '127.0.0.1', 'port': PORT, 'protocol': 'http'}]},
                {'name': 'Second Plex', 'clientIdentifier': 'machine-two', 'provides': 'server', 'owned': True, 'accessToken': 'plextoken2', 'connections': [
                    {'uri': f'http://127.0.0.1:{PORT}', 'local': True, 'address': '127.0.0.1', 'port': PORT, 'protocol': 'http'}]},
                {'name': 'Living room TV', 'provides': 'client,player', 'accessToken': 'x', 'connections': []},
                {'name': 'Friend server', 'provides': 'server', 'owned': False, 'accessToken': '', 'connections': []}])
        return self._json({'error': 'not found'}, 404)

    def _qc(self, method, path, body=b''):
        """Fake Jellyfin Quick Connect: the code is approved on the second Connect check."""
        if 'Client="Preludarr"' not in (self.headers.get('Authorization') or ''):
            return self._json({'error': 'no client header'}, 400)
        if method == 'GET' and path == '/QuickConnect/Enabled':
            return self._json(QC['enabled'])
        if method == 'POST' and path == '/QuickConnect/Initiate':
            return self._json({'Secret': 'qcsecret0123456789', 'Code': '482913'}) if QC['enabled'] else self._json({'error': 'off'}, 401)
        if method == 'GET' and path == '/QuickConnect/Connect':
            QC['checks'] += 1
            return self._json({'Authenticated': QC['checks'] >= 2, 'Secret': 'qcsecret0123456789', 'Code': '482913'})
        if method == 'POST' and path == '/Users/AuthenticateWithQuickConnect':
            if json.loads(body or b'{}').get('Secret') != 'qcsecret0123456789':
                return self._json({'error': 'bad secret'}, 400)
            return self._json({'AccessToken': 'jftoken', 'User': {'Name': 'jfadmin', 'Policy': {'IsAdministrator': True}}})
        return self._json({'error': 'not found'}, 404)

    def do_GET(self):
        if self.path.startswith('/QuickConnect/'):
            return self._qc('GET', urllib.parse.urlsplit(self.path).path)
        if self.path.startswith('/api/v2/'):
            return self._plextv('GET', urllib.parse.urlsplit(self.path).path)
        who, url = self._who(), urllib.parse.urlsplit(self.path)
        if who is None:
            return self._json({'error': 'unauthorized'}, 401)
        path = url.path[5:] if who == 'emby' and url.path.startswith('/emby/') else url.path
        q = urllib.parse.parse_qs(url.query)
        if who == 'plex' and path == '/library/metadata/501':
            return self._json({'MediaContainer': {'Metadata': [{'ratingKey': '501', 'title': 'Bleach Collection', 'theme': '/library/metadata/501/theme/1800000000'}]}})
        if who == 'plex' and path == '/library/metadata/501/theme/1800000000':
            return self._raw(b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'T' * 3000, 'audio/mpeg')
        if who == 'plex' and path in PLEX:
            if path.endswith('/children'):
                log({'server': who, 'op': 'children', 'path': path})
            return self._json(PLEX[path])
        if who == 'plex' and path == '/photo/:/transcode' and q.get('url') == ['/library/collections/501/composite/1700000000']:
            log({'server': who, 'op': 'poster', 'path': path})
            return self._raw(JPEG, 'image/jpeg')
        if who in ('jellyfin', 'emby') and path == '/Items/abc123/Images/Primary':
            log({'server': who, 'op': 'poster', 'path': path})
            return self._raw(PNG, 'image/png')
        if who in ('jellyfin', 'emby') and path == '/System/Info':
            return self._json({'ServerName': 'Fake ' + who})
        if who in ('jellyfin', 'emby') and path == '/Items':
            if q.get('IncludeItemTypes') == ['BoxSet']:
                return self._json(JF_SETS)
            if q.get('ParentId') == ['abc123']:
                return self._json(JF_KIDS)
        self._json({'error': 'not found'}, 404)

    def do_POST(self):
        if self.path == '/emby/Users/AuthenticateByName':
            n = int(self.headers.get('Content-Length', '0') or 0)
            body = json.loads(self.rfile.read(n) or b'{}')
            if 'Client="Preludarr"' not in (self.headers.get('X-Emby-Authorization') or ''):
                return self._json({'error': 'no client header'}, 400)
            users = {'embyadmin': ('correct-pass', True), 'embyuser': ('user-pass', False)}
            if body.get('Username') in users and users[body['Username']][0] == body.get('Pw'):
                return self._json({'AccessToken': 'embytoken', 'User': {'Name': body['Username'], 'Policy': {'IsAdministrator': users[body['Username']][1]}}})
            return self._json({'error': 'bad credentials'}, 401)
        if self.path.startswith('/QuickConnect/') or self.path.startswith('/Users/AuthenticateWithQuickConnect'):
            n = int(self.headers.get('Content-Length', '0') or 0)
            return self._qc('POST', urllib.parse.urlsplit(self.path).path, self.rfile.read(n) if n else b'')
        if self.path.startswith('/api/v2/'):
            return self._plextv('POST', urllib.parse.urlsplit(self.path).path)
        who, path = self._who(), urllib.parse.urlsplit(self.path).path
        n = int(self.headers.get('Content-Length', '0') or 0)
        body = self.rfile.read(n) if n else b''
        if who is None:
            return self._json({'error': 'unauthorized'}, 401)
        if who == 'plex' and path.endswith('/themes'):
            log({'server': who, 'op': 'upload', 'path': path, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
                 'content_type': self.headers.get('Content-Type')})
            return self._ok()
        if path.endswith('/Refresh'):
            log({'server': who, 'op': 'refresh', 'path': path})
            return self._ok()
        self._json({'error': 'not found'}, 404)

    def do_DELETE(self):
        who, path = self._who(), urllib.parse.urlsplit(self.path).path
        if who == 'plex' and path.endswith('/theme'):
            log({'server': who, 'op': 'delete', 'path': path})
            return self._ok()
        self._json({'error': 'not found'}, 404)


ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
