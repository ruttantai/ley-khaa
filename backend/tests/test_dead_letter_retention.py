"""Retention on `dead_letters` (spec §3.4, backlog item 18).

`MAX_PAYLOAD_CHARS` bounds a row's SIZE; nothing bounded the row COUNT before
this. A permanently bad token writes one `connection` row per minute at the
supervisor's 60s backoff cap, forever — this is what stops that from being
unbounded growth.

Count-based, not time-based: a quiet system with a handful of old dead
letters must keep all of them. And when the cap IS hit, the NEWEST rows must
survive — a reader investigating an incident needs what just happened, not
what happened first.
"""
import pytest

from ley_khaa.config import settings
from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository


@pytest.fixture
def dead_letter_cap(monkeypatch):
    """Set the cap for one test and restore it afterwards, the same
    object.__setattr__-on-the-shared-instance pattern test_api.py uses for
    other frozen Settings fields."""

    def _set(cap: int) -> None:
        object.__setattr__(settings, "dead_letter_max_rows", cap)

    original = settings.dead_letter_max_rows
    yield _set
    object.__setattr__(settings, "dead_letter_max_rows", original)


def test_writing_past_the_cap_leaves_exactly_the_caps_worth_of_rows(session, dead_letter_cap):
    dead_letter_cap(5)
    repo = DeadLetterRepository(session)

    for i in range(12):
        repo.record(source="slack", kind="connection", reason=f"drop {i}")

    assert len(repo.list(limit=100)) == 5


def test_the_survivors_after_pruning_are_the_newest_not_the_oldest(session, dead_letter_cap):
    dead_letter_cap(3)
    repo = DeadLetterRepository(session)

    for i in range(7):
        repo.record(source="slack", kind="connection", reason=f"drop {i}")

    survivors = {row.reason for row in repo.list(limit=100)}
    # Rows 4, 5, 6 are the three newest writes; 0-3 must be gone.
    assert survivors == {"drop 4", "drop 5", "drop 6"}


def test_a_cap_of_zero_still_retains_the_newest_dead_letter(session, dead_letter_cap):
    """A cap of 0 is a natural thing for an operator to try when they mean
    "disable retention" — but 0 must NOT mean unbounded (that resurrects the
    bug this task closes) and must NOT mean "keep nothing" either: record()
    is called from the notifier's own exception handlers, so pruning away
    the row a call just wrote would crash that call's own session.refresh(),
    turning a handled notification failure into an unhandled one."""
    dead_letter_cap(0)
    repo = DeadLetterRepository(session)

    repo.record(source="slack", kind="connection", reason="first")
    newest = repo.record(source="slack", kind="connection", reason="second")

    rows = repo.list(limit=100)
    assert len(rows) >= 1
    assert rows[0].id == newest.id
    assert rows[0].reason == "second"


def test_a_negative_cap_also_clamps_to_at_least_one(session, dead_letter_cap):
    dead_letter_cap(-5)
    repo = DeadLetterRepository(session)

    repo.record(source="slack", kind="connection", reason="first")
    newest = repo.record(source="slack", kind="connection", reason="second")

    rows = repo.list(limit=100)
    assert len(rows) >= 1
    assert rows[0].id == newest.id
    assert rows[0].reason == "second"


def test_a_non_numeric_cap_falls_back_to_the_default_instead_of_killing_the_import(
    monkeypatch, caplog
):
    """The typo version of the same operator mistake the clamp above handles.

    `LEY_KHAA_DEAD_LETTER_MAX_ROWS=lots` used to raise `ValueError` inside
    `import ley_khaa.config` — before any logging is configured, so the
    service simply did not start. That is the opposite posture to the clamp
    two lines away, which went to real trouble to make a MISCONFIGURED
    retention cap non-fatal. A retention cap must not be able to stop the
    service, whichever way it is wrong.

    `_tolerant_int` is exercised directly rather than through
    `importlib.reload(config)`: reloading that module rebinds
    `ley_khaa.config.settings` to a new object while every
    `from ..config import settings` importer keeps the old one. That leak was
    this suite's one order-dependence (backlog item 25) until v1.0.0 removed
    the last two reload sites; nothing in `tests/` reloads `config` any more,
    and nothing should start. Not worth adding one back to test a pure
    function.
    """
    from ley_khaa.config import _tolerant_int

    monkeypatch.setenv("LEY_KHAA_DEAD_LETTER_MAX_ROWS", "lots")
    with caplog.at_level("WARNING", logger="ley_khaa.config"):
        assert _tolerant_int("LEY_KHAA_DEAD_LETTER_MAX_ROWS", 1000) == 1000
    assert "not an integer" in caplog.text, "a silently ignored typo is worse than a loud one"

    monkeypatch.setenv("LEY_KHAA_DEAD_LETTER_MAX_ROWS", "25")
    assert _tolerant_int("LEY_KHAA_DEAD_LETTER_MAX_ROWS", 1000) == 25

    monkeypatch.delenv("LEY_KHAA_DEAD_LETTER_MAX_ROWS")
    assert _tolerant_int("LEY_KHAA_DEAD_LETTER_MAX_ROWS", 1000) == 1000
