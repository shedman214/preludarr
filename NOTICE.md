# Notices

**Preludarr** (previously named Splicarr, and before that Themearr). Copyright (C) 2026 The Preludarr contributors.

Preludarr is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either **version 3 of the License, or (at your option) any later version** (`GPL-3.0-or-later`). Preludarr is distributed in the hope that it will be useful, but **WITHOUT ANY WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the [LICENSE](LICENSE) file for the full text.

Every source file carries the tag `SPDX-License-Identifier: GPL-3.0-or-later`.

## Third-party software in the Docker image
The Preludarr image bundles the programs and libraries below. Each keeps its own licence. Preludarr runs them as separate programs or uses them as libraries.

The complete, generated list of every package and its licence is in [docker/THIRD_PARTY_PACKAGES.txt](docker/THIRD_PARTY_PACKAGES.txt).

| Component | Used for | Licence |
|---|---|---|
| Python 3.12 | Runs Preludarr | PSF-2.0 |
| Alpine Linux 3.24 (base packages, musl) | Operating system of the image | Various (MIT, BSD, GPL-2.0-or-later …) |
| FFmpeg 8.1 + x264/x265/LAME/Opus/libvpx/AOM libraries (Alpine builds) | Converting theme audio to MP3 | GPL-2.0-or-later and LGPL-2.1-or-later (libraries: GPL-2.0-or-later, LGPL, BSD) |
| yt-dlp | Downloading YouTube audio the user chose | Unlicense |
| yt-dlp-ejs | YouTube challenge solver scripts used by yt-dlp | Unlicense AND MIT AND ISC |
| Deno 2.7 | JavaScript runtime for yt-dlp's challenge solver (sandboxed) | MIT |
| mutagen | Audio metadata (yt-dlp dependency) | GPL-2.0-or-later |
| requests, urllib3, certifi, idna, charset-normalizer, brotli, websockets, pycryptodomex | yt-dlp dependencies | Apache-2.0 / MIT / MPL-2.0 / BSD-3-Clause / Public Domain |
| OpenSSL 3 | TLS | Apache-2.0 |
| SQLite | Preludarr's database | Public domain ("blessing") |

**Source code for the bundled packages:**
- Alpine Linux packages are built from the public aports repository: <https://gitlab.alpinelinux.org/alpine/aports>. Use the branch that matches the Alpine release in THIRD_PARTY_PACKAGES.txt; each package's sources and build files are there.
- Python packages are published with their source on PyPI.
- If you received the Preludarr image and need the corresponding source of a GPL component and cannot find it there, open an issue on the Preludarr repository and the maintainers will provide it.

## Online services
Preludarr can contact these services. None of them is part of Preludarr, and each has its own terms:
- **AnimeThemes:** <https://animethemes.moe>
- **AniList**
- **AniDB** (title list)
- **TMDB:** The Movie Database. This product uses the TMDB API but is not endorsed or certified by TMDB.
- **TheTVDB**
- **Plex TV Themes**
- **ThemerrDB:** LizardByte, BSD-3-Clause data
- **YouTube:** the YouTube Data API, with **your own** API key, so you use it under the [YouTube Terms of Service](https://www.youtube.com/t/terms), the [YouTube API Services Terms](https://developers.google.com/youtube/terms/api-services-terms-of-service) and the [Google Privacy Policy](https://policies.google.com/privacy). Search results (including matches on the [MainThemeSoundtracks](https://www.youtube.com/@mainthemesoundtracks) channel) are for preview only: Preludarr never downloads one by itself. Using one is your own step (paste or pick the link, confirm you may use the audio, install). Search results are kept at most 30 days.
- Sonarr, Radarr, Plex, Jellyfin and Emby, but only the servers *you* connect.

**Finding a song through a source does not mean you are allowed to download it.** Download only what you have the right to use, under the terms of each service and the law where you live.

## Names, logos and artwork
- **Preludarr logo and emblem:** created with an AI image generator (ChatGPT) for this project. This is recorded so the origin is clear. It is **not** a statement that the artwork is cleared for copyright or trademark purposes.
- **Logos of the theme sources and connected services** (for example AnimeThemes, Plex, ThemerrDB, AniDB, Sonarr, Radarr): shown only to identify those services. They belong to their respective owners. Preludarr is not affiliated with or endorsed by them.
- "Sonarr", "Radarr", "Plex", "Jellyfin", "Emby", "YouTube" and the other names above are trademarks of their respective owners.
