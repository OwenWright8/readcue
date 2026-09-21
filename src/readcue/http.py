"""Minimal POST helper on the standard library (used for Ollama and Pushover)."""

from __future__ import annotations

import urllib.error
import urllib.request


def post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """Return (status, body) for any HTTP response, including 4xx/5xx.

    Connection failures and timeouts raise OSError (URLError and TimeoutError are subclasses).
    """
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
