from datetime import datetime, timedelta, timezone

import pytest

from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.persistence.orm import CandidateRow

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)


def _cand(state="ready", missing_fields=None, open_question=None):
    return CandidateRow(
        id="c",
        conversation_id="c1",
        candidate_key="k",
        title="t",
        summary="s",
        state=state,
        message_ids=["m1"],
        missing_fields=missing_fields or [],
        open_question=open_question,
    )


@pytest.mark.parametrize("state", ["forming", "crystallizing"])
def test_unready_candidates_never_emit(state):
    gate = ReadinessGate(debounce_seconds=45)
    assert gate.should_emit(_cand(state=state), last_message_at=NOW - timedelta(minutes=5), now=NOW) is False


def test_ready_candidate_emits_after_a_conversational_pause():
    gate = ReadinessGate(debounce_seconds=45)
    assert gate.should_emit(_cand(), last_message_at=NOW - timedelta(seconds=60), now=NOW) is True


def test_ready_candidate_waits_while_the_human_is_still_typing():
    gate = ReadinessGate(debounce_seconds=45)
    assert gate.should_emit(_cand(), last_message_at=NOW - timedelta(seconds=10), now=NOW) is False


def test_missing_fields_block_emission_even_after_a_pause():
    gate = ReadinessGate(debounce_seconds=45)
    row = _cand(missing_fields=["output_format"])
    assert gate.should_emit(row, last_message_at=NOW - timedelta(minutes=5), now=NOW) is False


def test_open_question_blocks_emission():
    gate = ReadinessGate(debounce_seconds=45)
    row = _cand(open_question="Excel or CSV?")
    assert gate.should_emit(row, last_message_at=NOW - timedelta(minutes=5), now=NOW) is False


def test_exactly_at_the_threshold_emits():
    gate = ReadinessGate(debounce_seconds=45)
    assert gate.should_emit(_cand(), last_message_at=NOW - timedelta(seconds=45), now=NOW) is True


def test_zero_debounce_emits_immediately():
    gate = ReadinessGate(debounce_seconds=0)
    assert gate.should_emit(_cand(), last_message_at=NOW, now=NOW) is True


def test_naive_last_message_timestamp_is_treated_as_utc():
    # SQLite hands back naive datetimes; the gate must not crash comparing them.
    gate = ReadinessGate(debounce_seconds=45)
    naive = (NOW - timedelta(minutes=5)).replace(tzinfo=None)
    assert gate.should_emit(_cand(), last_message_at=naive, now=NOW) is True


def test_a_conversation_with_no_messages_never_emits():
    """`MessageRepository.last_timestamp` answers None when a conversation has
    no messages, and both callers hand that straight to the gate. Before this
    guard as_utc() raised AttributeError inside a sweep, taking down the whole
    sweep rather than skipping one candidate — and a `datetime` in the signature
    said the case could not arise."""
    gate = ReadinessGate(debounce_seconds=45)
    assert gate.should_emit(_cand(), last_message_at=None, now=NOW) is False
