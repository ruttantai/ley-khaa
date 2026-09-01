import pytest
import requests

from ley_khaa.vision.fetcher import FetchRefused, ImageFetcher

ALLOWED = frozenset({"files.slack.com", "cdn.discordapp.com"})
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


class _Response:
    def __init__(self, body=PNG, status=200, content_type="image/png"):
        self._body = body
        self.status_code = status
        self.headers = {"Content-Type": content_type}

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        pass


class _Transport:
    """Records what the fetcher tried to send."""

    def __init__(self, response=None):
        self.response = response or _Response()
        self.calls = []

    def __call__(self, url, *, headers, timeout, allow_redirects):
        self.calls.append(
            {"url": url, "headers": headers, "timeout": timeout, "allow_redirects": allow_redirects}
        )
        return self.response


def _fetcher(transport, **over):
    return ImageFetcher(
        allowed_hosts=over.pop("allowed_hosts", ALLOWED),
        max_bytes=over.pop("max_bytes", 1024),
        slack_token=over.pop("slack_token", "xoxb-secret"),
        transport=transport,
    )


def test_an_allowlisted_slack_url_is_fetched():
    t = _Transport()
    data, media_type = _fetcher(t).fetch("https://files.slack.com/f/abc.png")
    assert data == PNG
    assert media_type == "image/png"


def test_the_slack_token_is_sent_to_a_slack_host():
    t = _Transport()
    _fetcher(t).fetch("https://files.slack.com/f/abc.png")
    assert t.calls[0]["headers"]["Authorization"] == "Bearer xoxb-secret"


def test_the_slack_token_is_NEVER_sent_to_a_non_slack_host():
    """THE rule of this module. A payload-supplied URL on an allowlisted but
    non-Slack host must not receive the workspace's bot token."""
    t = _Transport()
    _fetcher(t).fetch("https://cdn.discordapp.com/attachments/1/2/a.png")

    headers = t.calls[0]["headers"]
    assert "Authorization" not in headers
    assert "xoxb-secret" not in repr(headers)


def test_redirects_are_not_followed():
    """A 302 to an off-allowlist host would defeat both the allowlist and the
    token rule, because the check already passed by then."""
    t = _Transport()
    _fetcher(t).fetch("https://files.slack.com/f/abc.png")
    assert t.calls[0]["allow_redirects"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://files.slack.com/f/abc.png",          # not https
        "https://evil.example.com/a.png",            # not allowlisted
        "https://files.slack.com.evil.com/a.png",    # suffix trick (attacker's host as a prefix)
        "https://evilfiles.slack.com/a.png",         # suffix trick (allowlisted name as a bare suffix)
        "ftp://files.slack.com/a.png",               # not http at all
        "https://127.0.0.1/a.png",                   # not allowlisted
        "not a url",
        "https://[::1/a.png",                        # malformed netloc: urlparse itself raises ValueError
    ],
)
def test_a_disallowed_url_is_refused_before_any_request(url):
    t = _Transport()
    with pytest.raises(FetchRefused):
        _fetcher(t).fetch(url)
    assert t.calls == [], "a refused URL must never be requested"


def test_a_body_over_the_cap_is_refused():
    t = _Transport(_Response(body=b"y" * 5000))
    with pytest.raises(FetchRefused):
        _fetcher(t, max_bytes=1024).fetch("https://files.slack.com/f/big.png")


def test_the_cap_is_enforced_on_the_body_not_on_content_length():
    """Content-Length is attacker-controlled. A small declared length with a
    huge body must still be refused."""
    response = _Response(body=b"y" * 5000)
    response.headers["Content-Length"] = "10"
    with pytest.raises(FetchRefused):
        _fetcher(_Transport(response), max_bytes=1024).fetch("https://files.slack.com/f/lie.png")


def test_the_cap_is_enforced_while_streaming_not_after_the_whole_body_arrives():
    """A regression to accumulate-then-check would read a hostile server's
    entire body into memory before refusing it — the point of streaming the
    cap is that a huge body is abandoned within a few chunks, not after it
    has all been buffered."""
    yielded = []

    class _StreamingResponse:
        status_code = 200
        headers = {"Content-Type": "image/png"}

        def iter_content(self, chunk_size):
            # Far more chunks than a correctly-streamed cap should ever need
            # to look at; each chunk alone already exceeds max_bytes below.
            for _ in range(200):
                yielded.append(1)
                yield b"y" * 8192

        def close(self):
            pass

    t = _Transport(_StreamingResponse())
    with pytest.raises(FetchRefused):
        _fetcher(t, max_bytes=1024).fetch("https://files.slack.com/f/huge.png")

    assert len(yielded) <= 2, (
        "the cap must be enforced as chunks arrive, not after the generator "
        f"has been fully drained (drained {len(yielded)} of 200 chunks)"
    )


def test_a_connection_error_is_refused_not_raised():
    """A ReadTimeout or connection error on an allowlisted host must become a
    FetchRefused, not an unwrapped requests exception escaping the boundary
    — Task 6's dead-letter handler only catches FetchRefused."""

    class _FailingTransport:
        def __call__(self, url, *, headers, timeout, allow_redirects):
            # A real RequestException.__str__ can carry the full request URL
            # (query string, credentials and all) — deliberately included
            # here so the assertion below can prove it never leaks.
            raise requests.exceptions.ReadTimeout(
                "https://files.slack.com/f/big.png?t=xoxb-secret timed out"
            )

    with pytest.raises(FetchRefused) as exc_info:
        _fetcher(_FailingTransport()).fetch("https://files.slack.com/f/big.png")
    assert "xoxb-secret" not in str(exc_info.value)


def test_a_connection_error_mid_body_is_refused_not_raised():
    """A ChunkedEncodingError partway through the read must also become a
    FetchRefused, not escape from inside the streaming loop."""

    class _BreakingResponse:
        status_code = 200
        headers = {"Content-Type": "image/png"}

        def iter_content(self, chunk_size):
            yield b"partial"
            raise requests.exceptions.ChunkedEncodingError("stream broke")

        def close(self):
            pass

    t = _Transport(_BreakingResponse())
    with pytest.raises(FetchRefused):
        _fetcher(t).fetch("https://files.slack.com/f/broken.png")


def test_a_non_image_content_type_is_refused():
    t = _Transport(_Response(content_type="text/html"))
    with pytest.raises(FetchRefused):
        _fetcher(t).fetch("https://files.slack.com/f/login.html")


def test_a_non_200_is_refused():
    t = _Transport(_Response(status=403))
    with pytest.raises(FetchRefused):
        _fetcher(t).fetch("https://files.slack.com/f/private.png")


def test_the_refusal_reason_never_contains_the_token():
    """A FetchRefused reaches a dead letter, and a dead letter is read by a
    human in a browser."""
    t = _Transport(_Response(status=403))
    try:
        _fetcher(t).fetch("https://files.slack.com/f/private.png")
    except FetchRefused as exc:
        assert "xoxb-secret" not in str(exc)
    else:
        pytest.fail("expected FetchRefused")
