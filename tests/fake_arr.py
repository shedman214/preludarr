# SPDX-License-Identifier: GPL-3.0-or-later
"""Stand-in Sonarr + Radarr API (v3) for tests. Usage: python3 fake_arr.py PORT  (API key: testkey123)"""
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
KEY = 'testkey123'
SONARR = {'status': {'appName': 'Sonarr', 'version': '4.0.99.1'}, 'root': [{'path': '/data/tv/anime'}],
          'naming': {'seriesFolderFormat': '{Series TitleYear}', 'seasonFolderFormat': 'Season {season:00}', 'animeEpisodeFormat': '{Series Title} - S{season:00}E{episode:00}'},
          'items': [{'id': 1, 'title': 'Romelia War Chronicle', 'year': 2026, 'path': '/data/tv/anime/Romelia War Chronicle', 'added': '2026-10-25T10:00:00Z', 'statistics': {'episodeFileCount': 3}},
                    {'id': 2, 'title': 'Koupen-chan', 'year': 2025, 'path': '/data/tv/anime/Koupen-chan (2025)', 'added': '2024-01-02T10:00:00Z', 'statistics': {'episodeFileCount': 9}},
                    {'id': 3, 'title': 'Brand New Show', 'year': 2026, 'path': '/data/tv/anime/Brand New Show (2026)', 'added': '2026-10-26T01:00:00Z', 'statistics': {'episodeFileCount': 0}},
                    {'id': 4, 'title': 'Live Action Thing', 'year': 2020, 'path': '/data/tv_shows/tv/Live Action Thing (2020)', 'added': '2026-10-01T01:00:00Z', 'statistics': {}},
                    {'id': 6, 'title': 'Breaking Bad', 'year': 2008, 'path': '/data/tv/anime/Breaking Bad (2008)', 'added': '2026-10-20T01:00:00Z', 'tvdbId': 81189, 'statistics': {'episodeFileCount': 1}},
                    {'id': 5, 'title': 'Sneaky', 'year': 2026, 'path': '/data/tv/anime/../escape', 'added': '2026-10-26T01:00:00Z', 'statistics': {}}]}
RADARR = {'status': {'appName': 'Radarr', 'version': '5.9.1'}, 'root': [{'path': '/data/movies/anime movies'}],
          'naming': {'movieFolderFormat': '{Movie Title} ({Release Year}) - imdb-{ImdbId}', 'standardMovieFormat': '{Movie Title} ({Release Year})'},
          'items': [{'id': 7, 'title': 'Bubble', 'year': 2022, 'path': '/data/movies/anime movies/Bubble (2022)', 'added': '2026-10-24T09:00:00Z', 'hasFile': True},
                    {'id': 9, 'title': 'The Matrix', 'year': 1999, 'path': '/data/movies/anime movies/The Matrix (1999)', 'added': '2026-10-20T02:00:00Z', 'tmdbId': 603, 'hasFile': True},
                    {'id': 8, 'title': 'New Movie', 'year': 2026, 'path': '/data/movies/anime movies/New Movie (2026) - imdb-tt1234567', 'added': '2026-10-26T02:00:00Z', 'hasFile': False}]}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        port = self.server.server_address[1]
        app = SONARR if port % 2 == 1 else RADARR
        if self.headers.get('X-Api-Key') != KEY:
            self.send_response(401); self.end_headers(); return
        ep = self.path.split('?')[0].replace('/api/v3/', '')
        body = {'system/status': app['status'], 'rootfolder': app['root'], 'config/naming': app['naming'], 'series': app['items'], 'movie': app['items']}.get(ep)
        if body is None: self.send_response(404); self.end_headers(); return
        raw = json.dumps(body).encode(); self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
class FastServer(ThreadingHTTPServer):
    def server_bind(self):  # skip the slow host-name lookup (no DNS in an isolated test container)
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = 'localhost', self.server_address[1]


def serve(ports):
    import threading
    servers = [FastServer(('0.0.0.0', int(port)), H) for port in ports]
    threads = [threading.Thread(target=x.serve_forever) for x in servers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == '__main__':
    serve(sys.argv[1:])
