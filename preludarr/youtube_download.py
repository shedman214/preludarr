# SPDX-License-Identifier: GPL-3.0-or-later

"""Manual, confirmation-first YouTube audio acquisition for Themearr."""

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import urllib.parse
from pathlib import Path

import youtube_settings as cookie_settings

MAX_AUDIO = 40 * 1024 * 1024
MAX_DURATION = 600
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
DOWNLOAD_LOCK = threading.Lock()


def canonical_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid YouTube URL")

    try:
        parsed = urllib.parse.urlsplit(value.strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid YouTube URL") from None

    if (parsed.scheme != "https" or parsed.username is not None
            or parsed.password is not None or port is not None
            or parsed.fragment):
        raise ValueError("Only standard HTTPS YouTube URLs are accepted")

    hosts = {
        "youtube.com", "www.youtube.com",
        "m.youtube.com", "music.youtube.com", "youtu.be"
    }

    if host not in hosts:
        raise ValueError("URL must belong to YouTube")

    query = urllib.parse.parse_qs(
        parsed.query, keep_blank_values=True
    )

    if "list" in query or "index" in query:
        raise ValueError("Playlists are not supported")

    if host == "youtu.be":
        video = parsed.path.removeprefix("/")
    elif parsed.path == "/watch":
        values = query.get("v", [])
        if len(values) != 1:
            raise ValueError("Invalid YouTube video ID")
        video = values[0]
    elif parsed.path.startswith(("/shorts/", "/live/")):
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2:
            raise ValueError("Invalid YouTube video URL")
        video = parts[1]
    else:
        raise ValueError("Unsupported YouTube video URL")

    if not VIDEO_ID.fullmatch(video):
        raise ValueError("Invalid YouTube video ID")

    return "https://www.youtube.com/watch?v=" + video, video


def cookie_arguments(config):
    state = cookie_settings.status(config)

    if not state.get("configured"):
        return []

    if not state.get("private"):
        raise ValueError("YouTube cookies must have private permissions")

    path = cookie_settings._path(config)
    info = path.lstat()

    if (not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("Unsafe YouTube cookie configuration")

    return ["--cookies", str(path)]


def run_ytdlp(arguments, timeout):
    __import__("ratelimit").wait("youtube")  # V1.9.14: every yt-dlp run is spaced (YouTube blocks busy addresses)
    executable = shutil.which("yt-dlp")

    if not executable:
        raise RuntimeError("yt-dlp is not installed in Preludarr")

    command = [
        executable, "--ignore-config", "--no-playlist",
        "--no-warnings", "--no-progress",
        "--socket-timeout", "15",
        "--retries", "2",
        "--fragment-retries", "2",
    ] + arguments

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("YouTube download timed out") from None

    if result.returncode:
        raise RuntimeError(
            "YouTube extraction failed. The video may be unavailable "
            "or require valid YouTube cookies."
        )

    if len(result.stdout) > 2 * 1024 * 1024:
        raise ValueError("YouTube metadata exceeds the permitted size")

    return result.stdout


def inspect_video(url, config):
    canonical, expected_id = canonical_url(url)

    raw = run_ytdlp(
        cookie_arguments(config)
        + ["--skip-download", "--dump-single-json", canonical],
        timeout=75
    )

    try:
        metadata = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid YouTube metadata") from None

    if not isinstance(metadata, dict):
        raise ValueError("Invalid YouTube metadata")

    if (metadata.get("id") != expected_id
            or metadata.get("extractor_key") != "Youtube"):
        raise ValueError("Unexpected YouTube video identity")

    duration = metadata.get("duration")

    if (not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not 0 < duration <= MAX_DURATION):
        raise ValueError(
            "Video must have a known duration of 10 minutes or less"
        )

    if metadata.get("is_live"):
        raise ValueError("Live streams are not supported")

    title = str(metadata.get("title") or "YouTube theme")[:180]

    return {
        "url": canonical,
        "video_id": expected_id,
        "title": title,
        "duration": duration,
    }


def download_to_stage(url, config):
    """Download temporary source audio. Never install a theme here."""

    __import__("ratelimit").take_hourly("youtube_downloads")  # V1.9.14: hourly cap (plain error when reached)
    config = Path(config)

    if not config.is_dir() or config.is_symlink():
        raise ValueError("Invalid configuration directory")

    if not DOWNLOAD_LOCK.acquire(blocking=False):
        raise RuntimeError("Another YouTube download is in progress")

    stage = None

    try:
        details = inspect_video(url, config)

        stage = Path(tempfile.mkdtemp(
            prefix="themearr-youtube-",
            dir=config
        ))
        os.chmod(stage, 0o700)

        template = str(stage / "%(id)s.%(ext)s")

        run_ytdlp(
            cookie_arguments(config)
            + [
                "-f", "bestaudio",
                "--max-filesize", "40M",
                "--no-part",
                "--no-write-info-json",
                "--no-write-thumbnail",
                "--output", template,
                details["url"]
            ],
            timeout=180
        )

        entries = list(stage.iterdir())

        if len(entries) != 1:
            raise ValueError("Unexpected downloaded files")

        audio = entries[0]
        info = audio.lstat()

        if (not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not 1024 <= info.st_size <= MAX_AUDIO):
            raise ValueError("Downloaded audio failed validation")

        if not audio.name.startswith(details["video_id"] + "."):
            raise ValueError("Downloaded video identity changed")

        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "a:0",
                    "-show_entries", "stream=codec_type",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(audio)
                ],
                capture_output=True,
                timeout=20,
                check=False
            )
        except subprocess.TimeoutExpired:
            raise ValueError("Audio validation timed out") from None

        if result.returncode or result.stdout.strip() != b"audio":
            raise ValueError("Downloaded file contains no usable audio")

        details["stage"] = stage
        details["audio"] = audio
        details["bytes"] = info.st_size

        return details

    except Exception:
        if stage is not None:
            cleanup_stage(stage, config)
        raise

    finally:
        DOWNLOAD_LOCK.release()


def cleanup_stage(stage, config):
    """Remove only a Themearr-owned temporary YouTube directory."""

    stage = Path(stage)
    config = Path(config)

    if (stage.parent != config
            or not stage.name.startswith("themearr-youtube-")
            or stage.is_symlink()):
        raise ValueError("Refusing unsafe staging cleanup")

    if stage.is_dir():
        shutil.rmtree(stage)
