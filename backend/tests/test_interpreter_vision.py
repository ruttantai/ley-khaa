from ley_khaa.interpreter.interpreter import _render
from ley_khaa.persistence.orm import ImageExtractionRow


class _Row:
    """The two attributes _render reads from a message."""

    def __init__(self, attachments):
        self.id = "m1"
        self.author = "ana"
        self.text = "compare these"
        self.attachments = attachments


class _Task:
    title = "compare the holdings"


class _Extractor:
    def __init__(self, summary="a table of holdings", content="a,b\n1,2"):
        self.row = ImageExtractionRow(
            image_sha256="d" * 64, kind="table", content=content,
            summary=summary, media_type="image/png", byte_size=10, model="anthropic",
        )
        self.seen = []

    def extract(self, attachment):
        self.seen.append(attachment)
        return self.row


IMAGE = {"kind": "image", "name": "chart.png", "content": "https://files.slack.com/f/a.png"}
TABLE = {"kind": "table", "name": "h.csv", "content": "a,b\n1,2"}


def test_without_an_extractor_the_rendering_is_unchanged():
    """Every pre-phase-7 caller must see exactly what it saw before."""
    out = _render(_Task(), [_Row([IMAGE])], extractor=None)
    assert "attachment: image named chart.png" in out
    assert "a table of holdings" not in out


def test_an_image_summary_reaches_the_prompt():
    """This IS 'understood via vision' — the interpreter can now reason about
    what the picture contained."""
    out = _render(_Task(), [_Row([IMAGE])], extractor=_Extractor())
    assert "a table of holdings" in out


def test_a_non_image_attachment_is_never_sent_to_the_extractor():
    extractor = _Extractor()
    _render(_Task(), [_Row([TABLE])], extractor=extractor)
    assert extractor.seen == []


def test_an_unread_image_still_renders_its_name():
    """Degradation must leave the interpreter able to say what it could not
    read, rather than silently dropping the attachment."""
    extractor = _Extractor(summary="chart.png was not read: no vision backend", content="")
    out = _render(_Task(), [_Row([IMAGE])], extractor=extractor)

    assert "chart.png" in out
    assert "was not read" in out


def test_the_full_extracted_content_is_NOT_pasted_into_the_prompt():
    """summary exists precisely so a 5000-row CSV does not blow the prompt."""
    big = "col\n" + "\n".join(str(i) for i in range(5000))
    out = _render(_Task(), [_Row([IMAGE])], extractor=_Extractor(content=big))
    assert big not in out
