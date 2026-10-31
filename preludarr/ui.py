# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared visual language for Settings and System pages (Themearr V1.9.8, user design brief 2026-10-27).

Loaded LAST in <head> of Settings/System pages so it consistently overrides the older per-page styles
(incl. the "Connect V2" all-white rules). Keeps the logo, dark navy identity and every existing feature.

* Tokens: content width, card padding, headings, 42 px inputs, badges, focus ring, dark scrollbar.
* Button hierarchy: every <button>/.btn is classified server-side by its label into
  primary (main task), secondary (everything else) or danger (removing / deleting / resetting).
  No JavaScript needed; markup and form behaviour are unchanged.
"""
import html
import re

PRIMARY = re.compile(r'^(?:[＋+]\s*)?(?:save|add|create|run health scan|use this mapping|keep the selected|fix files|confirm|upload|sign in|filter|search|apply)\b', re.I)
DANGER = re.compile(r'^(?:[✕×🗑]|🔄\s*regenerate|(?:remove|delete|disconnect|regenerate|empty|restore|clear pin|reset)\b)', re.I)
SETTINGS_PATHS = ('/settings', '/media-management', '/media-servers', '/download-clients', '/connect/', '/system', '/library-health', '/media-server')

UI_CSS = """<style id="tm-ui">
/* ---- Themearr UI tokens (V1.9.8) ---- */
:root{--tm-bg:#0f1727;--tm-card:#1b2840;--tm-card2:#22324f;--tm-line:#36486a;--tm-text:#eef4ff;--tm-muted:#b4c4dc;
 --tm-accent:#4f8ff7;--tm-accent2:#3a77df;--tm-danger:#c2475a;--tm-ok:#2f8f5b;--tm-warn:#c9902f;--tm-focus:#8fc1ff;--tm-radius:12px;--tm-pad:22px;--tm-content:1320px}
html body{background:var(--tm-bg)}
/* Content column. Cause of the V1.9.8-dev wide-screen regression: a max-width (border-box) combined with the older
   viewport-based side padding max(20px,(100vw-1400px)/3) — padding grew with the window while the box stayed fixed, so the
   content shrank to ~400 px at 2560 px. Now: fixed padding, max-width, centred in the space beside the sidebar. */
html body main,html body main.main,html body main#settings-general{box-sizing:border-box;width:100%;max-width:var(--tm-content);
 margin-left:auto!important;margin-right:auto!important;padding:32px clamp(20px,2.5vw,40px) 60px!important;flex:1 1 auto;min-width:0}
html body main h1,html body main .heading{font-size:28px;line-height:1.2;margin:10px 0 6px;color:var(--tm-text)}
html body main h2{font-size:20px;line-height:1.3;margin:0 0 10px;color:var(--tm-text)}
html body main h3{font-size:16px;margin:16px 0 8px;color:var(--tm-text)}
html body main .panel,html body main .card,html body main .connection,html body main .arr-card,html body main .lh-card,html body main .tile{
 background:var(--tm-card);border:1px solid var(--tm-line);border-radius:var(--tm-radius);padding:var(--tm-pad)}
html body main .panel{margin-top:18px}
html body main .muted,html body main small,html body main .help,html body main .pm-help,html body main .intro,html body main .crumb,html body main .stats{color:var(--tm-muted)!important}
html body main .stats strong,html body main strong{color:var(--tm-text)}
html body main a:not(.btn):not(.tm-btn):not(.choice):not(.ov-card):not(.lg-file){color:#9fd0ff;text-decoration:underline;text-underline-offset:3px}
html body main .tabs a,html body main .tm-subnav a{text-decoration:none!important}
html body main .tabs a{color:#c5d8f6!important}html body main .tabs a.active,html body main .tabs a[aria-current]{color:#fff!important}
html body main a:not(.btn):not(.tm-btn):not(.choice):hover{color:#c6e3ff}
/* form fields: one height, sensible width; tables keep full width */
html body main input:not([type=checkbox]):not([type=radio]):not([type=file]):not([type=hidden]),html body main select{
 min-height:42px;height:42px;padding:8px 12px;font:inherit;font-size:15px;color:var(--tm-text)!important;background:#0b1424!important;
 border:1px solid #556a8e!important;border-radius:8px;box-sizing:border-box;max-width:min(560px,100%)}
html body main textarea{font:inherit;color:var(--tm-text);background:#0b1424;border:1px solid #556a8e;border-radius:8px;padding:10px 12px;max-width:min(720px,100%);box-sizing:border-box}
html body main input::placeholder,html body main textarea::placeholder{color:#8fa1bd;opacity:1}
html body main table{width:100%}html body main table.pm-table{width:auto}
/* buttons: primary / secondary / danger — repeated .tm-btn raises specificity above the old Connect V2 rules (e.g. .actions .btn.ghost !important) */
html body main .tm-btn,html body main button.tm-btn,html body main a.tm-btn,html body main summary.tm-btn{
 display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:40px;padding:8px 16px;border-radius:8px!important;
 font:inherit;font-size:14.5px;font-weight:700;line-height:1.2;cursor:pointer;text-decoration:none!important;box-shadow:none!important;filter:none!important;white-space:nowrap}
html body main .tm-btn.tm-btn.tm-btn.tm-secondary{background:#253552!important;color:#e3edfc!important;border:1px solid #5a6f94!important}
html body main .tm-btn.tm-btn.tm-btn.tm-secondary:hover{background:#2f4366!important}
html body main .tm-btn.tm-btn.tm-btn.tm-primary{background:var(--tm-accent)!important;color:#fff!important;border:1px solid var(--tm-accent)!important}
html body main .tm-btn.tm-btn.tm-btn.tm-primary:hover{background:var(--tm-accent2)!important}
html body main .tm-btn.tm-btn.tm-btn.tm-danger{background:transparent!important;color:#ffb3bd!important;border:1px solid #c2475a!important}
html body main .tm-btn.tm-btn.tm-btn.tm-danger:hover{background:#4a2430!important}
html body main .tm-btn[disabled]{opacity:.55;cursor:not-allowed}
/* focus: always visible */
html body a:focus-visible,html body button:focus-visible,html body input:focus-visible,html body select:focus-visible,html body textarea:focus-visible,html body summary:focus-visible{
 outline:3px solid var(--tm-focus)!important;outline-offset:2px}
/* badges */
html body main .tag,html body main .lh-badge{display:inline-flex;align-items:center;min-height:22px;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700;
 background:#2a3a57!important;color:#e4edfb!important;border:1px solid #4f6388!important}
html body main .tag.on,html body main .lh-badge.ok,html body main .tag.arr-ok{background:#1d4a39!important;color:#b9f3d5!important;border-color:#3d8d68!important}
html body main .tag.off,html body main .lh-badge.unk,html body main .tag.arr-unknown{background:#333b4b!important;color:#d6dce7!important;border-color:#59647a!important}
html body main .lh-badge.bad,html body main .tag.arr-bad{background:#4a2430!important;color:#ffc4cc!important;border-color:#c2475a!important}
html body main .tag.warn{background:#4a3a1c!important;color:#ffe2a8!important;border-color:#c9902f!important}
/* status lines that tell readiness apart from connectivity */
html body main .tm-status{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
/* in-page section navigation */
html body main .tm-subnav{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 4px;padding:6px;border:1px solid var(--tm-line);border-radius:10px;background:#152038;position:sticky;top:0;z-index:5}
html body main .tm-subnav a{padding:6px 12px;border-radius:7px;color:#cfe0f8!important;text-decoration:none!important;font-weight:600;font-size:14px}
html body main .tm-subnav a:hover,html body main .tm-subnav a.on{background:#2a3c5e;color:#fff!important}
html body main details.tm-help{margin:8px 0;border:1px solid var(--tm-line);border-radius:8px;padding:6px 10px;background:#17223a}
html body main details.tm-help>summary{cursor:pointer;color:#cfe0f8;font-weight:600;font-size:14px}
html body main .tm-label{display:inline-block;font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;padding:1px 7px;border-radius:5px;background:#2a3a57;color:#cfe0f8;margin-left:6px;vertical-align:middle}
html body main .tm-label.cached{background:#3a3220;color:#ffdca0}html body main .tm-label.current{background:#1d4a39;color:#b9f3d5}
html body main .tm-label.historical{background:#33304a;color:#d8d0ff}html body main .tm-label.unavailable{background:#3a3f4b;color:#d6dce7}
html body main [id^=settings-],html body main section[id]{scroll-margin-top:72px}
html body main nav.tm-subnav{flex-direction:row!important;align-items:center}
html body main#settings-general .panel,html body main#settings-general section.panel{max-width:none;width:auto}
/* V1.9.9: General tabs hold short forms, so the cards (and their tab bar) stop at a readable width instead of leaving half the card empty */
html body main#settings-general .panel,html body main#settings-general section.panel,html body main#settings-general nav.tm-tabs{max-width:960px}
html body main#settings-general .help{max-width:none}  /* user 2026-10-27: help text uses the full card width */
/* V1.9.9: compact dark audio players (native controls; color-scheme makes Chrome/Edge draw them dark) */
html body audio{color-scheme:dark;accent-color:#5b8cff;height:40px;max-width:100%;border-radius:20px;background:#0d1626}
html body audio::-webkit-media-controls-panel{background:#16233a}
/* all scrollbars dark (sidebar, log viewer, wide tables) */
html body *{scrollbar-width:thin;scrollbar-color:#3b4b6b #0f1727}
html body ::-webkit-scrollbar{width:9px;height:9px}html body ::-webkit-scrollbar-thumb{background:#3b4b6b;border-radius:8px}html body ::-webkit-scrollbar-track{background:#0f1727}
/* sidebar scrollbar: dark, not a white bar */
html body aside{scrollbar-width:thin;scrollbar-color:#3b4b6b #151e32}
html body aside::-webkit-scrollbar{width:8px}html body aside::-webkit-scrollbar-thumb{background:#3b4b6b;border-radius:8px}html body aside::-webkit-scrollbar-track{background:#151e32}
/* wide diagnostics stay usable on small screens */
html body main .scroll,html body main .tm-wide{overflow-x:auto;-webkit-overflow-scrolling:touch}
@media(max-width:760px){html body main,html body main.main,html body main#settings-general{padding:20px 14px 48px!important}html body main .panel{padding:16px}}
</style>"""


def _label(inner):
    return html.unescape(re.sub(r'<[^>]+>', ' ', inner)).strip()


def classify(label, attrs=''):
    """'tm-danger' for removing/deleting/resetting, 'tm-primary' for the main task, else 'tm-secondary'."""
    text = re.sub(r'\s+', ' ', label).strip()
    if 'value="remove"' in attrs or 'value="delete"' in attrs or DANGER.match(text):
        return 'tm-danger'
    cls = re.search(r'\bclass="([^"]*)"', attrs)
    if PRIMARY.match(text) or (cls and re.search(r'\bprimary\b', cls.group(1))):
        return 'tm-primary'
    return 'tm-secondary'


def _add_class(attrs, cls):
    if re.search(r'\bclass="', attrs):
        return re.sub(r'\bclass="([^"]*)"', lambda m: 'class="' + m.group(1) + ' tm-btn ' + cls + '"', attrs, count=1)
    return attrs + ' class="tm-btn ' + cls + '"'


def buttons(body):
    """Add tm-btn + hierarchy class to every <button>, a.btn and summary.btn (once). <script> blocks are left untouched."""
    parts = re.split(r'(<script\b.*?</script>)', body, flags=re.S | re.I)
    return ''.join(p if p[:7].lower() == '<script' else _buttons(p) for p in parts)


def _buttons(body):
    def btn(m):
        attrs, inner = m.group(1), m.group(2)
        if 'tm-btn' in attrs:
            return m.group(0)
        return '<button' + _add_class(attrs, classify(_label(inner), attrs)) + '>' + inner + '</button>'
    body = re.sub(r'<button\b([^>]*)>(.*?)</button>', btn, body, flags=re.S)

    def link(m):
        tag, attrs, inner, end = m.group(1), m.group(2), m.group(3), m.group(4)
        if 'tm-btn' in attrs or not re.search(r'\bclass="[^"]*\b(btn|button)\b', attrs):
            return m.group(0)
        return '<' + tag + _add_class(attrs, classify(_label(inner), attrs)) + '>' + inner + end
    return re.sub(r'<(a|summary)\b([^>]*)>(.*?)(</\1>)', link, body, flags=re.S)


def wanted(path):
    path = (path or '').split('?', 1)[0]
    return any(path == p or path.startswith(p.rstrip('/') + '/') or path.startswith(p) for p in SETTINGS_PATHS)


def apply(body, path):
    """Inject the shared CSS (last in <head>) and classify buttons on Settings/System pages."""
    if not isinstance(body, str) or 'id="tm-ui"' in body or not wanted(path):
        return body
    low = body.lower()
    i = low.find('</head>')
    body = (body[:i] + UI_CSS + body[i:]) if i != -1 else (UI_CSS + body)
    return buttons(body)


def when(ts, with_date=True):
    """Local time with an explicit timezone, e.g. 'Oct 27, 2026 · 9:31 PM CDT' (ts = epoch seconds or aware datetime)."""
    import datetime as _dt
    try:
        d = ts if isinstance(ts, _dt.datetime) else _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        d = d.astimezone()
        text = d.strftime(('%b %d, %Y · ' if with_date else '') + '%I:%M %p %Z')
        return text.replace(' 0', ' ').replace('· 0', '· ')
    except (TypeError, ValueError, OSError, OverflowError):
        return 'unknown time'


def label(kind):
    """Small badge telling whether information is current, cached, historical or unavailable."""
    names = {'current': 'Current', 'cached': 'Cached', 'historical': 'Historical', 'unavailable': 'Unavailable'}
    return '<span class="tm-label ' + kind + '">' + names[kind] + '</span>'
