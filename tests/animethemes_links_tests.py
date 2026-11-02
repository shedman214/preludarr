# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline website-link rendering regressions; also picked up by run_tests.sh."""
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
import animethemes


class Links(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.anchors = []
        self.tags = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == 'a':
            self.anchors.append(dict(attrs))


class CandidateLinks(unittest.TestCase):
    def test_provider_slug_and_secure_external_anchor(self):
        body = animethemes.search_result({'slug': 'hajimete_no_gal', 'name': 'Different local title', 'year': 2017}, '/library/A&B')
        links = Links(body)
        preview, external = links.anchors
        self.assertIn('theme_slug=hajimete_no_gal', preview['href'])
        self.assertIn('path=%2Flibrary%2FA%26B', preview['href'])
        self.assertEqual(external['href'], 'https://animethemes.moe/anime/hajimete_no_gal')
        self.assertEqual(external['target'], '_blank')
        self.assertEqual(set(external['rel'].split()), {'noopener', 'noreferrer'})
        self.assertIn('(2017)', body)
        self.assertIn('Preview OP/ED tracks', body)
        self.assertIn('View on AnimeThemes', body)
        self.assertNotIn('form', links.tags)
        self.assertNotIn('onclick', body)
        self.assertIn('overflow-wrap:anywhere', body)

    def test_invalid_or_missing_provider_identifier(self):
        for slug in (None, '', 'a/b', '../evil', 'https://evil.test', 'a" onclick="bad', 'a?x=1', 'a#b', 123, 'a'*121):
            with self.subTest(slug=slug):
                self.assertEqual(animethemes.anime_page_link(slug), '')
        self.assertEqual(animethemes.search_result({'name': 'Never guess from me'}, '/x'), '')
        self.assertNotIn('View on AnimeThemes', animethemes.search_result({'slug': 'bad/slug'}, '/x'))

    def test_escaping(self):
        body = animethemes.search_result({'slug': 'real_slug', 'name': '<img src=x onerror=bad>&"', 'year': '<script>bad</script>'}, '/x"&')
        self.assertEqual(Links(body).tags.count('a'), 2)
        self.assertNotIn('img', Links(body).tags)
        self.assertNotIn('script', Links(body).tags)
        self.assertIn('&lt;img', body)
        self.assertIn('&lt;script&gt;', body)

    def test_all_candidate_paths_use_shared_link(self):
        root = Path(__file__).resolve().parents[1] / 'preludarr'
        for filename, call in [('anidb.py', "animethemes.anime_page_link(r['slug'])"), ('media_servers.py', 'animethemes.anime_page_link(slug)'), ('app.py', "animethemes.anime_page_link(show.get('slug'))"), ('app.py', "animethemes.anime_page_link(chosen.get('slug'))"), ('app.py', 'animethemes.search_result(candidate,selected)')]:
            self.assertIn(call, (root / filename).read_text(encoding='utf-8'))


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CandidateLinks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f'{result.testsRun - len(result.failures) - len(result.errors)}/{result.testsRun} passed')
    sys.exit(not result.wasSuccessful())
