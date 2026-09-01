import base64

from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.persistence.image_extraction_repository import (
    ImageExtractionRepository,
    sha256_of,
)
from ley_khaa.vision.contract import VisionExtraction
from ley_khaa.vision.extractor import VisionExtractor
from ley_khaa.vision.fetcher import FetchRefused

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
