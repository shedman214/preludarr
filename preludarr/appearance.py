# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → Appearance (1.12, user 2026-10-29 / 2026-10-30): colour scheme (Dark / Light / Auto), OLED mode, standard accent themes
and immersive themes, with a live preview.

Preludarr's pages carry their colours in their own CSS, so instead of restyling every page this module maps each colour in a page
to the chosen theme as the page is sent: neutral interface colours (the navy greys) take the theme's tint (Light mode flips their
lightness), accent blues become the theme's accent, status colours (red/green/amber) keep their meaning (darker in Light mode),
and OLED turns the darkest backgrounds black. Dark + Blue without OLED is the original look and is sent untouched.
"Auto" follows each device: both palettes are sent and CSS picks one (no flash, no script needed).
"""
import base64
import colorsys
import html
import json
import os
import re
import threading
import urllib.parse
from functools import lru_cache
from pathlib import Path

FILE = 'preludarr_appearance.json'
DEFAULTS = {'scheme': 'dark', 'oled': False, 'theme': 'blue'}
SCHEMES = (('dark', 'Dark'), ('light', 'Light'), ('auto', 'Auto'))
# standard themes: accent hue (degrees) and saturation; 'slate' is a grey accent with grey chrome
STANDARD = {'blue': ('Blue', 217, 1.0), 'purple': ('Purple', 268, 0.80), 'green': ('Green', 145, 0.62), 'orange': ('Orange', 27, 0.92),
            'rose': ('Rose', 345, 0.80), 'slate': ('Slate', 215, 0.16)}
# immersive themes: their own dark look. chrome = (hue, saturation scale, lightness scale), accent = (hue, sat), font, extra CSS
IMMERSIVE = {
    'winamp': ('Winamp', (220, 0.10, 0.95), (90, 0.85), "'Tahoma','Verdana',sans-serif", 'button,.btn{border-radius:2px!important}'),
    'terminal': ('Terminal', (130, 0.55, 0.60), (130, 0.85), "'DejaVu Sans Mono','Consolas',monospace", 'body,main,aside{text-shadow:0 0 2px rgba(80,255,120,.25)}'),
    'vaporwave': ('Vaporwave', (285, 0.75, 1.05), (185, 0.85), "'Trebuchet MS',sans-serif", ''),
    'y2k': ('Y2K Cyber', (200, 0.35, 1.10), (190, 1.0), "'Verdana',sans-serif", 'button,.btn{border-radius:14px!important}'),
    'noir': ('Film Noir', (0, 0.0, 0.90), (0, 0.0), "Georgia,'Times New Roman',serif", ''),
    'vhs': ('VHS', (235, 0.55, 0.85), (350, 0.90), "'Courier New',monospace", 'h1,h2,h3{text-shadow:2px 0 rgba(255,40,80,.45),-2px 0 rgba(40,220,255,.45)}'),
    'synthwave': ('Synthwave', (275, 0.80, 0.95), (325, 1.0), "'Trebuchet MS',sans-serif", 'h1,h2{text-shadow:0 0 10px rgba(255,60,200,.55)}'),
    'amber': ('Amber CRT', (35, 0.70, 0.55), (38, 1.0), "'DejaVu Sans Mono','Consolas',monospace", 'body,main,aside{text-shadow:0 0 2px rgba(255,176,0,.3)}'),
}
_LOCK = threading.Lock()
_CACHE = {}


def _config():
    return Path(os.environ.get('CONFIG_DIR', '/config'))


def settings():
    path = _config() / FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return dict(DEFAULTS)
    with _LOCK:
        if _CACHE.get('mtime') == mtime:
            return dict(_CACHE['data'])
    try:
        raw = json.loads(path.read_text()) if not path.is_symlink() else {}
    except (OSError, ValueError):
        raw = {}
    data = dict(DEFAULTS)
    if raw.get('scheme') in dict(SCHEMES):
        data['scheme'] = raw['scheme']
    data['oled'] = raw.get('oled') is True
    if raw.get('theme') in STANDARD or raw.get('theme') in IMMERSIVE:
        data['theme'] = raw['theme']
    with _LOCK:
        _CACHE.update(mtime=mtime, data=data)
    return dict(data)


def save(data):
    clean = dict(DEFAULTS)
    if data.get('scheme') in dict(SCHEMES):
        clean['scheme'] = data['scheme']
    clean['oled'] = bool(data.get('oled'))
    if data.get('theme') in STANDARD or data.get('theme') in IMMERSIVE:
        clean['theme'] = data['theme']
    path = _config() / FILE
    tmp = path.with_name('.' + FILE + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(clean, f)
    os.replace(tmp, path)
    return clean


# ------------------------------------------------------------------------------ colour mapping ----
def _rgb(hexcode):
    h = hexcode.lstrip('#')
    if len(h) in (3, 4):
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)), (h[6:8] if len(h) == 8 else '')


def _hex(rgb, alpha=''):
    return '#' + ''.join('%02x' % max(0, min(255, round(c * 255))) for c in rgb) + alpha


@lru_cache(maxsize=8192)
def map_colour(hexcode, mode, oled, theme):
    """One colour → the same role in the chosen theme. mode: 'dark' or 'light'."""
    (r, g, b), alpha = _rgb(hexcode)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    hue = h * 360
    immersive = IMMERSIVE.get(theme)
    accent_hue, accent_sat = (immersive[2] if immersive else STANDARD.get(theme, STANDARD['blue'])[1:])
    is_accent = 195 <= hue <= 235 and s >= 0.55 and 0.30 <= l <= 0.84
    is_chrome = ((195 <= hue <= 240 and (s < 0.55 or l < 0.30)) or s < 0.12) or l < 0.06 or l > 0.97
    if is_accent:
        if theme != 'blue' or mode == 'light':
            h, s = accent_hue / 360, min(1.0, s * accent_sat if accent_sat < 0.5 else max(s, accent_sat) * 0.95)
            if mode == 'light':
                l = min(l, 0.42) if l > 0.5 else l   # strong enough on a light background
    elif is_chrome:
        if immersive:
            ch, cs, cl = immersive[1]
            h, s = ch / 360, min(1.0, s * cs + (0.10 * cs if l < 0.5 else 0.25 * cs))
            l = max(0.0, min(1.0, l * cl if l < 0.5 else 1 - (1 - l) * cl))
        elif theme == 'slate':
            s *= 0.35
        if mode == 'light':   # flip: dark backgrounds → near-white (panels a touch darker), light text → dark
            l = 0.975 - l * 0.55 if l < 0.5 else 0.03 + (1 - l) * 0.9
            s = min(s, 0.45)
        elif oled and l < 0.16:
            l = l * 0.18
    else:  # status colours: same hue; darker/stronger in light mode
        if mode == 'light':
            l = 0.06 + (1 - l) * 0.70 if l > 0.5 else min(0.97, 0.80 + (0.5 - l) * 0.35)
        elif immersive and theme == 'noir':
            s *= 0.15
    return _hex(colorsys.hls_to_rgb(h, l, s), alpha)


_HEX = re.compile(r'(?<=[:\s,("\'])#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z_-])')
_RGBA = re.compile(r'rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*([0-9.]+%?)\s*)?\)')
_NAMED = re.compile(r'(?<=:)\s*(white|black)(?=\s*[;}"!])')
_COLOUR = re.compile(r'#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z_-])|rgba?\([^)]*\)|\b(?:white|black)\b')
PAGE_BG = '#101c30'   # the app's main background, for text without a background of its own


def _lum(rgb):
    def ch(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = _lum(_rgb(a)[0]), _lum(_rgb(b)[0])
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


@lru_cache(maxsize=8192)
def readable(fg, bg, need=4.5):
    """fg moved lighter or darker (same hue) until it reaches `need` against bg; white/black only as a last resort."""
    if contrast(fg, bg) >= need:
        return fg
    (r, g, b), alpha = _rgb(fg)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    best = None
    for direction in ((1, -1) if _lum(_rgb(bg)[0]) < 0.18 else (-1, 1)):
        ll = l
        while 0 <= ll <= 1:
            cand = _hex(colorsys.hls_to_rgb(h, ll, s), alpha)
            if contrast(cand, bg) >= need:
                best = best or cand
                break
            ll += 0.03 * direction
        if best:
            return best
    return max(('#ffffff', '#0b0f17'), key=lambda c: contrast(c, bg))


def _value_to_hex(v):
    v = v.strip()
    if v in ('white', 'black'):
        return '#ffffff' if v == 'white' else '#000000', ''
    m = _RGBA.fullmatch(v)
    if m:
        return '#%02x%02x%02x' % tuple(min(255, int(x)) for x in m.group(1, 2, 3)), m.group(4) or ''
    return v.lower(), None


def _map_any(v, mode, oled, theme):
    hx, alpha = _value_to_hex(v)
    mapped = map_colour(hx, mode, oled, theme)
    if alpha is None:
        return mapped
    if not alpha:
        return mapped if not v.startswith('rgb') else 'rgb(%d,%d,%d)' % tuple(round(c * 255) for c in _rgb(mapped)[0])
    return 'rgba(%d,%d,%d,%s)' % (tuple(round(c * 255) for c in _rgb(mapped)[0]) + (alpha,))


def _solid(v, mode, oled, theme):
    hx, alpha = _value_to_hex(v)
    if alpha and alpha not in ('1', '1.0', '100%'):
        return None   # see-through: the page shows through, don't trust it as a background
    return map_colour(hx, mode, oled, theme)


_DECL = re.compile(r'([a-zA-Z-]+)\s*:\s*([^;{}"]+)')


def _block(text, mode, oled, theme):
    """One CSS rule body or style="" attribute: map colours, then keep text readable on its own background."""
    if '--pl-keep' in text:   # e.g. theme swatches: always their true colour
        return text
    bg = None
    for prop, val in _DECL.findall(text):
        if prop.lower() in ('background', 'background-color'):
            m = _COLOUR.search(val)
            if m:
                bg = _solid(m.group(0), mode, oled, theme) or bg
    page_bg = map_colour(PAGE_BG, mode, oled, theme)

    def decl(m):
        prop, val = m.group(1), m.group(2)
        newval = _COLOUR.sub(lambda c: _map_any(c.group(0), mode, oled, theme), val)
        if prop.lower() == 'color':
            c = _COLOUR.search(newval)
            if c and not c.group(0).startswith('rgba'):
                newval = newval.replace(c.group(0), readable(_value_to_hex(c.group(0))[0], bg or page_bg), 1)
        return prop + ':' + newval
    return _DECL.sub(decl, text)


def _transform(text, mode, oled, theme):
    """Map every colour in a page; rules and style attributes are handled as a whole so their text stays readable."""
    def style_tag(m):
        return m.group(1) + re.sub(r'\{([^{}]*)\}', lambda b: '{' + _block(b.group(1), mode, oled, theme) + '}', m.group(2)) + m.group(3)
    text = re.sub(r'(<style[^>]*>)(.*?)(</style>)', style_tag, text, flags=re.S)
    text = re.sub(r'(style=")([^"]*)(")', lambda m: m.group(1) + _block(m.group(2), mode, oled, theme) + m.group(3), text)
    # anything else (colours inside scripts): plain mapping
    parts = re.split(r'(<style[^>]*>.*?</style>|style="[^"]*")', text, flags=re.S)
    for i in range(0, len(parts), 2):
        parts[i] = _NAMED.sub(lambda m: map_colour('#ffffff' if m.group(1) == 'white' else '#000000', mode, oled, theme),
                              _RGBA.sub(lambda m: _map_any(m.group(0), mode, oled, theme),
                                        _HEX.sub(lambda m: map_colour('#' + m.group(1).lower(), mode, oled, theme), parts[i])))
    return ''.join(parts)


def is_default(s):
    return s['scheme'] == 'dark' and not s['oled'] and s['theme'] == 'blue'


AUTO_SCRIPT = ('<script>(function(){try{var w=matchMedia("(prefers-color-scheme: light)").matches?"light":"dark";'
               'if(document.cookie.indexOf("pl_cs="+w)<0){document.cookie="pl_cs="+w+";path=/;max-age=31536000;samesite=strict";'
               'if(document.cookie.indexOf("pl_cs="+w)>=0)location.reload();}}catch(e){}})();</script>')


def apply(body, s=None, prefers=''):
    """Theme one HTML page. Dark + Blue without OLED: untouched. Auto: the device's choice, remembered in the pl_cs cookie
    (a tiny script sets it on first visit or when the device switches, then reloads once)."""
    s = s or settings()
    if not isinstance(body, str) or is_default(s) or 'data-pl-themed' in body:
        return body
    theme, oled = s['theme'], s['oled']
    immersive = IMMERSIVE.get(theme)
    mode = 'dark' if immersive else ('light' if s['scheme'] == 'light' or (s['scheme'] == 'auto' and prefers == 'light') else 'dark')
    if mode == 'dark' and not oled and theme == 'blue':
        out = body   # Auto on a dark device with the default theme: the original look
    else:
        out = _transform(body, mode, oled, theme)
    extra = ''
    if immersive:
        extra = 'body,main,aside,button,input,select,textarea{font-family:' + immersive[3] + '!important}' + immersive[4]
    tag = ('<style id="pl-appearance" data-pl-themed="1">' + extra + '</style>' + (AUTO_SCRIPT if s['scheme'] == 'auto' and not immersive else '')
           + ('<meta name="color-scheme" content="' + mode + '">'))
    out = out.replace('</head>', tag + '</head>', 1) if '</head>' in out else tag + out
    return out.replace('<html', '<html data-pl-theme="' + theme + '" data-pl-scheme="' + mode + '"', 1)


# ------------------------------------------------------------------------------ settings page ----
PREVIEW = ('<div class="pl-preview" id="pl-preview"><div class="pv-side"><b>Preludarr</b><span class="on">Library</span><span>Collections</span><span>Settings</span></div>'
           '<div class="pv-main"><div class="pv-stats"><div><strong>4,537</strong><small>titles</small></div><div><strong>1,126</strong><small>installed</small></div>'
           '<div><strong>12</strong><small>waiting</small></div></div>'
           '<div class="pv-row"><span>Attack on Titan</span><span class="pv-ok">Installed</span></div><div class="pv-row"><span>Cowboy Bebop</span><span class="pv-warn">Missing</span></div>'
           '<div class="pv-btns"><button type="button" class="pv-primary">Primary</button><button type="button" class="pv-secondary">Secondary</button><a href="#" onclick="return false">Link</a></div>'
           '<p class="pv-cap" id="pv-cap"></p></div></div>')
PREVIEW_CSS = ('.pl-preview{display:flex;border:1px solid #3b4b6b;border-radius:12px;overflow:hidden;background:#101c30;color:#eef4ff;min-height:230px;max-width:640px}'
               '.pv-side{width:150px;background:#0b1424;padding:12px;display:flex;flex-direction:column;gap:6px;font-size:13px;color:#cfe0f8}.pv-side b{margin-bottom:6px}'
               '.pv-side span{padding:6px 8px;border-radius:7px}.pv-side span.on{background:#2a4a7a;color:#ffffff}.pv-main{flex:1;padding:14px;display:grid;gap:10px}'
               '.pv-stats{display:flex;gap:8px}.pv-stats div{flex:1;background:#16243b;border:1px solid #2b3a55;border-radius:9px;padding:8px}.pv-stats strong{display:block;font-size:18px}'
               '.pv-stats small{color:#9fb0cc}.pv-row{display:flex;justify-content:space-between;padding:8px 10px;background:#16243b;border-radius:8px;border:1px solid #2b3a55;font-size:14px}'
               '.pv-ok{background:#1f4532;color:#bff0d2;padding:1px 8px;border-radius:999px;font-size:12px}.pv-warn{background:#4a3a1a;color:#ffd36b;padding:1px 8px;border-radius:999px;font-size:12px}'
               '.pv-btns{display:flex;gap:8px;align-items:center}.pv-primary{background:#4f8cff;color:#ffffff;border:0;border-radius:8px;padding:8px 14px;font-weight:700}'
               '.pv-secondary{background:#22324f;color:#eef4ff;border:1px solid #3e5174;border-radius:8px;padding:8px 14px}.pv-btns a{color:#9fd0ff}.pv-cap{margin:0;color:#9fb0cc;font-size:13px}')


def _choice(name, value, label, current, swatch=''):
    return ('<label class="pl-opt' + (' on' if value == current else '') + '"><input type="radio" name="' + name + '" value="' + value + '"'
            + (' checked' if value == current else '') + '>' + swatch + '<span>' + html.escape(label) + '</span></label>')


def page(app, handler):
    s = settings()
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    notice = q.get('notice', [''])[0][:200]
    ms = __import__('media_servers')
    previews = {}
    for scheme in ('dark', 'light'):
        for oled in (False, True):
            for t in list(STANDARD) + list(IMMERSIVE):
                previews['%s|%d|%s' % (scheme, oled, t)] = _transform('<style>' + PREVIEW_CSS + '</style>', 'dark' if t in IMMERSIVE else scheme, oled, t)[7:-8]
    std = ''.join(_choice('theme', k, v[0], s['theme'], '<i class="sw" style="--pl-keep:1;background:' + map_colour('#4f8cff', 'dark', False, k) + '"></i>') for k, v in STANDARD.items())
    imm = ''.join(_choice('theme', k, v[0], s['theme'], '<i class="sw" style="--pl-keep:1;background:linear-gradient(135deg,' + map_colour('#101c30', 'dark', False, k) + ' 50%,'
                          + map_colour('#4f8cff', 'dark', False, k) + ' 50%)"></i>') for k, v in IMMERSIVE.items())
    body = (ms.settings_shell('Appearance', 'appearance') + '</aside><main class="main" id="settings-appearance">'
            '<div class="topline"><div class="crumb">Settings / <strong>Appearance</strong></div></div><h1 class="heading">Appearance</h1>'
            '<p class="muted">How Preludarr looks, for everyone who uses it. Auto follows each device’s light or dark setting.</p>'
            + ('<p class="notice" role="status">' + html.escape(notice) + '</p>' if notice else '') +
            '<form method="post" action="/settings/appearance" id="pl-form"><section class="panel"><h2>Color scheme</h2><div class="pl-seg">'
            + ''.join(_choice('scheme', k, v, s['scheme']) for k, v in SCHEMES) + '</div>'
            '<label class="check pl-oled"><input type="checkbox" name="oled" value="1"' + (' checked' if s['oled'] else '') + '><span><strong>OLED mode</strong> — '
            'pure black backgrounds in dark mode (saves power on OLED screens)</span></label></section>'
            '<section class="panel"><h2>Standard themes</h2><p class="muted">An accent colour that adapts to light and dark.</p><div class="pl-grid">' + std + '</div></section>'
            '<section class="panel"><h2>Immersive themes</h2><p class="muted">Their own dark look, with their own fonts.</p><div class="pl-grid">' + imm + '</div></section>'
            '<section class="panel"><h2>Live preview</h2><style id="pv-style"></style>' + PREVIEW + '</section>'
            '<div class="pl-save"><button type="submit" class="primary">Save appearance</button></div></form>'
            '<style>.pl-seg{display:inline-flex;border:1px solid #3e5174;border-radius:10px;overflow:hidden}.pl-seg .pl-opt{padding:9px 18px;border:0;border-radius:0}'
            '.pl-opt{display:inline-flex;gap:8px;align-items:center;padding:9px 14px;border:1px solid #3e5174;border-radius:10px;cursor:pointer;background:#16243b}'
            '.pl-opt input{position:absolute;opacity:0;pointer-events:none}.pl-opt.on{background:#2a4a7a;box-shadow:inset 0 0 0 2px #8fc1ff}'
            '.pl-opt:focus-within{outline:3px solid #8fc1ff;outline-offset:2px}.pl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}'
            '.sw{display:inline-block;width:22px;height:22px;border-radius:50%;border:1px solid #ffffff40}.pl-oled{margin-top:14px;display:flex;gap:10px;align-items:flex-start}'
            '.pl-save{margin:16px 0 40px}#settings-appearance .panel{max-width:900px}</style>'
            '<script>(function(){var P=JSON.parse(atob("' + base64.b64encode(json.dumps(previews).encode()).decode() + '")),f=document.getElementById("pl-form"),st=document.getElementById("pv-style"),cap=document.getElementById("pv-cap"),'
            'names=' + json.dumps({k: v[0] for k, v in list(STANDARD.items()) + list(IMMERSIVE.items())}) + ';'
            'function upd(){var sc=f.scheme.value,o=f.oled.checked?1:0,t=f.theme.value,mq=window.matchMedia&&matchMedia("(prefers-color-scheme: light)").matches,'
            'eff=sc==="auto"?(mq?"light":"dark"):sc;st.textContent=P[eff+"|"+(eff==="dark"?o:0)+"|"+t]||"";'
            'cap.textContent=(sc==="auto"?"System":sc[0].toUpperCase()+sc.slice(1))+" mode"+(o&&eff==="dark"?" · OLED":"")+" · "+names[t]+" theme";'
            'f.querySelectorAll(".pl-opt").forEach(function(l){l.classList.toggle("on",l.querySelector("input").checked);});}'
            'f.addEventListener("change",upd);upd();})();</script></main></body></html>')
    return handler.send(200, body, 'text/html')


def post(app, handler, redirect):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 1024:
            raise ValueError
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=6)
        f = lambda k: form.get(k, [''])[0]  # noqa: E731
        saved = save({'scheme': f('scheme'), 'oled': f('oled') == '1', 'theme': f('theme')})
        names = dict(SCHEMES)
        msg = 'Saved: ' + names[saved['scheme']] + (' · OLED' if saved['oled'] else '') + ' · ' + (STANDARD.get(saved['theme']) or IMMERSIVE[saved['theme']])[0] + ' theme.'
    except (ValueError, OSError, UnicodeError):
        msg = 'Not saved: invalid form.'
    return redirect(handler, '/settings/appearance?' + urllib.parse.urlencode({'notice': msg}))
