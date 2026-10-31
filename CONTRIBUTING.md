# Contributing to Preludarr

Thank you for helping. Bug reports, tested fixes and clear ideas are all welcome.

## Before you start
- **Security problems:** never in public Issues. See [SECURITY.md](SECURITY.md).
- **Bugs:** use the *Bug report* form. Remove passwords, API keys, tokens, cookies and private paths or host names from anything you paste (logs, screenshots, settings).
- **Bigger changes:** open an issue first so we can agree on the approach.

## Working on the code
- The app is plain Python 3.12 (standard library only); the code is in `preludarr/`. `docker/` holds the image's start-up script and pinned packages.
- Build: `docker build -t preludarr:dev .`
- Test everything (network off, disposable data):
  ```sh
  sh tools/run_tests.sh preludarr:dev      # all test suites + the anime matching regression
  sh tools/smoke_test.sh preludarr:dev     # a fresh install starts and asks for setup
  sh tools/e2e_test.sh preludarr:dev       # sign-in, writes, uploads, API, streaming, slow clients
  ```
- Add or update a test in `tests/` for every change in behaviour.
- Keep existing installs working: settings files, database tables, cookies and `THEMEARR_*`/`SPLICARR_*` settings keep their names.
- Never commit real media, posters, databases, backups, logs, API keys or tokens. CI scans the full Git history for secrets.
- Every source file starts with `# SPDX-License-Identifier: GPL-3.0-or-later`. By contributing you agree that your contribution is licensed under GPL-3.0-or-later.

## Pull requests
- One topic per pull request, with a short description of what changed and how you tested it.
- CI must pass: build, tests, end-to-end, secrets, dependency, image and static checks.
