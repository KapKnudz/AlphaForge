"""HTTP transport with MAX_RETRIES=3, backoff, Retry-After, 0.5-1s sleep."""

from __future__ import annotations

import random
import time
from typing import Any

import requests

MAX_RETRIES = 3


def sanitize_provider_error(exc: BaseException) -> str:
    """Keep provider failures useful without exposing response bodies or URLs."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    name = type(exc).__name__
    if status is None:
        return name
    return f"{name} status={status}"


def request_with_retry(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
    max_retries: int = MAX_RETRIES,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, params=params, headers=headers, timeout=timeout)
            # Honor Retry-After on 429/503
            if resp.status_code in (429, 503):
                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        delay = int(retry_after)
                    except ValueError:
                        delay = 2**attempt
                else:
                    delay = 2**attempt
                if attempt < max_retries:
                    time.sleep(min(delay, 10))
                    continue
            # Retry on 5xx
            if resp.status_code >= 500 and attempt < max_retries:
                time.sleep(2**attempt)
                continue
            return resp
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(2**attempt)
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("request_with_retry exhausted retries")


def sleep_between_batches() -> None:
    time.sleep(random.uniform(0.5, 1.0))
