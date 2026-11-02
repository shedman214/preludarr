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
        months = text('January February March April May June July August September October November December', lang).split('|'…2347 tokens truncated…
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
