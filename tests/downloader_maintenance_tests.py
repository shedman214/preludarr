# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline checks against the actual downloader installed in the Docker image."""
import ast
import importlib.metadata
import re
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class DownloaderMaintenance(unittest.TestCase):
    def test_installed_downloader_and_cli(self):
        self.assertEqual(importlib.metadata.version('yt-dlp'), '2026.10.26')
        result = subprocess.run(['yt-dlp', '--ignore-config', '--no-playlist', '--no-warnings',
                                 '--no-progress', '--socket-timeout', '15', '--retries', '2',
                                 '--fragment-retries', '2', '--version'], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '2026.10.26')
        self.assertEqual(importlib.metadata.version('yt-dlp-ejs'), '0.8.0')

    def test_generated_lock_is_reproducible_and_valid(self):
        import io
        import json
        pins = ast.literal_eval(next(node.value for node in ast.parse((ROOT / 'tools/lock_requirements.py').read_text()).body
                                     if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PINS' for t in node.targets)))
        lock = (ROOT / 'docker/requirements.txt').read_text()
        blocks = lock.split('\n\n', 1)[1]
        hashes = {}
        for block in re.split(r'\n(?=[a-z])', blocks):
            spec = block.split('==', 1)[0]
            hashes[spec] = re.findall(r'--hash=sha256:([0-9a-f]{64})', block)
        def answer(url, timeout):
            name = url.split('/')[-3]
            spec = next(spec for spec, provider, version in pins if (provider or spec) == name)
            return io.BytesIO(json.dumps({'urls': [{'digests': {'sha256': h}} for h in hashes[spec]]}).encode())
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'tools/lock_requirements.py'
            script.parent.mkdir(); (Path(tmp) / 'docker').mkdir()
            script.write_text((ROOT / 'tools/lock_requirements.py').read_text())
            with patch('urllib.request.urlopen', side_effect=answer):
                runpy.run_path(str(script), run_name='__main__')
            generated = (Path(tmp) / 'docker/requirements.txt').read_text()
            self.assertEqual(generated, lock)
            self.assertNotIn(chr(92) * 2, generated)
            self.assertEqual(len(pins), len(hashes))


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DownloaderMaintenance))
    print(f'{result.testsRun - len(result.failures) - len(result.errors)}/{result.testsRun} passed')
    sys.exit(not result.wasSuccessful())
