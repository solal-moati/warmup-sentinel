"""Shared HTTP retry policy: transient failures (timeouts, 429, 5xx)
auto-retry with backoff, so a single network hiccup never fails an
unattended run.

allowed_methods=None retries every verb: the agent's writes are safe to
repeat — pausing a lead is idempotent, and lemlist rejects a duplicate
alert.
"""

from __future__ import annotations

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def mount_retries(session, total: int = 4, backoff_factor: float = 1.5):
    retry = Retry(
        total=total, connect=total, read=total, status=total,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=None,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
