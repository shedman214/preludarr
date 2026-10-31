# Security check exceptions

Every exception below was checked by hand against the code on 2026-10-30. Re-check an entry when the code it points to changes.

## gitleaks (secrets)
| Location | Rule | Why it is not a secret |
|---|---|---|
| `tests/v1914_tests.py` (log redaction test) | generic-api-key | The dummy value `0123456789abcdef0123456789abcdef`, used to check that logs hide API keys. Allow-listed for that file and value only in `.gitleaks.toml`. |
| `tests/fake_media_server.py`, `tests/jellyfin_qc_tests.py` (fake Jellyfin) | generic-api-key | The dummy Quick Connect secret `qcsecret0123456789` of the fake Jellyfin server (first flagged by CI on 2026-10-30, commit 50010eb). Allow-listed for those files and that value only. |

## Bandit (static analysis)
`bandit-baseline.json` records the 82 findings that existed when the check was introduced. CI fails on any **new** finding of medium or higher severity and confidence (`-ll -ii`). Summary of the baseline:

| Test | Count | Verdict |
|---|---|---|
| B608 SQL built from strings | 23 | False positive. Every fragment is fixed text chosen by the code (`name LIKE ?`, `library=?`, the poster filter map, `root:` scope); user values are only ever passed as `?` parameters. Table and column names come from constants in the code. |
| B310 `urlopen` with a variable URL | 10 | Accepted. Fixed API addresses (AniDB, AnimeThemes, TheTVDB, GitHub, YouTube) or provider URLs restricted to fixed HTTPS hosts (TMDB/TheTVDB images, `a.`/`v.animethemes.moe`, Plex TV Themes, which also checks the final host after redirects). See the review note on redirects below. |
| B314 `xml.etree` on server answers | 3 | Accepted. Parses the AniDB title dump and Plex answers; Python's expat in the image has entity-expansion ("billion laughs") protection, and external entities are not resolved. |
| B104 bind to 0.0.0.0 | 1 | Required inside a container; Docker publishes the port. |
| B105 "hard-coded password" | 1 | False positive: `preview_token = ''` (an empty variable). |
| B101 `assert` | 1 | A layout sanity check in the Connect page, not a security check. |
| B110/B112 try/except pass/continue, B404/B603/B607 subprocess | 44 | Low severity. Subprocesses (`ffmpeg`, `ffprobe`, `yt-dlp`) are started with argument lists, never a shell, with timeouts. |

## pip-audit / Trivy
No exceptions. `.trivyignore` does not exist; add one only with the CVE, the reason and a review date.

## Pre-release gate #14 (2026-10-30)
- Trivy 0.76.0 on the image (148 Alpine packages + Python packages): no known vulnerabilities. pip-audit 2.10.1: none. Bandit 1.9.4: nothing new beyond the baseline.
- docker/THIRD_PARTY_PACKAGES.txt regenerated from the image with `tools/third_party_list.sh` (it was out of date: Alpine 3.24.2 → 3.24.3, and pip is no longer in the image).
- YouTube API rules: Collections' "Use this" on a search result used to download it in one click. It now only fills the YouTube link box; the user ticks "I may use this audio" and installs, as on movie pages.

## Known follow-up (not a scanner finding)
- Fixed in 1.12 (pre-release gate #14): poster and AnimeThemes audio downloads used to follow HTTP redirects without re-checking the host. Every redirect is now checked *before* it is followed (HTTPS, approved host, no port or credentials) and the final address is checked again (`app.open_approved`, tests/download_redirect_tests.py).
- Fixed in 1.12 (review finding): replacing a theme used to overwrite the previous theme.mp3 without a copy; it is now kept in the recycle bin first (tests/replace_backup_tests.py).
