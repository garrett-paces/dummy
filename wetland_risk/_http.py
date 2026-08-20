"""Tiny retrying HTTP helper.

We deliberately don't take a hard dependency on ``httpx``/``aiohttp``
so this module works in constrained environments; ``requests`` is the
only third-party HTTP dependency.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60  # seconds
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2.0  # seconds


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> dict[str, Any]:
    return _request_json("GET", url, params=params, timeout=timeout,
                         retries=retries, backoff=backoff)


def post_json(
    url: str,
    data: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> dict[str, Any]:
    return _request_json("POST", url, data=data, timeout=timeout,
                         retries=retries, backoff=backoff)


def _request_json(method: str, url: str, **kwargs) -> dict[str, Any]:
    timeout = kwargs.pop("timeout")
    retries = kwargs.pop("retries")
    backoff = kwargs.pop("backoff")

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            payload = resp.json()
            # ArcGIS REST returns 200 with an `error` object on failure
            if isinstance(payload, dict) and "error" in payload:
                raise RuntimeError(f"ArcGIS error: {payload['error']}")
            return payload
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last_exc = e
            if attempt >= retries:
                break
            wait = backoff * (2 ** (attempt - 1))
            log.warning("%s %s failed (%s); retry %d/%d in %.1fs",
                        method, url, e, attempt, retries, wait)
            time.sleep(wait)

    raise RuntimeError(f"{method} {url} failed after {retries} attempts") from last_exc
