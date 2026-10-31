# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare two harness outputs; exit 1 unless Original matching is identical for every title.

Usage: python3 compare_original.py BASELINE.json CANDIDATE.json
"""
import json
import sys

base = json.load(open(sys.argv[1]))
cand = json.load(open(sys.argv[2]))
failures = []

if cand.get('unrecorded_requests'):
    failures.append(f"candidate made {len(cand['unrecorded_requests'])} request(s) the baseline never made, "
                    f"e.g. {cand['unrecorded_requests'][0]}")

b, c = base['results'], cand['results']
for path in sorted(set(b) | set(c)):
    if path not in b or path not in c:
        failures.append(f'title missing from one run: {path}')
        continue
    for part in ('auto', 'browser'):
        if b[path][part] != c[path][part]:
            failures.append(f"{part:7} differs: {b[path]['name']}\n   baseline : {json.dumps(b[path][part], ensure_ascii=False)[:300]}"
                            f"\n   candidate: {json.dumps(c[path][part], ensure_ascii=False)[:300]}")

print(f'titles compared: {len(set(b) & set(c))}')
if failures:
    print(f'FAIL: {len(failures)} difference(s)')
    for line in failures:
        print(' -', line)
    sys.exit(1)
print('PASS: Original matching identical on both paths (automatic + Theme Browser) for every title')
