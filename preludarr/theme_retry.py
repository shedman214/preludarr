# SPDX-License-Identifier: GPL-3.0-or-later
"""Persist temporary theme failures separately from matches that need human review.

The scheduler only retries missing titles when their cooldown is due. A manual
retry can still be requested explicitly. No install or file operation happens here.
"""
import json
import re
import time
import urllib.error

BASE_DELAY = 300
MAX_DELAY = 6 * 3600


class RetryLater(OSError):
    def __init__(self, reason, retry_after=0):
        super().__init__(reason)
        self.retry_after = retry_after


class InstallBusy(RuntimeError):
    pass


def provider_error(provider, error):
    """Classify errors at the provider boundary, not filesystem/FFmpeg errors."""
    if isinstance(error, RetryLater):
        return error
    if isinstance(error, urllib.error.HTTPError):
        if error.code not in (408, 425, 429) and not 500 <= error.code < 600:
            return None
        from ratelimit import retry_after_seconds
        wait = retry_after_seconds((error.headers or {}).get('Retry-After'), default=0)
        return RetryLater(f'{provider} returned HTTP {error.code}', min(wait, 7 * 86400))
    if isinstance(error, (OSError, json.JSONDecodeError)):
        return RetryLater(f'{provider} temporarily unavailable ({type(error).__name__})')
    return None


def provider_call(provider, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as error:
        temporary = provider_error(provider, error)
        if temporary is None or temporary is error:
            raise
        raise temporary from error


def old_provider_failure(detail, has_id_tag=False):
    """Recover only recognizable old transient errors, and the fixed 1.16.1
    folder-ID-tag 404. Do not automatically retry a year mismatch or ambiguity.
    """
    text = str(detail or '')
    match = re.match(r'^HTTP Error (\d{3}):', text)
    if match:
        code = int(match.group(1))
        return code in (408, 425, 429) or 500 <= code < 600 or (code == 404 and has_id_tag)
    return text.startswith('AnimeThemes asked Preludarr to slow down')


def detail(row):
    when = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(row['next_retry']))
    return f"Waiting for retry: {row['reason']} · no earlier than {when}"


def defer(app, path, reason, retry_after=0, now=None):
    """Store capped exponential backoff; keep provider-requested delays."""
    current = int(time.time() if now is None else now)
    with app.db() as c:
        previous = c.execute('SELECT attempts FROM theme_retry WHERE path=?', (str(path),)).fetchone()
        attempts = min(32, (previous[0] if previous else 0) + 1)
        delay = max(min(MAX_DELAY, BASE_DELAY * 2 ** min(attempts - 1, 12)), int(retry_after or 0))
        row = {'attempts': attempts, 'next_retry': current + delay, 'reason': str(reason)[:300]}
        c.execute('INSERT INTO theme_retry(path,attempts,next_retry,reason) VALUES(?,?,?,?) '
                  'ON CONFLICT(path) DO UPDATE SET attempts=excluded.attempts,next_retry=excluded.next_retry,reason=excluded.reason',
                  (str(path), row['attempts'], row['next_retry'], row['reason']))
    return detail(row)
