# SPDX-License-Identifier: GPL-3.0-or-later
# Preludarr image, built from the official Python Alpine image.
# Build: docker build --build-arg VERSION=1.12.0-dev -t preludarr:dev .
FROM python:3.12-alpine3.24@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8

ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED=unknown

# ffmpeg: converts theme audio to MP3 · deno: JavaScript runtime yt-dlp needs for YouTube · su-exec: drop root at start-up (non-root runtime)
RUN apk add --no-cache ffmpeg deno su-exec tzdata \
 && find /media -mindepth 1 -maxdepth 1 -type d -empty -delete   # cdrom/floppy/usb from the base image would show up in the folder picker

# yt-dlp and its dependencies, every file checked against the hashes in requirements.txt
COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes --no-deps -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt \
 && { apk info -v 2>/dev/null | sort; pip list --format=freeze 2>/dev/null; } > /usr/share/preludarr-packages.txt \
 && python3 -m pip uninstall -y -q pip   # pip is only needed to build the image (and is the only thing scanners flag)

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/preludarr-entrypoint

WORKDIR /app
COPY LICENSE NOTICE.md CHANGELOG.md docker/THIRD_PARTY_PACKAGES.txt ./
COPY preludarr/ ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8787 \
    CONFIG_DIR=/config \
    UMASK=002

VOLUME ["/config"]
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8787'), timeout=8)" || exit 1

LABEL org.opencontainers.image.title="Preludarr" \
      org.opencontainers.image.description="Theme songs for your media library. Self-hosted management for anime, TV shows and movies." \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.source="https://github.com/shedman214/preludarr" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

# Starts as root only to give /config to PUID:PGID, then runs Preludarr as that user (docker/entrypoint.sh).
ENTRYPOINT ["/usr/local/bin/preludarr-entrypoint"]
CMD ["python3", "/app/app.py"]
