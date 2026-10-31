# SPDX-License-Identifier: GPL-3.0-or-later

"""Manual YouTube theme review, confirmation, and cancellation."""

import html
import os
import re
import stat
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import custom_upload as uploads
import youtube_download as youtube

PENDING_SECONDS = 300
_PENDING = {}
_LOCK = threading.Lock()
_REVIEW_LOCK = threading.Lock()
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,128}\Z")


def enabled():
    return os.getenv("THEMEARR_MANUAL_INSTALL_ENABLED", "0") == "1"


def read_form(handler, limit=4096):
    if handler.headers.get("Transfer-Encoding"):
        raise ValueError("Transfer-Encoding is unsupported")

    lengths = handler.headers.get_all("Content-Length") or []

    if len(lengths) != 1 or not lengths[0].isdigit():
        raise ValueError("Content-Length required")

    size = int(lengths[0])

    if not 1 <= size <= limit:
        raise ValueError("Invalid request size")

    content_type = handler.headers.get("Content-Type", "").lower()

    if not content_type.startswith(
        "application/x-www-form-urlencoded"
    ):
        raise ValueError("URL-encoded form required")

    handler.connection.settimeout(30)
    body = handler.rfile.read(size)

    if len(body) != size:
        raise ValueError("Incomplete request")

    try:
        form = urllib.parse.parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True
        )
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid form data") from None

    if any(len(values) != 1 for values in form.values()):
        raise ValueError("Duplicate form fields")

    return {key: values[0] for key, values in form.items()}


def expire(config):
    now = time.monotonic()

    for token, item in list(_PENDING.items()):
        if item["expiry"] <= now:
            youtube.cleanup_stage(item["stage"], config)
            del _PENDING[token]


def review(handler, config, roots, db, issue_confirmation):
    if not enabled():
        return handler.send(
            403, "Manual installations disabled", "text/plain"
        )

    if not _REVIEW_LOCK.acquire(blocking=False):
        return handler.send(
            429, "Another YouTube review is running", "text/plain"
        )

    details = None

    try:
        try:
            form = read_form(handler)

            if set(form) != {"path", "youtube_url"}:
                raise ValueError("Anime path and YouTube URL required")

            folder, anime_name = uploads._destination(
                form["path"], roots, db
            )

            youtube.canonical_url(form["youtube_url"])

            with _LOCK:
                expire(config)

                if len(_PENDING) >= 2:
                    return handler.send(
                        429,
                        "Two YouTube reviews are awaiting confirmation",
                        "text/plain"
                    )

            details = youtube.download_to_stage(
                form["youtube_url"], config
            )

            video_id = details["video_id"]

            token = issue_confirmation(
                folder, "youtube_manual", video_id
            )

            item = {
                "expiry": time.monotonic() + PENDING_SECONDS,
                "stage": details["stage"],
                "audio": details["audio"],
                "folder": str(folder),
                "video_id": video_id,
                "title": str(details.get("title") or "")[:120],  # V1.7.3: shown in Recent activity
            }

            with _LOCK:
                expire(config)
                _PENDING[token] = item

            existing = (folder / "theme.mp3").exists()

            warning = (
                "An existing theme.mp3 will be replaced "
                "only if you confirm."
                if existing else
                "No existing theme.mp3 was detected."
            )

            escape = html.escape

            page = (
                '<!doctype html><html><head>'
                '<meta name="viewport" '
                'content="width=device-width,initial-scale=1">'
                '<title>Review YouTube Theme</title></head>'
                '<body><main style="max-width:760px;'
                'margin:3rem auto;font-family:sans-serif">'
                '<h2>Review YouTube Theme</h2>'
                '<p><strong>Anime:</strong> '
                + escape(anime_name) + '</p>'
                '<p><strong>YouTube video:</strong> '
                + escape(details["title"]) + '</p>'
                '<p><strong>Duration:</strong> '
                + str(round(details["duration"])) + ' seconds</p>'
                '<p><strong>Audio size:</strong> '
                + f'{details["bytes"]/1048576:.2f}' + ' MB</p>'
                '<p><strong>Destination:</strong> theme.mp3</p>'
                '<p>' + escape(warning) + '</p>'
                '<p>Confirmation expires in five minutes.</p>'
                '<h3>Listen to downloaded audio</h3>'
                '<p>Preview this audio before replacing your theme.</p>'
                '<audio controls preload="metadata" style="width:100%" '
                'src="/preview-youtube-theme?token='
                + escape(token, quote=True) + '"'
                '>Your browser cannot play this audio format.</audio>'
                '<p><a href="https://www.youtube.com/watch?v='
                + escape(video_id, quote=True)
                + '" target="_blank" rel="noopener noreferrer" '
                'referrerpolicy="no-referrer">Open original on YouTube</a></p>'
                '<form method="post" action="/confirm-youtube-theme">'
                '<input type="hidden" name="token" value="'
                + escape(token, quote=True) + '">'
                '<button type="submit">Confirm and Install</button>'
                '</form>'
                '<form method="post" action="/cancel-youtube-theme">'
                '<input type="hidden" name="token" value="'
                + escape(token, quote=True) + '">'
                '<button type="submit">Cancel</button>'
                '</form></main></body></html>'
            )

            return handler.send(200, page, "text/html")

        except (ValueError, RuntimeError, OSError) as error:
            if details is not None:
                youtube.cleanup_stage(details["stage"], config)

            return handler.send(
                400,
                "YouTube review unavailable: " + str(error),
                "text/plain"
            )

    finally:
        _REVIEW_LOCK.release()


def confirm(handler, config, roots, db,
            take_confirmation, install):
    if not enabled():
        return handler.send(
            403, "Manual installations disabled", "text/plain"
        )

    try:
        form = read_form(handler, limit=2048)

        if set(form) != {"token"}:
            raise ValueError("Confirmation token required")

        token = form["token"]

        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("Invalid confirmation token")

    except ValueError:
        return handler.send(
            400, "Invalid confirmation request", "text/plain"
        )

    with _LOCK:
        expire(config)
        item = _PENDING.pop(token, None)

    if item is None:
        return handler.send(
            409, "Review expired or already used", "text/plain"
        )

    try:
        folder, _ = uploads._destination(
            item["folder"], roots, db
        )

        fingerprint = take_confirmation(
            token, folder,
            "youtube_manual", item["video_id"]
        )

        if fingerprint is None:
            return handler.send(
                409,
                "Confirmation expired or theme changed",
                "text/plain"
            )

        audio = Path(item["audio"])
        information = audio.lstat()

        if (not stat.S_ISREG(information.st_mode)
                or information.st_nlink != 1
                or not 1024 <= information.st_size
                <= youtube.MAX_AUDIO):
            raise ValueError("Staged audio is invalid")

        installed = install(
            folder, audio,
            replace=True,
            expected_fingerprint=fingerprint,
            anime_slug="youtube_manual",
            video_id=item["video_id"],
            activity_label=item.get("title") or None
        )

        with db() as connection:
            connection.execute(
                "UPDATE shows SET state=?,detail=?,updated=? "
                "WHERE path=?",
                (
                    "installed",
                    "Manual YouTube theme installation",
                    time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),  # V1.9.14: UTC ISO like every other writer
                    item["folder"]
                )
            )

        return handler.send(
            200,
            "YouTube theme installed successfully: "
            + str(installed),
            "text/plain"
        )

    except (
        OSError, ValueError, RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired
    ):
        return handler.send(
            409,
            "YouTube theme installation failed. "
            "The existing theme was not intentionally replaced.",
            "text/plain"
        )

    finally:
        youtube.cleanup_stage(item["stage"], config)


def cancel(handler, config):
    try:
        form = read_form(handler, limit=2048)

        if set(form) != {"token"}:
            raise ValueError("Token required")

        token = form["token"]

        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("Invalid token")

    except ValueError:
        return handler.send(
            400, "Invalid cancellation request", "text/plain"
        )

    with _LOCK:
        expire(config)
        item = _PENDING.pop(token, None)

    if item is not None:
        youtube.cleanup_stage(item["stage"], config)

    return handler.send(
        200, "YouTube theme review canceled", "text/plain"
    )


def preview(handler, config, token):
    """Authenticated, read-only streaming of a pending YouTube review."""

    import urllib.parse

    if not enabled():
        return handler.send(403, "Preview disabled", "text/plain")

    if not TOKEN_PATTERN.fullmatch(token or ""):
        return handler.send(404, "Preview unavailable", "text/plain")

    config = Path(config)
    fd = None
    audio = None

    try:
        with _LOCK:
            expire(config)
            item = _PENDING.get(token)

            if item is None:
                return handler.send(
                    404, "Preview expired or unavailable", "text/plain"
                )

            stage = Path(item["stage"])
            audio = Path(item["audio"])

            if (
                stage.parent != config
                or not stage.name.startswith("themearr-youtube-")
                or audio.parent != stage
            ):
                return handler.send(
                    404, "Invalid preview location", "text/plain"
                )

            stage_info = stage.lstat()
            if not stat.S_ISDIR(stage_info.st_mode):
                return handler.send(
                    404, "Invalid preview directory", "text/plain"
                )

            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(audio, flags)

            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not 1024 <= info.st_size <= youtube.MAX_AUDIO
            ):
                os.close(fd)
                fd = None
                return handler.send(
                    404, "Invalid preview audio", "text/plain"
                )

        size = info.st_size

        mime = {
            ".webm": "audio/webm",
            ".m4a": "audio/mp4",
            ".mp4": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
            ".opus": "audio/ogg",
        }.get(audio.suffix.lower(), "application/octet-stream")

        requested = handler.headers.get("Range", "")
        start, end = 0, size - 1

        def invalid_range():
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.send_header("Content-Length", "0")
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()

        if requested:
            match = re.fullmatch(
                r"bytes=(\d{0,20})-(\d{0,20})",
                requested
            )

            if not match or not any(match.groups()):
                return invalid_range()

            first, last = match.groups()

            if first:
                start = int(first)
                if last:
                    end = min(int(last), size - 1)
            else:
                suffix = int(last)
                start = max(0, size - suffix) if suffix else size

            if start >= size or start > end:
                return invalid_range()

        length = end - start + 1

        handler.send_response(206 if requested else 200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(length))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "private, no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")

        if requested:
            handler.send_header(
                "Content-Range", f"bytes {start}-{end}/{size}"
            )

        handler.end_headers()

        with os.fdopen(fd, "rb") as stream:
            fd = None
            stream.seek(start)
            remaining = length

            try:
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    except (OSError, ValueError):
        if fd is None:
            return
        return handler.send(
            404, "Preview unavailable", "text/plain"
        )

    finally:
        if fd is not None:
            os.close(fd)
