"""One requests.Session factory for every outbound call: retries with backoff and Retry-After.

A server's Retry-After is honoured up to `MAX_RETRY_AFTER`. A longer demand (a throttling site
asking for an hour) is not waited out, since the worker would hold its lease and the token would
run out while it slept: the request fails with `RetryAfterTooLong`, a `RequestException`, so the
caller records that one row as failed and moves on.
"""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from patient_access import __version__

USER_AGENT = f"patient-access/{__version__}"
MAX_RETRY_AFTER = 60.0  # seconds


class RetryAfterTooLong(requests.RequestException):
    """The server asked us to wait longer than we will."""


class CappedRetry(Retry):
    """urllib3's Retry, refusing a Retry-After longer than MAX_RETRY_AFTER instead of sleeping."""

    def sleep_for_retry(self, response: Any = None) -> bool:
        wait = self.get_retry_after(response) if response is not None else None
        if wait is not None and wait > MAX_RETRY_AFTER:
            raise RetryAfterTooLong(f"the server asked to retry after {wait:.0f} s")
        return bool(super().sleep_for_retry(response))


def session(max_retries: int = 3, backoff: float = 0.5) -> requests.Session:
    s = requests.Session()
    retry = CappedRetry(
        total=max_retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),  # never replay a token POST: codes are single use
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers["User-Agent"] = USER_AGENT
    return s
