# SPDX-License-Identifier: GPL-3.0-or-later
"""Language & Region regression suite: offline, disposable data, all eight languages."""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
import region
import general_settings as gs


class LanguageRegion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, CONFIG_DIR=self.tmp.name)
        self.env.start()
        self.path = Path(self.tmp.name) / region.FILE

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def prefs(self, **extra):
        return dict(region.DEFAULTS, **extra)

    def form(self, **extra):
        values = self.prefs(**extra)
        return {k: ['1'] if v is True else [v] for k, v in values.items() if v is not False}

    def test_defaults_and_bad_saved_data(self):
        self.assertEqual(region.settings(), region.DEFAULTS)
        for invalid in ('{', '[]', '{"language":"../../bad","clock":"99","timezone":"../../etc/passwd","relative_dates":"true"}'):
            self.path.write_text(invalid)
            self.assertEqual(region.settings(), region.DEFAULTS)

    def test_all_catalogs_and_english_fallback(self):
        keys = set(region.catalog('es'))
        for lang in region.LANGUAGES:
            self.assertEqual(region.text('not yet translated', lang), 'not yet translated')
            if lang != 'en':
                words = region.catalog(lang)
                self.assertEqual(set(words), keys)
                self.assertTrue(all(isinstance(v, str) and v for v in words.values()))
                self.assertNotEqual(region.text('Save Language & Region', lang), 'Save Language & Region')
                self.assertEqual(len(words['January February March April May June July August September October November December'].split('|')), 12)

    def test_dates_and_time_formats(self):
        stamp = dt.datetime(2026, 11, 2, 17, 30, tzinfo=dt.timezone.utc)
        for lang in region.LANGUAGES:
            for fmt, date in [('iso', '2026-11-02'), ('dmy', '02/11/2026'), ('mdy', '11/02/2026')]:
                p = self.prefs(language=lang, short_date=fmt, clock='24', timezone='UTC')
                self.assertEqual(region.when(stamp, prefs=p), date + ' · 17:30 UTC')
                self.assertEqual(region.when(stamp, prefs=p, date_only=True), date)
            self.assertIn('5:30 PM', region.when(stamp, prefs=self.prefs(language=lang, timezone='UTC')))
        self.assertIn('2026年11月2日', region.when(stamp, long=True, prefs=self.prefs(language='ja', timezone='UTC')))
        self.assertIn('Montag', region.when(stamp, long=True, prefs=self.prefs(language='de', timezone='UTC')))
        self.assertNotIn('Montag', region.when(stamp, long=True, prefs=self.prefs(language='de', long_date='date', timezone='UTC')))

    def test_relative_midnight_dst_future_and_invalid(self):
        now = dt.datetime(2026, 11, 2, 6, 30, tzinfo=dt.timezone.utc)
        for lang in region.LANGUAGES:
            p = self.prefs(language=lang, timezone='America/Chicago', relative_dates=True, clock='24', short_date='iso')
            self.assertTrue(region.when(now, prefs=p, now=now).startswith(region.text('Today', lang)))
            self.assertTrue(region.when(now-dt.timedelta(hours=2), prefs=p, now=now).startswith(region.text('Yesterday', lang)))
            self.assertIn('2026-11-03', region.when(now+dt.timedelta(days=1), prefs=p, now=now))
        p = self.prefs(timezone='America/Chicago', clock='24')
        self.assertIn('01:30 CDT', region.when(dt.datetime(2026,11,1,6,30,tzinfo=dt.timezone.utc), prefs=p))
        self.assertIn('01:30 CST', region.when(dt.datetime(2026,11,1,7,30,tzinfo=dt.timezone.utc), prefs=p))
        self.assertEqual(region.when('bad'), 'unknown time')
        self.assertFalse(region.valid_zone('../etc/passwd'))
        self.assertFalse(region.valid_zone('/etc/passwd'))
        self.assertFalse(region.valid_zone('not/a/zone'))

    def test_save_reload_atomic_private_and_existing_settings(self):
        for lang in region.LANGUAGES:
            region.save(self.form(language=lang, clock='24', short_date='dmy', relative_dates=True, timezone='Asia/Tokyo'))
            self.assertEqual(region.settings()['language'], lang)
            self.assertEqual(region.settings()['timezone'], 'Asia/Tokyo')
            self.assertTrue(region.settings()['relative_dates'])
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((Path(self.tmp.name) / gs.FILE).exists())
        before = self.path.read_bytes()
        for change in ({'language': ['bad']}, {'clock': ['12','24']}, {'short_date': ['<script>']}, {'timezone': ['../bad']}, {'relative_dates': ['bad']}):
            f = self.form();f.update(change)
            with self.assertRaises(ValueError):
                region.save(f)
            self.assertEqual(self.path.read_bytes(), before)

    def test_saved_symlink_and_atomic_temp_symlink(self):
        outside = Path(self.tmp.name) / 'outside.json';outside.write_text('{"language":"ja"}')
        self.path.symlink_to(outside)
        self.assertEqual(region.settings(), region.DEFAULTS)
        self.path.unlink()
        (Path(self.tmp.name) / ('.'+region.FILE+'.part')).symlink_to(outside)
        with self.assertRaises(OSError):
            region.save(self.form())
        self.assertEqual(outside.read_text(), '{"language":"ja"}')

    def test_interface_safety_and_entities(self):
        body = '<!doctype html><html lang="en"><head><script>const x="Settings";</script><style>.Library{color:red}</style></head><body><nav><a href="/settings?x=1&amp;y=2">⚙️ Settings</a><a href="/settings?section=region">Language &amp; Region</a></nav><main><h3>Library</h3><label>Library</label><nav id="library-tabs"><a>Library</a></nav><table><tr><td>Settings</td></tr></table><input value="Settings"><textarea>Settings</textarea><button>Save Changes</button><code>Library</code><div translate="no"><button>Search</button></div>&lt;script&gt;</main></body></html>'
        self.assertEqual(region.apply(body), body)
        for lang in region.LANGUAGES:
            self.path.write_text(json.dumps(self.prefs(language=lang)))
            out = region.apply(body)
            for original in ('<h3>Library</h3>', '<label>Library</label>', '<nav id="library-tabs"><a>Library</a></nav>', '<td>Settings</td>', '<input value="Settings">', '<textarea>Settings</textarea>', '<script>const x="Settings";</script>', '<style>.Library{color:red}</style>', '<code>Library</code>', '<button>Search</button>', '&lt;script&gt;', 'href="/settings?x=1&amp;y=2"'):
                self.assertIn(original, out)
            if lang != 'en':
                self.assertIn('lang="'+lang+'"', out)
                self.assertIn(region.text('Language & Region', lang), out)
                self.assertIn(region.text('Save Changes', lang), out)
        self.path.write_text(json.dumps(self.prefs(language='ar')))
        arabic=region.apply(body)
        self.assertIn('dir="rtl"', arabic)
        self.assertIn('id="region-rtl"', arabic)
        self.assertIn('lang="en" dir="ltr"', arabic)
        self.path.write_text(json.dumps(self.prefs(language='zh-Hans')))
        chinese=region.apply(body)
        self.assertIn('lang="zh-Hans" dir="ltr"', chinese)
        self.assertNotIn('id="region-rtl"', chinese)
        self.assertEqual(region.apply('{"Settings":1}'), '{"Settings":1}')

    def test_panel_saved_values_and_notice_escaping(self):
        self.path.write_text(json.dumps(self.prefs(language='es', relative_dates=True)))
        panel = region.panel({'region_notice':['<script>bad</script>']})
        self.assertIn('&lt;script&gt;', panel)
        self.assertNotIn('<script>bad', panel)
        self.assertIn('value="es" selected', panel)
        self.assertIn('name="relative_dates" value="1" checked', panel)
        translated = region.apply('<html><body>'+panel+'</body></html>')
        self.assertIn('Idioma y región', translated)
        self.assertIn('Guardar idioma y región', translated)
        self.assertIn('action="/settings/general"', translated)
        self.assertIn('form="region-english-form" translate="no" lang="en"', panel)
        self.assertIn('<form id="region-english-form"', panel)
        self.assertIn('class="region-row"', panel)

    def test_post_route_valid_and_rejected(self):
        class H:
            def __init__(self, form):
                raw=urllib.parse.urlencode(form, doseq=True).encode()
                self.headers={'Content-Length':str(len(raw))};self.rfile=io.BytesIO(raw)
        redirects=[]
        region_form=self.form(language='fr');region_form['section']=['region']
        gs.post(H(region_form), lambda h,url: redirects.append(url))
        self.assertEqual(region.settings()['language'], 'fr')
        self.assertIn('section=region', redirects[-1])
        gs.post(H(dict(region_form, timezone=['bad'])), lambda h,url: redirects.append(url))
        self.assertIn('region_notice=', redirects[-1])
        self.assertEqual(region.settings()['timezone'], 'server')

    def test_english_recovery_preserves_other_preferences(self):
        self.path.write_text(json.dumps(self.prefs(language='ja', clock='24', timezone='Asia/Tokyo')))
        class H:
            headers={'Content-Length':'22'}
            rfile=io.BytesIO(b'section=region-english')
        gs.post(H(), lambda h,url: None)
        self.assertEqual(region.settings()['language'], 'en')
        self.assertEqual(region.settings()['clock'], '24')
        self.assertEqual(region.settings()['timezone'], 'Asia/Tokyo')

    def test_authenticated_page_decorator_and_tab_visibility(self):
        import login
        auth=login.Auth()
        auth.credentials=lambda: ('admin','unused')
        auth.active=lambda h: True
        class H:
            path='/settings?section=region'
            headers={'Host':'192.168.1.20:8788'}
            client_address=('10.0.0.9',1)
        body=auth.settings(H())
        self.assertIn('#settings-region{display:block!important}', body)
        self.assertIn('section=region', body)
        decorated=auth.decorate(H(), body)
        # Existing form protection must also cover the new settings form.
        self.assertIn('Switch back to English', decorated)
        auth.hash_text=lambda: 'configured'
        auth.same_origin=lambda h: False
        refused=[]
        H.send=lambda h,status,body,ctype: refused.append(status)
        H.path='/settings/general'
        self.assertFalse(auth.post(H()))
        self.assertEqual(refused, [403])
        self.assertIn('action="/settings/general"', decorated)


if __name__ == '__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(LanguageRegion)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    print(f'{result.testsRun-len(result.failures)-len(result.errors)}/{result.testsRun} passed')
    sys.exit(not result.wasSuccessful())
