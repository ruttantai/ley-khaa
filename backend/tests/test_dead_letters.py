import json

from ley_khaa.persistence.dead_letter_repository import (
    MAX_PAYLOAD_CHARS,
    DeadLetterRepository,
    redact,
    scrub_text,
)


def test_a_dead_letter_is_recorded_and_listed(session):
    repo = DeadLetterRepository(session)
    repo.record(source="slack", kind="inbound", reason="no text", payload={"a": 1})

    rows = repo.list()
    assert len(rows) == 1
    assert rows[0].source == "slack"
    assert rows[0].kind == "inbound"
    assert rows[0].reason == "no text"
    assert json.loads(rows[0].payload) == {"a": 1}


def test_the_newest_dead_letter_comes_first(session):
    repo = DeadLetterRepository(session)
    repo.record(source="slack", kind="inbound", reason="first")
    repo.record(source="slack", kind="inbound", reason="second")

    assert [r.reason for r in repo.list()] == ["second", "first"]


def test_the_limit_is_honoured(session):
    repo = DeadLetterRepository(session)
    for i in range(5):
        repo.record(source="slack", kind="inbound", reason=f"r{i}")

    assert [r.reason for r in repo.list(limit=2)] == ["r4", "r3"]


def test_a_token_never_reaches_storage(session):
    """This is the whole point of the redactor, so it is asserted on the STORED
    text, not on redact()'s return value: a repository that forgot to call the
    redactor would still pass a test written against redact() alone."""
    repo = DeadLetterRepository(session)
    repo.record(
        source="slack",
        kind="inbound",
        reason="translation failed",
        payload={"token": "xoxb-super-secret", "event": {"text": "hello"}},
    )

    stored = repo.list()[0].payload
    assert "xoxb-super-secret" not in stored
    assert "[redacted]" in stored
    assert "hello" in stored, "redaction must not eat the diagnostic content"


def test_redaction_reaches_nested_values_and_lists():
    text = redact(
        {
            "outer": {"api_key": "k", "keep": "yes"},
            "items": [{"Authorization": "Bearer x"}, {"fine": 1}],
        }
    )
    assert "\"k\"" not in text
    assert "Bearer x" not in text
    assert "yes" in text
    assert "\"fine\": 1" in text


def test_redaction_is_case_insensitive_and_matches_substrings():
    text = redact({"SLACK_BOT_TOKEN": "t", "signingSecret": "s", "password": "p"})
    assert "\"t\"" not in text
    assert "\"s\"" not in text
    assert "\"p\"" not in text
    assert text.count("[redacted]") == 3


def test_an_oversized_payload_is_truncated(session):
    repo = DeadLetterRepository(session)
    repo.record(source="slack", kind="inbound", reason="huge", payload={"t": "x" * 50_000})

    stored = repo.list()[0].payload
    assert len(stored) <= MAX_PAYLOAD_CHARS
    assert stored.endswith("…[truncated]")


def test_a_non_serialisable_value_becomes_a_type_placeholder(session):
    """`_scrub` renders an arbitrary object as its bare type name rather than
    letting json.dumps's default=repr fallback touch it. This does not exercise
    the "never raises" guarantee (see the two circular/bad-key tests below for
    that) - object() is perfectly renderable once _scrub turns it into a
    placeholder string, so no exception is ever in play here."""
    repo = DeadLetterRepository(session)
    repo.record(source="slack", kind="inbound", reason="odd", payload={"o": object()})

    assert repo.list()[0].payload  # something readable, and no exception


def test_a_circular_payload_is_described_rather_than_raising(session):
    """The promise that makes dead letters safe: recording a failure must never
    itself raise. Reached via RecursionError - _scrub recurses through the
    self-reference before json ever sees it."""
    circular: dict = {}
    circular["self"] = circular

    repo = DeadLetterRepository(session)
    repo.record(source="slack", kind="inbound", reason="circular", payload=circular)

    assert repo.list()[0].payload  # something readable, and no exception


def test_a_payload_json_cannot_key_is_described_rather_than_raising():
    """The other way into the same guard: _scrub renders the CONTAINER fine, and
    json.dumps then refuses the key type."""
    assert redact({("tuple", "key"): "value"})


def test_no_payload_stores_an_empty_string(session):
    repo = DeadLetterRepository(session)
    repo.record(source="discord", kind="connection", reason="socket closed")

    assert repo.list()[0].payload == ""


def test_an_objects_repr_never_reaches_storage(session):
    """The leak this guards is not hypothetical: an adapter crash is recorded
    with the exception in hand, and a platform client's exception routinely
    quotes the authenticated request that produced it."""

    class Client:
        def __init__(self, token):
            self.token = token

        def __repr__(self):
            return f"Client(token={self.token!r})"

    repo = DeadLetterRepository(session)
    repo.record(
        source="slack",
        kind="connection",
        reason="crashed",
        payload={"client": Client("xoxb-repr-secret")},
    )

    stored = repo.list()[0].payload
    assert "xoxb-repr-secret" not in stored
    assert "<Client>" in stored


def test_an_exception_payload_does_not_leak_its_message(session):
    repo = DeadLetterRepository(session)
    repo.record(
        source="slack",
        kind="connection",
        reason="crashed",
        payload={"error": ValueError("auth failed for xoxb-in-the-message")},
    )

    assert "xoxb-in-the-message" not in repo.list()[0].payload


def test_a_signature_header_is_redacted():
    text = redact({"x_slack_signature": "v0=abcdef", "client_secret": "cs"})
    assert "v0=abcdef" not in text
    assert "cs" not in text
    assert text.count("[redacted]") == 2


def test_a_token_in_the_reason_is_scrubbed(session):
    """`reason` is the field an adapter crash writes an exception message into,
    and an SDK exception routinely quotes the request that produced it."""
    repo = DeadLetterRepository(session)
    repo.record(
        source="slack",
        kind="connection",
        reason="ConnectionError: auth failed for xoxb-1234-abcd",
    )
    stored = repo.list()[0].reason
    assert "xoxb-1234-abcd" not in stored
    assert "[redacted]" in stored
    assert "ConnectionError" in stored, "the diagnostic must survive"


def test_a_token_in_a_payload_string_value_is_scrubbed(session):
    """Key-based redaction only sees a secret-NAMED key; this is the same
    secret hiding in an innocuously-named one."""
    repo = DeadLetterRepository(session)
    repo.record(
        source="slack",
        kind="inbound",
        reason="bad envelope",
        payload={"message": "retry with xapp-9999-zzzz please"},
    )
    stored = repo.list()[0].payload
    assert "xapp-9999-zzzz" not in stored
    assert "retry with" in stored, "the diagnostic must survive"


def test_scrubbing_leaves_ordinary_text_alone():
    """A false positive silently destroys a diagnostic, so the patterns are
    prefix-anchored and length-gated rather than entropy-based."""
    assert scrub_text("no text in the event") == "no text in the event"
    assert scrub_text("channel C0ALLOWED1 is not listed") == "channel C0ALLOWED1 is not listed"
    # The word "bearer" in a realistic auth diagnostic carries no secret.
    assert scrub_text("missing bearer token in the request") == (
        "missing bearer token in the request"
    )
    assert scrub_text("the bearer of this message is unknown") == (
        "the bearer of this message is unknown"
    )
    # A path is not a token: the prefixes require a trailing hyphen.
    assert scrub_text("/var/data/xoxbox/notreally.txt") == "/var/data/xoxbox/notreally.txt"


def test_a_real_bearer_token_is_still_scrubbed():
    """The length gate must not cost the actual protection."""
    text = scrub_text("401 for Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc123def456")
    assert "eyJhbGci" not in text
    assert "[redacted]" in text
    assert "401 for" in text, "the diagnostic must survive"


def test_an_uppercase_slack_token_is_scrubbed_too():
    """Slack issues lowercase, but a caller that upper-cases a message must not
    thereby defeat the scrub."""
    assert "XOXB-UPPER-1234" not in scrub_text("leaked XOXB-UPPER-1234 here")
