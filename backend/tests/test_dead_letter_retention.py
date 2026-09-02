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
