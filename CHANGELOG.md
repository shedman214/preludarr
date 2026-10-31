# Changelog

All notable changes to Preludarr are listed here. Preludarr was previously named **Splicarr** (1.10–1.11) and, before that, **Themearr**.

Versions follow [Semantic Versioning](https://semver.org/):
- `MAJOR.MINOR.PATCH`, for example `1.12.0`;
- pre-releases add a suffix: `1.12.0-beta.1`, `1.12.0-beta.2`, …;
- development builds end in `-dev` and are never published.

Each release says what changed for users. Upgrades keep your data, settings and backups unless a release says otherwise.

## [1.12.0-beta.1] — 2026-10-31 (first public beta)
- **MainThemeSoundtracks first.** YouTube search for shows, movies and collections first looks for an exact title on the MainThemeSoundtracks channel (official main themes; the channel list is read about once a week and uses none of your daily searches). Only if it isn't there does Preludarr search all of YouTube; a *Search all of YouTube* button is there when you want other versions.
- **Use this on title pages.** A YouTube search result on a show or movie page has *Use this*, which fills in the YouTube box; you review and install, as in Collections.
- **Preview on the page.** Collections' YouTube results play in a small player on the page instead of opening YouTube. *Use this* now fills the YouTube link box; you confirm and install, as on movie pages.
- **Searches kept for you.** Background YouTube search uses at most three quarters of the daily allowance; the last quarter (20 of 80) is kept for the searches you start yourself.
- **Fresh-install fixes.** Installing an AnimeThemes theme from the Theme Browser no longer needs manual installs turned on (that setting is only for your own uploads and YouTube links). A new install's dashboard shows *Getting started*. The setup and sign-in pages fit phone screens, scrollbars are dark, and the folder picker no longer shows empty system folders.
- **Unraid template uses the `beta` tag** during the beta (the plain name means `latest`, which only stable releases get).
- **Safer downloads.** Poster and AnimeThemes audio downloads only follow redirects to the same approved HTTPS sites.
- **Up-to-date package list.** docker/THIRD_PARTY_PACKAGES.txt is regenerated from the image (`tools/third_party_list.sh`).
- **New name: Preludarr.** Splicarr is renamed before its first public release, because a similar name is already used by a music app. Your settings, data, backups and Docker settings (`THEMEARR_*`, `SPLICARR_*`, and the new `PRELUDARR_*`) all keep working. A saved default name of "Splicarr" or "Themearr" shows as Preludarr; custom names are kept.
- **New logo** with the Preludarr wordmark; the emblem is unchanged.
- **Update available notice** (System → Releases): checks the project's GitHub releases once a day, never installs anything, and can be turned off.
- **Licence:** GPL-3.0-or-later, with a NOTICE file and a list of third-party packages.
- **First-run setup:** a new install asks you to create your account, using a one-time code printed in the container log.
- **Addresses:** any private or local IP address works without extra settings; host names can be allowed in Settings.
- **Restore on restart:** restoring a backup works on any Docker host. Confirm the restore on Backup & Restore, then restart the container. Preludarr saves a safety copy of your current data first, restores the database and **all** settings in the backup, checks them, and puts your previous data back automatically if anything fails. The result is shown on Backup & Restore.
- **System → Releases:** shows what's new and how to update or go back.
- **About** (System → Overview): licence and data-source credits.
- **New installs** start without root folders; add them in Settings. Library volumes mounted at `/media` can be browsed by default.
- **Docker image** for Intel/AMD and ARM: `ghcr.io/shedman214/preludarr`, with a health check. Install guide for Docker Compose, `docker run` and Unraid, and an Unraid template.
- **Runs as a normal user:** set `PUID`/`PGID` to the owner of your media (Unraid: 99/100). On first start Preludarr gives its `/config` folder to that user; media folders are never changed. Themes it installs get that owner.
- **Tidier project:** all app code now lives in one `preludarr/` folder, with clear module names. Settings files, the database and backups keep their names, so nothing changes for existing installs.

- **API key read-only by default** on new installs: starting scans must be allowed under Settings → General. Existing installs keep what they had (the key may start scans), saved once so it shows in Settings.
- **Sturdier web server:** limits on slow and oversized requests and on connections at once (overall and per client).
- **Icons:** 🍜 Anime and 🍥 Anime Movies.
- **Security policy** (private reporting), contribution guide, bug-report form, and automated checks for secrets, vulnerable packages, the container image and risky code.
- **Image tags:** betas are published as `beta` and their exact version; `latest` is for stable releases only.

- **Collections:** a theme song for each movie collection. Plex: uploaded to the collection (and removable). Jellyfin and Emby: experimental, written into the collection's folder. Each collection shows which Plex library it is in, with a library filter.
- **Sign in with Plex** (Settings → Connect → Plex): sign in with your Plex account and pick your server; no token copying. Several Plex servers can be connected, with one main server.
- **Jellyfin Quick Connect** (Settings → Connect → Jellyfin): approve a code in Jellyfin instead of creating an API key.
- **Sign in to Emby** (Settings → Connect → Emby): username and password once; the password is not saved.
- **Collection artwork** from Plex, Jellyfin or Emby on the Collections page.
- **Collections, other sources:** a YouTube link, your own audio file, or Smart YouTube search suggestions (preview first, you choose) instead of a movie's theme.
- **Collections search:** filter as you type, or search every folder by collection or movie name. Live install progress in each row, and a Current theme player.
- **Collections tabs:** one tab per movie root folder, like the Library; collections from libraries that are not root folders (e.g. 4K) are under "Other libraries". A Refresh only re-reads collections that changed.

- **Replaced themes are kept:** when you replace a theme, the previous theme.mp3 goes to the recycle bin first; Undo puts it back.
- **Quieter log:** AniList "no poster match" answers are no longer logged as errors.
- **Appearance** (Settings → Appearance): Dark / Light / Auto, OLED mode, six accent colours and eight immersive themes, with a live preview; text stays readable in every theme.

## [1.11.9]
- **Library:** one tab per root folder, named by the folder's display name (for example "Kids TV"). The dashboard has a card per folder.

## [1.11.8]
- **Installs:** Shows and Movies installs show live progress (download, convert, validate, install).
- **Installs:** a read-only folder is detected before anything is downloaded.

## [1.11.7]
- **Installed app:** a Back button appears in the installed app (Edge, Chrome, iPhone).

## [1.11.6]
- **Branding:** new Splicarr emblem for the browser tab, installed app and home-screen icons.

## [1.11.5]
- **YouTube:** YouTube downloads work again. The image now includes Deno and the yt-dlp challenge solver.
- **Installs:** read-only folders are explained before you press Install.

## [1.11.4]
- **Installed app:** Edge and Chrome show back and reload buttons.

## [1.11.3]
- **YouTube search:** a **Test API key** button.
- **Moving folders:** when saved data is moved between folders, the original dates, posters and ids are kept.

## [1.11.2]
- **Root folders:** adding and removing root folders never waits for a scan.
- **Root folders:** Edit → Folder moves a root folder together with all its saved data.
- **Root folders:** a **Reconnect** offer appears for data left under an old location.

## [1.11.1]
- **Phones:** posters start at Small size.
- **Activity:** titles in Activity open the Theme Browser.
- **YouTube:** a cookies.txt upload accepts any `.txt` file name and is tested automatically.
- **YouTube search:** a step-by-step API key guide.
- **Root folders:** a folder that is already used through another path is refused.

## [1.11.0]
- **Shows and Movies:** four root-folder types: Anime TV, Anime Movies, Shows, Movies.
- **Sources:** Shows use Plex TV Themes and ThemerrDB; Movies use ThemerrDB (by TMDB or IMDb id).
- **YouTube search:** optional, preview only. It needs your own API key and cookies.txt, and stays within a daily quota.
- **Library and dashboard:** Library tabs, and per-type statistics on the dashboard.

## [1.10.0]
- **Rebrand:** Themearr is now **Splicarr**, with a new logo, icons and wording. Settings, data and backups are unchanged.

## [1.9.14]
- **Clearer status:** a clearer status line, and honest connection labels.
- **API security:** the API key is accepted only in a header; added key scope and request limits; backup downloads ask for your password.
- **Posters:** poster matching by Sonarr/Radarr id.
- **Rate limits:** per-source rate limits.

## [1.9.13]
- Automatic backups with an interval and retention. Manual backups are never deleted automatically.

## [1.9.12]
- Polling stops when you are signed out.
- Clearer wording for the scope of bulk actions.
- A menu drawer on tablets and phones.

## [1.9.11]
- "Why this match" on title pages.
- **Try again** for theme lookups.
- Poster search results ranked by quality.

## [1.9.10]
- **Sonarr/Radarr:** mappings are previewed before saving.
- Clearer "Unmatched" and "not scanned" wording.
- crontab.guru help for schedules.

## [1.9.9]
- Polish across the title pages, players, Theme Management, the System overview and Settings.

## [1.9.8]
- Settings and System redesigned with tabs, timezones and a wide-screen layout.
- Host settings, API key and trusted proxies.

## [1.9.6]
- Remote access: a public HTTPS address for a tunnel or reverse proxy.
- Activity as a System tab.

## [1.9.5]
- Library Health as a System tab.
- Recycle-bin settings.

## [1.9.4]
- Review extra theme files and keep one.
- A theme recycle bin with undo and a retention period.

## [1.9.3]
- Library Health and the Path Validator.
- Checks before installing.

## [1.9.2]
- Clear connection errors for Sonarr, Radarr, Plex and Jellyfin.
- One-click suggested mappings.

## [1.9.1]
- Sonarr-style permissions (owner, group, fix existing theme.mp3).
- Theme-source logos.

## [1.9.0]
- Bulk Theme Queue, secondary sources, Sonarr and Radarr, permissions, Download Clients.

## Earlier (1.5 – 1.7)
- Automatic themes, "Recently added", root folders.
- AniDB identification and AnimeThemes reliability.
- Manual posters and the Theme Browser.
- Restore from backup.
