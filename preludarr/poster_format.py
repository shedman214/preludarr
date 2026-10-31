# SPDX-License-Identifier: GPL-3.0-or-later
"""Themearr Poster Format V1.3 -- safely convert WebP/PNG poster responses to JPEG.

No network, provider credentials, media library writes or image cache writes.
Existing JPEG bytes pass through without a conversion.
"""
import subprocess
import sys

MAX_BYTES = 3 * 1024 * 1024


def to_jpeg(content, content_type):
    """Return bounded JPEG bytes for an image/webp or image/png response.

    Conversion is isolated in a memory-limited, single-threaded FFmpeg child.
    The caller retains exclusive poster cache creation and DB behavior.
    """
    if not isinstance(content, bytes) or not (0 < len(content) <= MAX_BYTES):
        raise ValueError('Poster input is missing or too large')
    mime = str(content_type).split(';', 1)[0].strip().lower()
    if not mime.startswith('image/'):
        raise ValueError('Poster response is not an image')

    if content.startswith(b'\xff\xd8'):
        return content

    if mime == 'image/webp' and content.startswith(b'RIFF') and content[8:12] == b'WEBP':
        image_type = 'WebP'
    elif mime == 'image/png' and content.startswith(b'\x89PNG\r\n\x1a\n'):
        image_type = 'PNG'
    else:
        raise ValueError('Unsupported poster encoding or MIME/signature mismatch')

    # This child launcher is used rather than preexec_fn (unsafe with threads).
    # Limit FFmpeg's virtual address space to 768 MiB and conversion to 25s.
    launcher = (
        'import os,resource,sys;'
        'resource.setrlimit(resource.RLIMIT_AS,(805306368,805306368));'
        'os.execvp(sys.argv[1],sys.argv[1:])'
    )
    command = [
        sys.executable, '-c', launcher,
        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-threads', '1', '-f', 'image2pipe', '-i', 'pipe:0',
        '-an', '-frames:v', '1',
        '-vf', 'scale=w=min(720\\,iw):h=min(1080\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2',
        '-pix_fmt', 'yuvj420p', '-c:v', 'mjpeg', '-q:v', '5',
        '-f', 'image2pipe', 'pipe:1',
    ]
    try:
        result = subprocess.run(
            command, input=content, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=25, check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # Do not print FFmpeg stderr: errors could contain untrusted source data.
        raise ValueError(image_type + ' poster conversion failed (' + type(exc).__name__ + ')') from None

    output = result.stdout
    if not (4 <= len(output) <= MAX_BYTES and output.startswith(b'\xff\xd8')
            and output.endswith(b'\xff\xd9')):
        raise ValueError('Converted poster failed JPEG verification')
    return output
