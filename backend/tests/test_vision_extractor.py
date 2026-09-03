import base64

from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.persistence.image_extraction_repository import (
    ImageExtractionRepository,
    sha256_of,
)
from ley_khaa.vision.contract import VisionExtraction
from ley_khaa.vision.extractor import VisionExtractor
from ley_khaa.vision.fetcher import FetchRefused, ImageFetcher

PNG = b"\x89PNG\r\n\x1a\nbytes"
B64 = base64.standard_b64encode(PNG).decode()


def _image(content=B64, name="chart.png"):
    return {"kind": "image", "name": name, "content": content}


class _CountingLLM:
    """Wraps the REAL offline client and counts calls.

    A canned fake would let a broken cache pass by returning the same thing
    twice; only a counter around the real client proves no call was made.
    """

    def __init__(self, inner=None, result=None):
        self.inner = inner or HeuristicLLM()
        self.result = result
        self.calls = []
        self.name = self.inner.name

    def extract_image(self, **kwargs):
        self.calls.append(kwargs)
        if self.result is not None:
            return self.result
        return self.inner.extract_image(**kwargs)

    def parse(self, **kwargs):
        return self.inner.parse(**kwargs)


class _StubFetcher:
    def __init__(self, result=(PNG, "image/png"), error=None):
        self.result = result
        self.error = error
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.result


def _extractor(session, **over):
    return VisionExtractor(
        llm=over.pop("llm", _CountingLLM()),
        extractions=ImageExtractionRepository(session),
        fetcher=over.pop("fetcher", _StubFetcher()),
        dead_letter=over.pop("dead_letter", None),
        enabled=over.pop("enabled", True),
    )


def test_an_inline_base64_image_is_extracted(session):
    llm = _CountingLLM(result=VisionExtraction(kind="table", content="a,b\n1,2", summary="a table"))
    row = _extractor(session, llm=llm).extract(_image())

    assert row.kind == "table"
    assert row.content == "a,b\n1,2"
    assert row.image_sha256 == sha256_of(PNG)
    assert len(llm.calls) == 1


def test_a_url_image_is_fetched_then_extracted(session):
    fetcher = _StubFetcher()
    llm = _CountingLLM(result=VisionExtraction(kind="text", content="hello", summary="a note"))

    row = _extractor(session, llm=llm, fetcher=fetcher).extract(
        _image(content="https://files.slack.com/f/a.png")
    )

    assert fetcher.urls == ["https://files.slack.com/f/a.png"]
    assert row.content == "hello"


def test_the_second_extraction_of_the_same_image_makes_NO_model_call(session):
    """The phase's cache claim, proven by a counter around the real client
    rather than by two runs happening to agree."""
    llm = _CountingLLM(result=VisionExtraction(kind="table", content="a,b\n1,2", summary="t"))
    extractor = _extractor(session, llm=llm)

    first = extractor.extract(_image())
    assert len(llm.calls) == 1

    second = extractor.extract(_image())

    assert llm.calls == llm.calls[:1], "a cache hit must make no further call"
    assert len(llm.calls) == 1
    assert second.content == first.content
    assert second.image_sha256 == first.image_sha256


def test_the_cache_is_keyed_on_bytes_not_on_filename(session):
    """The same screenshot pasted twice under different names is one call."""
    llm = _CountingLLM(result=VisionExtraction(kind="text", content="x", summary="s"))
    extractor = _extractor(session, llm=llm)

    extractor.extract(_image(name="one.png"))
    extractor.extract(_image(name="two.png"))

    assert len(llm.calls) == 1


def test_a_different_image_is_a_different_key(session):
    llm = _CountingLLM(result=VisionExtraction(kind="text", content="x", summary="s"))
    extractor = _extractor(session, llm=llm)

    extractor.extract(_image())
    extractor.extract(_image(content=base64.standard_b64encode(b"different").decode()))

    assert len(llm.calls) == 2


def test_with_no_vision_backend_the_record_says_it_was_not_read(session):
    """The offline path. The zero-account demo must complete (spec §3.6)."""
    row = _extractor(session, llm=_CountingLLM()).extract(_image())

    assert row.content == ""
    assert "chart.png" in row.summary
    assert row.model == "heuristic", "the manifest must credit who ACTUALLY did it"


def test_a_disabled_extractor_makes_no_call_and_returns_the_unread_record(session):
    llm = _CountingLLM(result=VisionExtraction(kind="table", content="a", summary="s"))
    row = _extractor(session, llm=llm, enabled=False).extract(_image())

    assert llm.calls == []
    assert row.content == ""


class _RecordingTransport:
    """Records every call the fetcher would have made, headers included --
    same shape as test_image_fetcher.py's _Transport. A disabled extractor
    must never reach this at all (review B4): before the fix, `enabled` was
    checked AFTER _bytes_for, so a disabled extractor still spent a full
    HTTP request (bot token attached, for a Slack host) on a url nobody
    asked to read."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, *, headers, timeout, allow_redirects):
        self.calls.append({"url": url, "headers": headers})
        raise AssertionError("a disabled extractor must never reach the transport")


def test_a_disabled_extractor_fetches_nothing_and_sends_no_token(session):
    """LEY_KHAA_VISION=off must turn the whole path off, not just the model
    call: this drives it through a REAL ImageFetcher over a REAL Slack
    url_private, and proves zero fetches happen and no Authorization header
    is ever produced -- not merely that the returned record is empty."""
    transport = _RecordingTransport()
    fetcher = ImageFetcher(
        allowed_hosts=frozenset({"files.slack.com"}),
        max_bytes=1024,
        slack_token="xoxb-secret",
        transport=transport,
    )
    llm = _CountingLLM(result=VisionExtraction(kind="table", content="a", summary="s"))
    extractor = _extractor(session, llm=llm, fetcher=fetcher, enabled=False)

    row = extractor.extract(_image(content="https://files.slack.com/f/a.png"))

    assert transport.calls == [], "a disabled extractor must fetch nothing at all"
    assert llm.calls == []
    assert row.content == ""
    assert row.model == ""


def test_a_refused_fetch_dead_letters_and_still_returns_a_record(session):
    dead = []
    fetcher = _StubFetcher(error=FetchRefused("image host 'evil.example.com' is not allowlisted"))

    row = _extractor(
        session, fetcher=fetcher, dead_letter=lambda **kw: dead.append(kw)
    ).extract(_image(content="https://evil.example.com/a.png"))

    assert row.content == "", "a refused fetch must not block the task"
    assert len(dead) == 1
    assert dead[0]["kind"] == "inbound"
    assert "not allowlisted" in dead[0]["reason"]


def test_a_model_error_dead_letters_and_still_returns_a_record(session):
    class _Boom:
        name = "anthropic"

        def extract_image(self, **kwargs):
            raise RuntimeError("the model is down")

    dead = []
    row = _extractor(session, llm=_Boom(), dead_letter=lambda **kw: dead.append(kw)).extract(_image())

    assert row.content == ""
    assert len(dead) == 1
    assert "the model is down" in dead[0]["reason"]


def test_a_failed_extraction_is_STORED_so_it_is_not_retried(session):
    """Otherwise every re-drive re-attempts a fetch that will fail again, and a
    task that repairs three times pays for it three times."""
    calls = []

    class _Boom:
        name = "anthropic"

        def extract_image(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("down")

    extractor = _extractor(session, llm=_Boom(), dead_letter=lambda **kw: None)
    extractor.extract(_image())
    extractor.extract(_image())

    assert len(calls) == 1, "the unread record must be cached like any other"


def test_a_non_image_attachment_is_refused(session):
    """The caller filters, but this is the last line before a text blob is sent
    to a vision model as if it were a picture."""
    llm = _CountingLLM()
    extractor = _extractor(session, llm=llm)
    row = extractor.extract({"kind": "table", "name": "a.csv", "content": "a,b\n1,2"})

    assert row.content == ""
    assert llm.calls == []


# -- fix round 1 -----------------------------------------------------------


def test_a_malformed_model_return_does_not_escape_extract(session):
    """A model can stop on max_tokens and hand back None with no exception
    (AnthropicLLM.parse has no guard for it) — that must degrade like any
    other model failure, not raise out of .extract() (spec §3.6)."""

    class _Malformed:
        name = "anthropic"

        def extract_image(self, **kwargs):
            return None

    dead = []
    row = _extractor(
        session, llm=_Malformed(), dead_letter=lambda **kw: dead.append(kw)
    ).extract(_image())

    assert row.content == ""
    assert len(dead) == 1
    assert "VisionExtraction" in dead[0]["reason"] or "NoneType" in dead[0]["reason"]


def test_a_stored_unread_record_is_retried_by_a_different_backend(session):
    """A row stored by one backend (or by no backend at all) must not freeze
    forever: a later drive with a different backend gets a fresh call."""
    heuristic = _CountingLLM()  # inner is the real HeuristicLLM, name="heuristic"
    first = _extractor(session, llm=heuristic).extract(_image())
    assert first.content == ""
    assert first.model == "heuristic"

    real = _CountingLLM(result=VisionExtraction(kind="text", content="hello", summary="s"))
    real.name = "anthropic"
    second = _extractor(session, llm=real).extract(_image())

    assert len(real.calls) == 1, "a differently-named backend must be given a fresh chance"
    assert second.content == "hello"


def test_a_stored_unread_record_from_no_backend_is_retried_once_enabled(session):
    """enabled=False -> True must not freeze on the disabled run's empty row."""
    disabled_llm = _CountingLLM(result=VisionExtraction(kind="table", content="a", summary="s"))
    first = _extractor(session, llm=disabled_llm, enabled=False).extract(_image())
    assert first.content == ""
    assert first.model == ""

    second = _extractor(session, llm=disabled_llm, enabled=True).extract(_image())

    assert len(disabled_llm.calls) == 1
    assert second.content == "a"


def test_a_same_backend_failure_stays_frozen(session):
    """The ruling: bounded retry needs a retry-count column, which is out of
    scope. A transient 503 from the SAME backend stays frozen, not retried."""
    calls = []

    class _Boom:
        name = "anthropic"

        def extract_image(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("down")

    boom = _Boom()
    _extractor(session, llm=boom, dead_letter=lambda **kw: None).extract(_image())
    _extractor(session, llm=boom, dead_letter=lambda **kw: None).extract(_image())

    assert len(calls) == 1


def test_a_data_uri_prefixed_base64_image_is_extracted(session):
    """Browser pastes arrive as `data:image/png;base64,...` — this phase
    exists so a pasted screenshot works, not just a bare base64 string."""
    llm = _CountingLLM(result=VisionExtraction(kind="text", content="ok", summary="s"))
    row = _extractor(session, llm=llm).extract(_image(content=f"data:image/png;base64,{B64}"))

    assert row.content == "ok"


def test_a_line_wrapped_base64_image_is_extracted(session):
    """coreutils `base64` and base64.encodebytes wrap output at 76 columns."""
    wrapped = base64.encodebytes(PNG).decode()
    assert "\n" in wrapped
    llm = _CountingLLM(result=VisionExtraction(kind="text", content="ok", summary="s"))
    row = _extractor(session, llm=llm).extract(_image(content=wrapped))

    assert row.content == "ok"


def test_a_disabled_extraction_credits_no_backend(session):
    """The manifest must not credit a client that made zero calls — and this
    is load-bearing for the retry-on-different-backend fix above."""
    llm = _CountingLLM(result=VisionExtraction(kind="table", content="a", summary="s"))
    row = _extractor(session, llm=llm, enabled=False).extract(_image())

    assert row.model == ""


def test_media_type_is_sniffed_from_magic_bytes_not_hardcoded(session):
    """A pasted JPEG sent to the API mislabelled image/png 400s."""
    jpeg_bytes = b"\xff\xd8\xff\xe0" + b"fakejpegbytes"
    jpeg_b64 = base64.standard_b64encode(jpeg_bytes).decode()
    llm = _CountingLLM(result=VisionExtraction(kind="text", content="ok", summary="s"))
    extractor = _extractor(session, llm=llm)
    extractor.extract(_image(content=jpeg_b64, name="photo.jpg"))

    assert llm.calls[0]["media_type"] == "image/jpeg"


# -- fix round 2 -------------------------------------------------------------


def test_a_whitespace_only_payload_is_refused_without_a_model_call(session):
    """Stripping whitespace can collapse a payload to "". b64decode("") happily
    returns b"" rather than raising, which would otherwise spend a REAL model
    call on zero image bytes and store it under the shared sha256(b"") digest."""
    llm = _CountingLLM()
    row = _extractor(session, llm=llm).extract(_image(content="   \n  "))

    assert row.content == ""
    assert llm.calls == []


def test_a_bare_data_uri_prefix_with_nothing_after_it_is_refused_without_a_model_call(session):
    """`data:image/png;base64,` with nothing after the comma strips to ""."""
    llm = _CountingLLM()
    row = _extractor(session, llm=llm).extract(_image(content="data:image/png;base64,"))

    assert row.content == ""
    assert llm.calls == []


def test_a_successful_extraction_survives_a_different_backend_on_the_second_drive(session):
    """The project's definition-of-done: a successful extraction is written
    once and reused — a second drive, even under a DIFFERENTLY-named client,
    must make no model call. Only a stored FAILURE (empty content) is eligible
    for retry under a different backend; a real result is not re-litigated."""
    first_llm = _CountingLLM(result=VisionExtraction(kind="table", content="a,b\n1,2", summary="t"))
    first = _extractor(session, llm=first_llm).extract(_image())
    assert first.content == "a,b\n1,2"

    second_llm = _CountingLLM(result=VisionExtraction(kind="table", content="x,y\n9,9", summary="different"))
    second_llm.name = "a-completely-different-client"
    second = _extractor(session, llm=second_llm).extract(_image())

    assert second_llm.calls == [], "a successful extraction must not be re-litigated by a new backend"


# -- backlog item 19: an unfetchable image gets its own key space -----------


def test_an_unfetchable_url_dead_letters_once_across_two_drives(session):
    """The gap: with no image bytes there is nothing to key the image-bytes
    cache on, so a retried fetch of the same bad URL used to dead-letter
    again on every drive. A second key space, url_sha256, closes it: the
    same unfetchable URL across two drives produces ONE dead letter, not
    two, and .extract() still returns a row (never None, never raises) both
    times."""
    dead = []
    fetcher = _StubFetcher(error=FetchRefused("image host 'evil.example.com' is not allowlisted"))
    url = "https://evil.example.com/a.png"

    first = _extractor(
        session, fetcher=fetcher, dead_letter=lambda **kw: dead.append(kw)
    ).extract(_image(content=url))
    second = _extractor(
        session, fetcher=fetcher, dead_letter=lambda **kw: dead.append(kw)
    ).extract(_image(content=url))

    assert first is not None and second is not None
    assert first.content == ""
    assert second.content == ""
    assert len(dead) == 1, "a second drive of the identical unfetchable URL must not dead-letter again"


def test_an_unfetchable_url_is_a_different_key_from_a_different_unfetchable_url(session):
    """Two different bad URLs must not collide into one dead letter."""
    dead = []
    fetcher = _StubFetcher(error=FetchRefused("not allowlisted"))

    _extractor(session, fetcher=fetcher, dead_letter=lambda **kw: dead.append(kw)).extract(
        _image(content="https://evil.example.com/a.png")
    )
    _extractor(session, fetcher=fetcher, dead_letter=lambda **kw: dead.append(kw)).extract(
        _image(content="https://evil.example.com/b.png")
    )

    assert len(dead) == 2


def test_an_unfetchable_url_recovers_once_the_url_becomes_fetchable(session):
    """The negative cache does not freeze a URL forever: the fetch is
    attempted again on every drive (nothing here short-circuits _bytes_for),
    so a transient outage clearing, or an expired link being refreshed, is
    picked up for free on the very next drive."""
    dead = []
    failing_fetcher = _StubFetcher(error=FetchRefused("temporarily unreachable"))
    url = "https://files.slack.com/f/a.png"

    first = _extractor(
        session, fetcher=failing_fetcher, dead_letter=lambda **kw: dead.append(kw)
    ).extract(_image(content=url))
    assert first.content == ""

    llm = _CountingLLM(result=VisionExtraction(kind="text", content="hello", summary="s"))
    working_fetcher = _StubFetcher(result=(PNG, "image/png"))
    second = _extractor(session, llm=llm, fetcher=working_fetcher).extract(_image(content=url))

    assert second.content == "hello", "a URL that becomes fetchable again must not stay frozen"
    assert len(dead) == 1


def test_a_new_failure_after_a_successful_fetch_dead_letters_again(session):
    """The negative cache must not be permanent.

    `_record_unfetchable` promises to suppress the duplicate dead letter for
    an identical, STILL-unfetchable source. The row is keyed on the source
    string; a successful extraction in between is stored under the image
    BYTES' hash, a different key, so nothing used to retire it. A genuinely
    new failure of the same URL then found the stale row and was silently
    never dead-lettered — in the one table whose whole purpose is that a
    failure is never silent.

    Fail (host unreachable) -> succeed -> fail again for a DIFFERENT reason.
    Two incidents, two dead letters. Under the old behaviour the third drive
    contributes nothing and this asserts 1 == 2.
    """
    dead = []
    url = "https://files.slack.com/f/a.png"

    _extractor(
        session,
        fetcher=_StubFetcher(error=FetchRefused("temporarily unreachable")),
        dead_letter=lambda **kw: dead.append(kw),
    ).extract(_image(content=url))
    assert len(dead) == 1

    llm = _CountingLLM(result=VisionExtraction(kind="text", content="hello", summary="s"))
    working = _extractor(session, llm=llm, fetcher=_StubFetcher(result=(PNG, "image/png"))).extract(
        _image(content=url)
    )
    assert working.content == "hello"

    _extractor(
        session,
        fetcher=_StubFetcher(error=FetchRefused("image is larger than the 5 MiB cap")),
        dead_letter=lambda **kw: dead.append(kw),
    ).extract(_image(content=url))

    assert len(dead) == 2, "a new failure of a source that worked in between is a NEW incident"


def test_a_successful_fetch_does_not_disturb_a_different_sources_negative_row(session):
    """The clear is scoped to the source that actually fetched. Retiring the
    wrong row would reopen item 19 for every other bad URL in the system."""
    dead = []
    bad = "https://evil.example.com/a.png"
    good = "https://files.slack.com/f/b.png"

    _extractor(
        session,
        fetcher=_StubFetcher(error=FetchRefused("not allowlisted")),
        dead_letter=lambda **kw: dead.append(kw),
    ).extract(_image(content=bad))
    assert len(dead) == 1

    llm = _CountingLLM(result=VisionExtraction(kind="text", content="hello", summary="s"))
    _extractor(session, llm=llm, fetcher=_StubFetcher(result=(PNG, "image/png"))).extract(
        _image(content=good)
    )

    _extractor(
        session,
        fetcher=_StubFetcher(error=FetchRefused("not allowlisted")),
        dead_letter=lambda **kw: dead.append(kw),
    ).extract(_image(content=bad))

    assert len(dead) == 1, "the bad URL's suppression must survive an unrelated success"
