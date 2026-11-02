# SPDX-License-Identifier: GPL-3.0-or-later
"""Language and display preferences. Never changes stored dates or scheduled jobs."""
from functools import lru_cache
import datetime as dt
import html
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

LANGUAGES = {'en': 'English', 'es': 'Español', 'fr': 'Français', 'de': 'Deutsch',
             'pt-BR': 'Português (Brasil)', 'ja': '日本語', 'zh-Hans': '简体中文', 'ar': 'العربية'}
DEFAULTS = {'language': 'en', 'short_date': 'month', 'long_date': 'weekday',
            'clock': '12', 'relative_dates': False, 'timezone': 'server'}
CHOICES = {'language': LANGUAGES, 'short_date': ('month', 'iso', 'dmy', 'mdy'),
           'long_date': ('weekday', 'date'), 'clock': ('12', '24')}
FILE = 'preludarr_region.json'


def settings():
    result = dict(DEFAULTS)
    try:
        path = Path(os.environ.get('CONFIG_DIR', '/config')) / FILE
        if path.is_symlink():
            return result
        saved = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(saved, dict):
            return result
        for key, choices in CHOICES.items():
            if isinstance(saved.get(key), str) and saved[key] in choices:
                result[key] = saved[key]
        if isinstance(saved.get('relative_dates'), bool):
            result['relative_dates'] = saved['relative_dates']
        zone = saved.get('timezone')
        if isinstance(zone, str) and valid_zone(zone):
            result['timezone'] = zone
    except (OSError, ValueError, TypeError):
        pass
    return result


def valid_zone(value):
    if value == 'server':
        return True
    if not isinstance(value, str) or len(value) > 100 or value.startswith('/') or '..' in value:
        return False
    try:
        ZoneInfo(value)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def save(form):
    result = {}
    for key, choices in CHOICES.items():
        values = form.get(key, [])
        if len(values) != 1 or values[0] not in choices:
            raise ValueError('Choose a valid value for ' + key + '.')
        result[key] = values[0]
    zone = form.get('timezone', [])
    if len(zone) != 1 or not valid_zone(zone[0]):
        raise ValueError('Choose a valid time zone.')
    relative = form.get('relative_dates', [])
    if relative not in ([], ['1']):
        raise ValueError('Choose a valid relative date setting.')
    result.update(timezone=zone[0], relative_dates=relative == ['1'])
    # Reuse the app's atomic, private settings writer and its symlink protections.
    import general_settings
    general_settings._write(Path(os.environ.get('CONFIG_DIR', '/config')) / FILE,
                            json.dumps(result, ensure_ascii=False))
    return result


@lru_cache(maxsize=8)
def catalog(language):
    if language not in LANGUAGES or language == 'en':
        return {}
    return json.loads((Path(__file__).parent / 'locales' / (language + '.json')).read_text(encoding='utf-8'))


def text(value, language=None):
    lang = language or settings()['language']
    return catalog(lang).get(value, value)


def _local(value, zone):
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(None if zone == 'server' else ZoneInfo(zone))


def _named_date(value, months, language):
    month = months[value.month-1]
    if language in ('ja', 'zh-Hans'):
        return f'{value.year}年{month}{value.day}日'
    if language in ('es', 'pt-BR'):
        return f'{value.day} de {month} de {value.year}'
    if language == 'ar':
        return f'{value.day} {month} {value.year}'
    if language == 'fr':
        return f'{value.day} {month} {value.year}'
    if language == 'de':
        return f'{value.day}. {month} {value.year}'
    return f'{month} {value.day}, {value.year}'


def when(value, with_date=True, long=False, prefs=None, now=None, date_only=False):
    s = prefs or settings()
    try:
        value = value if isinstance(value, dt.datetime) else dt.datetime.fromtimestamp(float(value), dt.timezone.utc)
        value = _local(value, s['timezone'])
        lang = s['language']
        if s == DEFAULTS and not long and not date_only:
            return value.strftime(('%b %d, %Y · ' if with_date else '') + '%I:%M %p %Z').replace(' 0', ' ').replace('· 0', '· ')
        months = text('January February March April May June July August September October November December', lang).split('|')
        if len(months) != 12:
            months = 'January February March April May June July August September October November December'.split()
        weekdays = text('Monday Tuesday Wednesday Thursday Friday Saturday Sunday', lang).split('|')
        if len(weekdays) != 7:
            weekdays = 'Monday Tuesday Wednesday Thursday Friday Saturday Sunday'.split()
        fmt = s['short_date']
        date = (value.strftime('%Y-%m-%d') if fmt == 'iso' else value.strftime('%d/%m/%Y') if fmt == 'dmy'
                else value.strftime('%m/%d/%Y') if fmt == 'mdy' else _named_date(value, months, lang))
        if long:
            date = _named_date(value, months, lang)
            if s['long_date'] == 'weekday':
                date = weekdays[value.weekday()] + ', ' + date
        if s['relative_dates'] and with_date:
            today = _local(now or dt.datetime.now(dt.timezone.utc), s['timezone']).date()
            age = (today - value.date()).days
            if age in (0, 1):
                date = text('Today' if age == 0 else 'Yesterday', lang)
        clock = value.strftime('%H:%M') if s['clock'] == '24' else value.strftime('%I:%M %p').lstrip('0')
        zone = value.tzname() or s['timezone']
        return date if date_only else (date + ' · ' if with_date else '') + clock + ' ' + zone
    except (TypeError, ValueError, OSError, OverflowError, ZoneInfoNotFoundError):
        return text('unknown time', s.get('language', 'en'))


def panel(query):
    s = settings()
    def select(key, label, options):
        recovery = ('<button type="submit" form="region-english-form" translate="no" lang="en" dir="ltr">Switch back to English</button>'
                    if key == 'language' else '')
        return ('<div class="region-row"><label for="region-' + key + '">' + html.escape(label) +
                '</label><div class="region-control"><select id="region-' + key + '" name="' + key + '">' +
                ''.join('<option value="' + html.escape(k, quote=True) + '"' +
                (' selected' if s[key] == k else '') + '>' + html.escape(v) + '</option>' for k, v in options.items()) +
                '</select>' + recovery + '</div></div>')
    notice = query.get('region_notice', [''])[0][:300]
    sample = dt.datetime(2026, 11, 2, 17, 30, tzinfo=dt.timezone.utc)
    zones = {'server': 'Server time zone'}
    zones.update({z: z for z in sorted(available_timezones()) if not z.startswith(('posix/', 'right/'))})
    return ('<section class="panel" id="settings-region"><h2>Language &amp; Region</h2>' +
            ('<p role="status">' + html.escape(notice) + '</p>' if notice else '') +
            '<p>These preferences apply to everyone using this Preludarr instance.</p>'
            '<form method="post" action="/settings/general"><input type="hidden" name="section" value="region">' +
            '<h3 class="region-heading">Language</h3>' + select('language', 'UI Language', LANGUAGES) +
            '<p class="help">Core navigation and common controls are translated. Untranslated text falls back to English. Media titles and provider data keep their original language.</p>' +
            '<h3 class="region-heading">Dates</h3>' + select('short_date', 'Short date format', {'month': when(sample, date_only=True, prefs=dict(s, short_date='month', relative_dates=False)), 'iso': '2026-11-02', 'dmy': '02/11/2026', 'mdy': '11/02/2026'}) +
            select('long_date', 'Long date format', {'weekday': when(sample, long=True, date_only=True, prefs=dict(s, long_date='weekday', relative_dates=False)), 'date': when(sample, long=True, date_only=True, prefs=dict(s, long_date='date', relative_dates=False))}) +
            select('clock', 'Time format', {'12': '5:30 PM (12-hour)', '24': '17:30 (24-hour)'}) +
            select('timezone', 'Display time zone', zones) +
            '<p class="help">Display only. Scheduled jobs, stored timestamps and Docker TZ are unchanged.</p>'
            '<label class="region-check"><input type="checkbox" name="relative_dates" value="1"' +
            (' checked' if s['relative_dates'] else '') + '>Show relative dates (Today / Yesterday)</label>'
            '<p>Preview</p><output aria-live="polite">' + html.escape(when(sample, prefs=s)) + '<br>' +
            html.escape(when(sample, long=True, prefs=s)) + '</output>'
            '<p class="help">Save to apply your choices and refresh the preview. No restart is needed.</p>'
            '<button type="submit">Save Language &amp; Region</button></form>'
            '<form id="region-english-form" method="post" action="/settings/general"><input type="hidden" name="section" value="region-english"></form>'
            '<style>#settings-region .region-heading{font-size:20px;font-weight:500;border-bottom:1px solid var(--tm-line,#536787);padding-bottom:8px;margin:24px 0 20px}'
            '#settings-region .region-row{display:grid;grid-template-columns:220px minmax(0,1fr);align-items:start;gap:20px;margin:18px 0}'
            '#settings-region .region-row>label{text-align:right;margin:10px 0 0;font-size:14px}'
            '#settings-region .region-control{display:flex;flex-wrap:wrap;align-items:center;gap:10px;min-width:0}'
            '#settings-region .region-control select{display:block;width:380px;max-width:100%;margin:0;min-height:36px;font:inherit}'
            '#settings-region .region-control button{margin:0;white-space:normal;text-align:center;max-width:100%}'
            '#settings-region output{display:block;overflow-wrap:anywhere;line-height:1.8}'
            '#settings-region .region-check{display:flex;gap:10px;align-items:center}'
            '#settings-region .region-check input{width:18px;height:18px;margin:0;flex:none}'
            '@media(max-width:700px){#settings-region .region-row{grid-template-columns:minmax(0,1fr);gap:8px}'
            '#settings-region .region-row>label{text-align:left;margin:0}#settings-region .region-control select{width:100%}}</style></section>')



class _Interface(HTMLParser):
    """Translate only interface controls, never arbitrary page text or media rows."""
    def __init__(self, words):
        super().__init__(convert_charrefs=False)
        self.words, self.out, self.stack, self.pending = words, [], [], []

    def handle_starttag(self, tag, attrs):
        self.flush()
        raw = self.get_starttag_text()
        if tag == 'html':
            raw = re.sub(r'\s+lang=["\'][^"\']*["\']', '', raw)
            raw = re.sub(r'\s+dir=["\'][^"\']*["\']', '', raw)
            raw = raw[:-1] + ' lang="' + self.words['__language__'] + '" dir="' + ('rtl' if self.words['__language__'] == 'ar' else 'ltr') + '">'
        self.out.append(raw)
        if tag not in ('input', 'img', 'br', 'hr', 'meta', 'link', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'track', 'col'):
            self.stack.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.flush()
        self.out.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.flush()
        self.out.append('</' + tag + '>')
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.pending.append(data)

    def flush(self):
        if not self.pending:
            return
        raw = ''.join(self.pending)
        self.pending.clear()
        tags = [tag for tag, attrs in self.stack]
        region = any(attrs.get('id') == 'settings-region' for tag, attrs in self.stack)
        blocked = any(t in tags for t in ('script', 'style', 'code', 'pre', 'textarea', 'output'))
        blocked = blocked or any('tm-brand' in attrs.get('class', '').split() or attrs.get('translate') == 'no'
                                 for tag, attrs in self.stack)
        settings_page = any(attrs.get('id') == 'settings-general' for tag, attrs in self.stack)
        dynamic_tabs = any(attrs.get('id') == 'library-tabs' for tag, attrs in self.stack)
        controls = region or 'button' in tags or ('nav' in tags and not dynamic_tabs) or (settings_page and any(t in tags for t in ('label', 'h1', 'h2')))
        if blocked or not controls:
            self.out.append(raw)
            return
        data = html.unescape(raw)
        stripped = data.strip()
        match = re.match(r'^([^\w]*)(.*?)(?:[ \t]*[▾▼])?$', stripped, flags=re.S)
        prefix, label = match.groups() if match else ('', stripped)
        suffix = ' ▾' if stripped.endswith('▾') else ' ▼' if stripped.endswith('▼') else ''
        translated = self.words.get(stripped, prefix + self.words[label] + suffix if label in self.words else None)
        if translated is None:
            self.out.append(raw)
        else:
            self.out.append(html.escape(data[:len(data)-len(data.lstrip())] + translated + data[len(data.rstrip()):]))

    def handle_entityref(self, name):
        self.pending.append('&' + name + ';')

    def handle_charref(self, name):
        self.pending.append('&#' + name + ';')

    def handle_comment(self, data):
        self.flush()
        self.out.append('<!--' + data + '-->')

    def handle_decl(self, data):
        self.flush()
        self.out.append('<!' + data + '>')


def apply(body):
    if not isinstance(body, str) or '<html' not in body.lower():
        return body
    lang = settings()['language']
    if lang == 'en':
        return body
    words = dict(catalog(lang))
    words['__language__'] = lang
    parser = _Interface(words)
    parser.feed(body)
    parser.close()
    parser.flush()
    result = ''.join(parser.out)
    if lang == 'ar':
        result = result.replace('</head>', RTL_CSS + '</head>', 1)
    # The recovery link stays English and only navigates; changing language requires a protected POST.
    return re.sub(r'(<nav\b[^>]*>)', lambda m: m.group(0) + '<a href="/settings?section=region" translate="no" lang="en" dir="ltr">🌐 Language / English</a>', result, count=1)


RTL_CSS = """<style id="region-rtl">
html[dir=rtl] aside.tm-full{border-right:0;border-left:1px solid #293954}
html[dir=rtl] aside.tm-full .tm-nav>a,html[dir=rtl] aside.tm-full .settings-subnav a{text-align:right}
html[dir=rtl] aside.tm-full .settings-subnav{margin-left:0;margin-right:13px;border-left:0;border-right:2px solid #476588;padding-left:0;padding-right:13px}
html[dir=rtl] aside.tm-full.tm-fixed{left:auto;right:0}
html[dir=rtl] .tm-fixed~.themearr-content{margin-left:0;margin-right:236px}
html[dir=rtl] #settings-region select:not(#region-language),html[dir=rtl] code,html[dir=rtl] input[type=url],html[dir=rtl] input[type=email]{direction:ltr;unicode-bidi:isolate}
html[dir=rtl] #settings-region option{unicode-bidi:isolate}
@media(max-width:1100px){html[dir=rtl] aside.tm-full,html[dir=rtl] aside.tm-full.tm-fixed{left:auto!important;right:-300px!important}
html[dir=rtl] aside.tm-full.tm-open{left:auto!important;right:0!important}
html[dir=rtl] aside.tm-full .tm-menu-btn{left:auto;right:10px}
html[dir=rtl] aside.tm-full .tm-scrim{left:0;right:280px}
html[dir=rtl] .tm-fixed~.themearr-content{margin-left:0;margin-right:0}}
</style>"""
