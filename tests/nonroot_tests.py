# SPDX-License-Identifier: GPL-3.0-or-later
"""Preludarr 1.12 task 9 (non-root runtime) and the repository layout. Run in a Preludarr image as root, --network none."""
import os
import pkgutil
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, '/src/preludarr')
import permissions as pm  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


real_uid, real_gid = os.geteuid, os.getegid


def owner(euid, egid=None, **env):
    for k in ('PUID', 'PGID'):
        os.environ.pop(k, None)
    os.environ.update(env)
    os.geteuid, os.getegid = (lambda: euid), (lambda: euid if egid is None else egid)
    try:
        return pm.theme_owner()
    finally:
        os.geteuid, os.getegid = real_uid, real_gid
        for k in ('PUID', 'PGID'):
            os.environ.pop(k, None)


check('Root without PUID keeps the old owner 99:100', owner(0) == (99, 100))
check('PUID/PGID set: themes belong to that user', owner(0, PUID='1000', PGID='1000') == (1000, 1000) and owner(99, 100, PUID='99', PGID='100') == (99, 100))
check('PUID=0 (root escape hatch) keeps the old owner 99:100', owner(0, PUID='0', PGID='0') == (99, 100))
check('Started with --user (no PUID): the running user, nothing to change', owner(1234, 4321) == (1234, 4321))
check('A junk PUID is ignored', owner(0, PUID='abc') == (99, 100))

d = Path(tempfile.mkdtemp())
f = d / 'theme.mp3'; f.write_bytes(b'x' * 2048); os.chown(f, 0, 0); os.chmod(f, 0o600)
os.environ.update(PUID='1000', PGID='1000')
fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW)
try:
    pm.set_theme_owner(fd)
finally:
    os.close(fd)
st = f.stat()
check('Installing gives theme.mp3 the PUID:PGID owner and mode 664', (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (1000, 1000, 0o664))
del os.environ['PUID'], os.environ['PGID']

src = Path('/src')
df = (src / 'Dockerfile').read_text()
ep = (src / 'docker' / 'entrypoint.sh').read_text()
check('Image starts through the entrypoint that drops root', 'ENTRYPOINT ["/usr/local/bin/preludarr-entrypoint"]' in df and 'exec su-exec "$PUID:$PGID" "$@"' in ep)
check('Entrypoint only ever changes the config folder, never follows symlinks', ep.count('chown') == 2 and '! -type l' in ep and '-xdev' in ep and '/media' not in ep.split('set -eu', 1)[1])
check('Unraid template runs as 99:100', 'Target="PUID" Default="99"' in (src / 'unraid' / 'my-preludarr.xml').read_text())

code = sorted(p.stem for p in (src / 'preludarr').glob('*.py'))
check('All app code lives in preludarr/; nothing named themearr_* any more', code and not any(n.startswith('themearr_') for n in code)
      and not list(src.glob('*.py')), [p.name for p in src.glob('*.py')])
clash = set(code) & (set(sys.stdlib_module_names) | {m.name for m in pkgutil.iter_modules() if not m.module_finder.path.startswith('/src')})
check('No module name hides a Python or installed module', not clash, sorted(clash))

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
