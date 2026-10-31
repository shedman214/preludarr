#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Run Preludarr's test suites inside an image, with the network off.
# Usage: tools/run_tests.sh IMAGE [ANIDB_DUMP.xml.gz]
# The AniDB suite runs only when a dump file is given (AniDB's title list is not stored in this repository).
set -u
IMAGE=${1:?usage: tools/run_tests.sh IMAGE [ANIDB_DUMP.xml.gz]}
DUMP=${2:-}
REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
mkdir -p "$WORK/rolib/Read Only Show (2020)" "$WORK/out"
chmod 777 "$WORK/out"
failed=0
run() { # name, extra docker args…, -- , script args…
  name=$1; shift
  extra=""; while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do extra="$extra $1"; shift; done; [ "$#" -gt 0 ] && shift
  # shellcheck disable=SC2086
  out=$(docker run --rm --network none --tmpfs /tmp --tmpfs /config --tmpfs /work $extra -v "$REPO":/src:ro -w /tmp \
        -e CONFIG_DIR=/config --entrypoint python3 "$IMAGE" -B "/src/tests/$name.py" "$@" 2>&1)
  rc=$?
  printf '%-24s %s\n' "$name" "$(printf '%s\n' "$out" | grep -E '[0-9]+/[0-9]+ passed' | tail -1)"
  if [ $rc -ne 0 ]; then failed=$((failed + 1)); printf '%s\n' "$out" | grep -E 'FAIL|Error|Traceback' | head -20; fi
}
for t in "$REPO"/tests/*_tests.py; do
  n=$(basename "$t" .py)
  case $n in
    health_tests) run "$n" -v "$WORK/rolib:/rolib:ro" --;;
    permissions_tests) run "$n" --read-only -- /src/preludarr /work;;
    anidb_tests) [ -n "$DUMP" ] && run "$n" -v "$DUMP:/dump.xml.gz:ro" -- /dump.xml.gz;;
    *) run "$n" --;;
  esac
done
# Anime matching must stay identical to the recorded baseline (62 titles, recorded API answers, no network).
docker run --rm --network none --tmpfs /tmp --tmpfs /config -e CONFIG_DIR=/config -v "$REPO":/src:ro -v "$WORK/out":/out -w /tmp \
  --entrypoint python3 "$IMAGE" -B /src/tests/original_matching_harness.py replay \
  /src/tests/matching/fixtures-original.json /src/tests/matching/regression-titles.json /out/replay.json >/dev/null 2>&1 \
  && docker run --rm --network none -v "$REPO":/src:ro -v "$WORK/out":/out --entrypoint python3 "$IMAGE" -B \
     /src/tests/matching/compare_original.py /src/tests/matching/baseline-v161-original.json /out/replay.json | tail -1 \
  || { echo 'matching harness: FAILED'; failed=$((failed + 1)); }
rm -rf "$WORK"
[ $failed -eq 0 ] && echo 'ALL TESTS PASSED' || echo "$failed test group(s) FAILED"
exit $failed
