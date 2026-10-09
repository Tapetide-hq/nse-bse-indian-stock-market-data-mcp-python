"""Refresh-token → short-lived access-token exchange against the remote's /token."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

# Refresh this long before expiry so a token never dies mid-request.
EXPIRY_MARGIN_SECONDS = 300


class AuthError(Exception):
    pass


class TokenManager:
    def __init__(self, base_url: str, refresh_token: str, user_agent: Callable[[], str], timeout: float):
        self._url = f"{base_url}/token"
        self._refresh_token = refresh_token
        self._user_agent = user_agent
        self._timeout = timeout
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._access_token is None or time.monotonic() >= self._expires_at:
                self._refresh()
            assert self._access_token is not None
            return self._access_token

    def invalidate(self) -> None:
        with self._lock:
            self._access_token = None

    def _refresh(self) -> None:
        body = urllib.parse.urlencode(
            {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        ).encode()
        req = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self._user_agent(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as res:
                data = json.loads(res.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            raise AuthError(f"Token refresh failed ({e.code}): {detail}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise AuthError(f"Token refresh failed: {e}") from None
        self._access_token = data["access_token"]
        ttl = max(float(data.get("expires_in", 0)) - EXPIRY_MARGIN_SECONDS, 0)
        self._expires_at = time.monotonic() + ttl
