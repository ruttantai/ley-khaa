"""Turn a channel attachment URL into image bytes, under an explicit boundary.

This is the phase's security surface. Resolving a Slack `url_private` means
handing the workspace's bot token to an HTTP client whose URL arrived inside a
platform payload, so every rule below exists to bound what that can do.
"""
from __future__ import annotations

from urllib.parse import urlparse

_SLACK_HOSTS = frozenset({"files.slack.com"})
_CHUNK = 64 * 1024


class FetchRefused(Exception):
    """The URL was not fetched. The reason is safe to show a human."""


class ImageFetcher:
    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int,
        slack_token: str = "",
        timeout: float = 10.0,
        transport=None,
    ) -> None:
        self.allowed_hosts = allowed_hosts
        self.max_bytes = max_bytes
        self._slack_token = slack_token
        self.timeout = timeout
        # Injected so the boundary is testable without a network. Defaults to
        # requests.get, which is already a dependency of the backend.
        self._transport = transport

    def _get(self, url, *, headers, timeout, allow_redirects):
        if self._transport is not None:
            return self._transport(url, headers=headers, timeout=timeout, allow_redirects=allow_redirects)
        import requests

        return requests.get(
            url, headers=headers, timeout=timeout, allow_redirects=allow_redirects, stream=True
        )

    def fetch(self, url: str) -> tuple[bytes, str]:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise FetchRefused(f"refusing a non-https image url ({parsed.scheme or 'no scheme'})")
        host = parsed.hostname or ""
        # Exact host match, never a suffix test: "files.slack.com.evil.com"
        # ends with an allowlisted name and is a different host entirely.
        if host not in self.allowed_hosts:
            raise FetchRefused(f"image host {host!r} is not allowlisted")

        headers = {}
        # THE rule: the token goes to Slack and nowhere else. An allowlisted
        # host is not automatically a trusted recipient of a credential.
        if host in _SLACK_HOSTS and self._slack_token:
            headers["Authorization"] = f"Bearer {self._slack_token}"

        response = self._get(
            url,
            headers=headers,
            timeout=self.timeout,
            # A 302 to an off-allowlist host would defeat both checks above,
            # because they already passed on the original URL.
            allow_redirects=False,
        )
        try:
            if response.status_code != 200:
                raise FetchRefused(f"image url returned HTTP {response.status_code}")
            media_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            if not media_type.startswith("image/"):
                raise FetchRefused(f"image url served {media_type or 'no content type'}")

            body = bytearray()
            for chunk in response.iter_content(chunk_size=_CHUNK):
                body.extend(chunk)
                # Checked as the body arrives. Content-Length is written by the
                # server and cannot be trusted to bound anything.
                if len(body) > self.max_bytes:
                    raise FetchRefused(f"image exceeds {self.max_bytes} bytes")
            return bytes(body), media_type
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
