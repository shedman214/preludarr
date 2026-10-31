# SPDX-License-Identifier: GPL-3.0-or-later

"""Private storage for manually supplied yt-dlp Netscape cookies."""

import os
import stat
import tempfile
from pathlib import Path

COOKIE_NAME = "themearr-youtube-cookies.txt"
MAX_SIZE = 2 * 1024 * 1024


def _directory(config):
    path = Path(config)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Invalid configuration directory")
    return path


def _path(config):
    return _directory(config) / COOKIE_NAME


def _validate(data):
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_SIZE:
        raise ValueError("Invalid cookie file size")

    if b"\x00" in data:
        raise ValueError("Invalid cookie file")

    try:
        contents = data.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("Cookie file must be UTF-8 text")

    lines = contents.splitlines()

    if not lines or not (
        lines[0].startswith("# Netscape HTTP Cookie File")
        or lines[0].startswith("# HTTP Cookie File")
    ):
        raise ValueError("Expected Netscape cookies.txt format")

    valid = 0

    for line in lines[1:]:
        if not line.strip():
            continue

        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue

        fields = line.split("\t")

        if len(fields) != 7:
            raise ValueError("Invalid Netscape cookie entry")

        domain, subdomains, path, secure, expiry, name, value = fields

        if domain.startswith("#HttpOnly_"):
            domain = domain[len("#HttpOnly_"):]

        if not domain or not path.startswith("/"):
            raise ValueError("Invalid cookie domain or path")

        if subdomains not in ("TRUE", "FALSE"):
            raise ValueError("Invalid cookie subdomain flag")

        if secure not in ("TRUE", "FALSE"):
            raise ValueError("Invalid cookie secure flag")

        try:
            int(expiry)
        except ValueError:
            raise ValueError("Invalid cookie expiration")

        if not name:
            raise ValueError("Invalid cookie name")

        valid += 1

    if not valid:
        raise ValueError("Cookie file contains no cookie entries")

    return len(data)


def _existing(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None

    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Unsafe cookie file detected")

    return info


def status(config):
    path = _path(config)
    info = _existing(path)

    if info is None:
        return {"configured": False}

    return {
        "configured": True,
        "size": info.st_size,
        "private": stat.S_IMODE(info.st_mode) == 0o600,
    }


def install(config, data):
    _validate(data)

    folder = _directory(config)
    destination = folder / COOKIE_NAME

    _existing(destination)

    fd, temporary = tempfile.mkstemp(
        prefix=".themearr-cookies-",
        dir=str(folder)
    )

    try:
        os.fchmod(fd, 0o600)

        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())

        _existing(destination)
        os.replace(temporary, destination)

    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    return status(config)


def remove(config):
    path = _path(config)

    if _existing(path) is None:
        return False

    path.unlink()
    return True
