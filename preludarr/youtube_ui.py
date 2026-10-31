# SPDX-License-Identifier: GPL-3.0-or-later

"""YouTube cookie controls for authenticated Themearr Settings."""

import html
import os
from email import policy
from email.parser import BytesParser

import youtube_settings as cookies

MAX_REQUEST = cookies.MAX_SIZE + 65536


def config_dir():
    return os.getenv("CONFIG_DIR", "/config")


# Preludarr 1.11 (user 2026-10-29): "make sure there is a guide or link to show users how to setup YouTube cookies.txt".
# Steps follow yt-dlp's official instructions (wiki: Extractors → Exporting YouTube cookies; FAQ → How do I pass cookies), checked 2026-10-29.
COOKIE_GUIDE = (
    '<details class="yt-guide" id="youtube-cookies-guide"><summary>📘 How to get your YouTube cookies.txt (step by step)</summary>'
    '<div class="yt-guide-body">'
    '<p class="yt-guide-warn">⚠ <strong>Use a spare Google account.</strong> yt-dlp warns that an account used with download tools can be '
    'temporarily or permanently banned by YouTube. A cookies.txt works like a login — never share it.</p>'
    '<ol>'
    '<li>Install a cookie-export browser extension recommended by yt-dlp: <strong>Get cookies.txt LOCALLY</strong> (Chrome / Edge) or '
    '<strong>cookies.txt</strong> (Firefox). Avoid the old Chrome extension called just “Get cookies.txt” — it was removed as malware.</li>'
    '<li>Allow the extension in private windows, then open a new <strong>private / incognito window</strong> and sign in to YouTube there.</li>'
    '<li>In that same tab, go to <code>https://www.youtube.com/robots.txt</code> (keep it the only private tab).</li>'
    '<li>Use the extension to export the cookies for <strong>youtube.com</strong> and save the file as <code>cookies.txt</code> '
    '(it must start with <code># Netscape HTTP Cookie File</code> or <code># HTTP Cookie File</code>).</li>'
    '<li><strong>Close the private window straight away</strong> and don’t open that session again — YouTube changes the cookies of open tabs, '
    'which would make the file stop working.</li>'
    '<li>Upload <code>cookies.txt</code> here. If YouTube later starts refusing requests, repeat these steps to make a fresh file.</li>'
    '</ol>'
    '<p class="muted">Official guides: <a href="https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies" target="_blank" rel="noopener noreferrer">'
    'yt-dlp — Exporting YouTube cookies ↗</a> · <a href="https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp" target="_blank" rel="noopener noreferrer">'
    'yt-dlp FAQ — passing cookies ↗</a></p></div></details>'
    '<style>.yt-guide{margin:12px 0;border:1px solid #3a4864;border-radius:10px;background:#16243b}.yt-guide>summary{cursor:pointer;padding:10px 14px;font-weight:700}'
    '.yt-guide-body{padding:0 14px 12px}.yt-guide ol{margin:6px 0 10px;padding-left:22px;line-height:1.55}.yt-guide li{margin:4px 0}'
    '.yt-guide-warn{margin:6px 0 8px;padding:8px 10px;border-radius:8px;background:#3a2f12;border:1px solid #8a6d1f;color:#ffe9b0}'
    '.yt-guide code{overflow-wrap:anywhere}.yt-guide a{color:#9fd0ff}</style>')


# ------------------------------------------------------------------------- cookie test ----
# Preludarr 1.11 (user 2026-10-29: "somewhere add a test to test the cookies"). Two parts:
#   1. local (no network): format, YouTube sign-in cookies present, expiry dates;
#   2. one live, rate-limited yt-dlp request to a page that only works when signed in (Watch Later), using a
#      throw-away COPY of the file — yt-dlp rewrites cookie files, and the saved file must stay exactly as uploaded.
# Cookie values are never shown, logged or stored; only the outcome is saved (themearr_youtube_cookie_test.json).
TEST_FILE = 'themearr_youtube_cookie_test.json'
SIGN_IN = ('SID', '__Secure-1PSID', '__Secure-3PSID', 'SAPISID', 'LOGIN_INFO')
TEST_URL = 'https://www.youtube.com/playlist?list=WL'


def local_check(directory):
    """{'ok', 'text', 'youtube', 'signin', 'expires'} from the saved file only (no network)."""
    import time
    st = cookies.status(directory)
    if not st.get('configured'):
        return {'ok': False, 'text': 'No cookies.txt saved yet.'}
    if not st.get('private'):
        return {'ok': False, 'text': 'cookies.txt permissions need attention (must be private).'}
    data = cookies._path(directory).read_bytes()
    cookies._validate(data)
    now, yt, signin, expiries = time.time(), 0, set(), []
    for line in data.decode('utf-8-sig').splitlines()[1:]:
        if not line.strip() or (line.startswith('#') and not line.startswith('#HttpOnly_')):
            continue
        domain, _, _, _, expiry, name, _ = line.split('\t')
        domain = domain[len('#HttpOnly_'):] if domain.startswith('#HttpOnly_') else domain
        if not (domain == 'youtube.com' or domain.endswith('.youtube.com')):
            continue
        yt += 1
        if name in SIGN_IN:
            signin.add(name)
            if int(expiry) > 0:
                expiries.append(int(expiry))
    if not yt:
        return {'ok': False, 'text': 'The file has no youtube.com cookies — export them while on youtube.com.'}
    if not signin:
        return {'ok': False, 'text': 'No YouTube sign-in cookies found — sign in to YouTube in the private window before exporting.'}
    if expiries and min(expiries) < now:
        return {'ok': False, 'text': 'The YouTube sign-in cookies have expired — make a fresh cookies.txt.'}
    soon = min(expiries) if expiries else 0
    return {'ok': True, 'text': f'{yt} YouTube cookies, signed-in cookies present' + (time.strftime(', earliest expiry %Y-%m-%d', time.localtime(soon)) if soon else '') + '.'}


def live_check(directory):
    """One yt-dlp request with a temporary copy of the file. (ok, text)."""
    import shutil
    import subprocess
    import tempfile
    exe = shutil.which('yt-dlp')
    if not exe:
        return False, 'yt-dlp is not installed in Preludarr.'
    __import__('ratelimit').wait('youtube')
    stage = tempfile.mkdtemp(prefix='.themearr-cookietest-', dir=str(directory))
    try:
        os.chmod(stage, 0o700)
        copy = os.path.join(stage, 'cookies.txt')
        fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(cookies._path(directory).read_bytes())
        try:
            r = subprocess.run([exe, '--ignore-config', '--no-warnings', '--no-progress', '--socket-timeout', '15', '--retries', '1',
                                '--cookies', copy, '--flat-playlist', '--playlist-items', '1', '--skip-download', '--dump-single-json', TEST_URL],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, check=False)
        except subprocess.TimeoutExpired:
            return False, 'YouTube did not answer in time — try again later.'
        err = r.stderr.decode('utf-8', 'replace')[-2000:].casefold()
        if r.returncode == 0:
            return True, 'YouTube accepted the cookies as signed in.'
        if 'sign in to confirm' in err or 'not a bot' in err:
            return False, 'YouTube asked to confirm you are not a bot — the cookies are not being accepted. Make a fresh cookies.txt.'
        if 'private' in err or 'login' in err or 'sign in' in err or 'authentication' in err or 'does not exist' in err:
            return False, 'YouTube did not treat these cookies as signed in — they may be expired or rotated. Make a fresh cookies.txt (see the guide).'
        if '429' in err or 'too many requests' in err:
            return False, 'YouTube is rate-limiting this server right now (HTTP 429) — wait an hour, then test again.'
        return False, 'The live test failed (yt-dlp could not open the signed-in page). Try again later or make a fresh cookies.txt.'
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def run_test(directory):
    import json
    import time
    try:
        local = local_check(directory)
    except (OSError, ValueError) as error:
        local = {'ok': False, 'text': 'cookies.txt could not be read (' + str(error) + ').'}
    if local['ok']:
        ok, live = live_check(directory)
        result = {'ok': ok, 'text': local['text'] + ' ' + live}
    else:
        result = {'ok': False, 'text': local['text'] + ' (Live test skipped.)'}
    result['at'] = int(time.time())
    path = os.path.join(str(directory), TEST_FILE)
    tmp = path + '.part'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(result, f)
    os.replace(tmp, path)
    return result


def last_test(directory):
    import json
    try:
        data = json.loads(open(os.path.join(str(directory), TEST_FILE)).read())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None



def _forget_test(directory):
    try:
        os.unlink(os.path.join(str(directory), TEST_FILE))
    except FileNotFoundError:
        pass


def _last_test_html(directory):
    import time
    t = last_test(directory)
    if not t or not cookies.status(directory).get('configured'):
        return '<p class="help">Not tested yet.</p>'
    return ('<p class="yt-result ' + ('ok' if t.get('ok') else 'bad') + '"><strong>Last test ' + html.escape(time.strftime('%Y-%m-%d %H:%M', time.localtime(int(t.get('at') or 0))))
            + ':</strong> ' + ('✅ ' if t.get('ok') else '❌ ') + html.escape(str(t.get('text') or '')[:400]) + '</p>')

def panel(config=None):
    directory = config if config is not None else config_dir()

    try:
        state = cookies.status(directory)

        if state["configured"]:
            if state["private"]:
                message = "Configured — private permissions verified"
            else:
                message = "Configured — WARNING: permissions need attention"
        else:
            message = "Not configured"

    except (OSError, ValueError):
        message = "Cookie configuration unavailable"

    return (
        '<section class="panel" id="youtube-settings">'
        '<div class="pill">Download Clients · YouTube</div>'
        '<h2>🎵 YouTube Downloader</h2>'
        '<p class="help">Upload a Netscape-format cookies.txt file '
        'from your browser. Cookies are sensitive account data. '
        '<strong>Required for YouTube search</strong> (Shows and Movies); recommended for YouTube downloads.</p>'
        + COOKIE_GUIDE +
        '<p><strong>Cookie status:</strong> '
        + html.escape(message) + '</p>'

        '<form method="post" action="/youtube-cookies-upload" '
        'enctype="multipart/form-data">'
        '<label for="youtube-cookies">YouTube cookies.txt</label>'
        '<input type="file" id="youtube-cookies" name="cookies" '
        'accept=".txt,text/plain" required>'
        '<button type="submit">Upload / Replace Cookies</button>'
        '</form>'

        '<form method="post" action="/youtube-cookies-test" class="yt-test" '
        'onsubmit="const f=document.getElementById(&quot;youtube-cookies&quot;);if(f&&f.files&&f.files.length){alert(&quot;You chose a file but have not uploaded it yet. Press Upload / Replace Cookies first (it tests the file automatically).&quot;);return false}">'
        '<button type="submit">Test cookies</button>'
        '<span class="help">Checks the file, then makes one signed-in request to YouTube (about 10–60 seconds).</span>'
        '</form>'
        + _last_test_html(directory) +
        '<form method="post" action="/youtube-cookies-remove">'
        '<button type="submit" style="background:#38465f">'
        'Remove Cookies</button>'
        '</form>'

        '<p class="help">Cookie contents are never displayed. '
        'Cookies do not guarantee YouTube access.</p>'
        '<style>.yt-test{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0}.yt-test .help{margin:0}'
        '.yt-result{margin:6px 0 10px;padding:8px 10px;border-radius:8px}.yt-result.ok{background:#173a2a;border:1px solid #3f8f5f}'
        '.yt-result.bad{background:#3a1d24;border:1px solid #9b394a}</style>'
        '</section>'
    )


def _body(handler):
    if handler.headers.get("Transfer-Encoding"):
        raise ValueError("Transfer-Encoding is not supported")

    lengths = handler.headers.get_all("Content-Length") or []

    if len(lengths) != 1 or not lengths[0].isdigit():
        raise ValueError("Valid Content-Length required")

    size = int(lengths[0])

    if not 0 <= size <= MAX_REQUEST:
        raise ValueError("Request exceeds size limit")

    handler.connection.settimeout(30)
    data = handler.rfile.read(size)

    if len(data) != size:
        raise ValueError("Incomplete upload")

    return data


def _upload(handler, config):
    body = _body(handler)
    content_type = handler.headers.get("Content-Type", "")

    if (
        not content_type.lower().startswith("multipart/form-data;")
        or len(content_type) > 1024
        or not content_type.isascii()
    ):
        raise ValueError("Multipart upload required")

    header = (
        "MIME-Version: 1.0\r\n"
        "Content-Type: " + content_type + "\r\n\r\n"
    ).encode("ascii")

    message = BytesParser(
        policy=policy.default
    ).parsebytes(header + body)

    if not message.is_multipart():
        raise ValueError("Invalid multipart upload")

    parts = list(message.iter_parts())

    if len(parts) != 1:
        raise ValueError("Exactly one cookie file required")

    part = parts[0]

    if (
        part.get_param(
            "name", header="content-disposition"
        ) != "cookies"
    ):
        raise ValueError("Unexpected upload field")

    # Preludarr 1.11.1: any .txt name (extensions save "www.youtube.com_cookies.txt", browsers add " (2)"); the CONTENT is validated
    name = str(part.get_filename() or "")
    if not name.lower().endswith(".txt") or len(name) > 200:
        raise ValueError("Choose the exported .txt cookies file")

    data = part.get_payload(decode=True)

    if data is None:
        raise ValueError("Missing cookie file")

    cookies.install(config, data)


def handle(handler, path, config=None):
    directory = config if config is not None else config_dir()

    try:
        if path == "/youtube-cookies-upload":
            _upload(handler, directory)
            _forget_test(directory)
            result = run_test(directory)  # 1.11.1: test straight away so the result is visible at once
            message = "YouTube cookies saved. " + ("Test passed: " if result["ok"] else "Test failed: ") + result["text"]

        elif path == "/youtube-cookies-test":
            if _body(handler):
                raise ValueError("Unexpected request body")
            result = run_test(directory)
            message = ('Cookie test passed: ' if result['ok'] else 'Cookie test failed: ') + result['text']

        elif path == "/youtube-cookies-remove":
            if _body(handler):
                raise ValueError("Unexpected request body")

            cookies.remove(directory)
            _forget_test(directory)
            message = "YouTube cookies removed."

        else:
            raise ValueError("Unknown settings action")

    except ValueError as error:  # validation messages are fixed texts (never file contents) — safe to show
        message = ("Unable to test YouTube cookies: " if path == "/youtube-cookies-test" else "Cookies not saved: ") + str(error) + "."
    except OSError:
        message = ("Unable to test YouTube cookies." if path == "/youtube-cookies-test" else "Cookies not saved: the file could not be stored.")

    # V1.8.0: back to Settings → Download Clients with the result shown there.
    import urllib.parse
    handler.send_response(303)
    handler.send_header('Location', '/download-clients?' + urllib.parse.urlencode({'notice': message[:300], 'tab': 'engines', 'cookies': '1'}) + '#youtube-settings')
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Content-Length', '0')
    handler.end_headers()
