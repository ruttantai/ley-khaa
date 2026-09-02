from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.llm.router import Stage, model_for
from ley_khaa.vision.contract import VisionExtraction

IMAGE = b"\x89PNG\r\n\x1a\nfake"
# model_for is a module-level FUNCTION — there is no ModelRouter class.
CHOICE = model_for(Stage.VISION_EXTRACTION)


def test_the_heuristic_client_returns_an_unread_record():
    """No vision backend must degrade, never raise: the zero-account demo has
    to complete (spec §3.6)."""
    result = HeuristicLLM().extract_image(
        choice=CHOICE, system="s", user="chart.png",
        image=IMAGE, media_type="image/png", output_format=VisionExtraction,
    )

    assert isinstance(result, VisionExtraction)
    assert result.content == "", "empty content IS the 'was not read' signal"
    assert "chart.png" in result.summary
    assert result.kind == "text"


def test_the_heuristic_client_is_deterministic():
    """Two calls on the same image agree, or the cache's byte-identity test
    would pass for the wrong reason."""
    llm = HeuristicLLM()
    kwargs = dict(
        choice=CHOICE, system="s", user="chart.png",
        image=IMAGE, media_type="image/png", output_format=VisionExtraction,
    )
    assert llm.extract_image(**kwargs) == llm.extract_image(**kwargs)


def test_the_heuristic_client_never_touches_the_image_bytes():
    """It has no vision. If it ever appears to read one, something has been
    wired to a real backend by accident."""
    llm = HeuristicLLM()
    a = llm.extract_image(
        choice=CHOICE, system="s", user="chart.png",
        image=b"totally different bytes", media_type="image/png",
        output_format=VisionExtraction,
    )
    b = llm.extract_image(
        choice=CHOICE, system="s", user="chart.png",
        image=IMAGE, media_type="image/png", output_format=VisionExtraction,
    )
    assert a == b


def test_the_fake_client_returns_its_queued_response():
    queued = VisionExtraction(kind="table", content="a,b\n1,2", summary="a table")
    llm = FakeLLM([queued])

    result = llm.extract_image(
        choice=CHOICE, system="s", user="u",
        image=IMAGE, media_type="image/png", output_format=VisionExtraction,
    )

    assert result is queued


def test_the_fake_client_records_the_call_like_parse_does():
    llm = FakeLLM([VisionExtraction(kind="text", content="x", summary="y")])
    llm.extract_image(
        choice=CHOICE, system="sys", user="usr",
        image=IMAGE, media_type="image/png", output_format=VisionExtraction,
    )

    assert llm.calls, "a vision call must be recorded, or a call-counter test cannot see it"
    assert llm.calls[-1].choice == CHOICE


def test_every_client_reports_a_name():
    """The manifest records who ACTUALLY produced an extraction."""
    assert HeuristicLLM().name == "heuristic"
    assert FakeLLM([]).name
