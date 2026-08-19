from datetime import datetime, timedelta, timezone

from ley_khaa.crystallizer.engine import CandidateDraft, Crystallizer, CrystallizerOutput
from ley_khaa.crystallizer.relevance import RelevanceVerdict
from ley_khaa.domain.models import Message
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository

RELEVANT = RelevanceVerdict(relevant=True, topic="universe", confidence=0.9)
NOISE = RelevanceVerdict(relevant=False, topic="chatter", confidence=0.9)


def _seed(session, texts, conv="c1"):
    repo = MessageRepository(session)
    base = datetime.now(timezone.utc)
    rows = []
    for i, t in enumerate(texts):
        rows.append(
            repo.add(
                Message(
                    source="simulator",
                    client="demo",
                    conversation_id=conv,
                    author="boss",
                    text=t,
                    timestamp=base + timedelta(seconds=i),
                )
            )
        )
    return rows


def _engine(session, llm):
    return Crystallizer(llm, MessageRepository(session), CandidateRepository(session))


def _draft(**kw):
    base = dict(
        candidate_key="cand-universe",
        title="Universe reconciliation",
        summary="Compare Bloomberg vs FactSet, send the difference",
        message_ids=["m1"],
        state="forming",
        missing_fields=[],
        open_question=None,
    )
    base.update(kw)
    return CandidateDraft(**base)


def test_noise_verdict_skips_the_llm_entirely(session):
    _seed(session, ["lol"])
    llm = FakeLLM([])  # exhausted: any call would raise
    result = _engine(session, llm).observe("c1", NOISE)
    assert result == []
    assert llm.calls == []


def test_relevant_message_creates_a_candidate(session):
    rows = _seed(session, ["compare the universes"])
    llm = FakeLLM([CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])])])
    result = _engine(session, llm).observe("c1", RELEVANT)
    assert len(result) == 1
    assert result[0].title == "Universe reconciliation"
    assert result[0].state == "forming"


def test_candidate_is_updated_not_duplicated_across_turns(session):
    rows = _seed(session, ["compare the universes", "as of month end"])
    llm = FakeLLM(
        [
            CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])]),
            CrystallizerOutput(
                candidates=[_draft(state="ready", message_ids=[rows[0].id, rows[1].id])]
            ),
        ]
    )
    engine = _engine(session, llm)
    engine.observe("c1", RELEVANT)
    result = engine.observe("c1", RELEVANT)
    assert len(result) == 1
    assert result[0].state == "ready"
    assert result[0].message_ids == [rows[0].id, rows[1].id]
    assert len(CandidateRepository(session).list_for_conversation("c1")) == 1


def test_two_interleaved_topics_become_two_candidates(session):
    rows = _seed(session, ["compare universes", "also rebuild the risk report"])
    llm = FakeLLM(
        [
            CrystallizerOutput(
                candidates=[
                    _draft(candidate_key="cand-universe", message_ids=[rows[0].id]),
                    _draft(
                        candidate_key="cand-risk",
                        title="Risk report rebuild",
                        message_ids=[rows[1].id],
                    ),
                ]
            )
        ]
    )
    result = _engine(session, llm).observe("c1", RELEVANT)
    assert {c.candidate_key for c in result} == {"cand-universe", "cand-risk"}


def test_noise_messages_are_never_owned_by_a_candidate(session):
    rows = _seed(session, ["compare universes", "haha nice", "by month end"])
    llm = FakeLLM(
        [CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id, rows[2].id])])]
    )
    result = _engine(session, llm).observe("c1", RELEVANT)
    assert rows[1].id not in result[0].message_ids


def test_missing_fields_and_question_are_persisted(session):
    rows = _seed(session, ["send me the differences"])
    llm = FakeLLM(
        [
            CrystallizerOutput(
                candidates=[
                    _draft(
                        state="crystallizing",
                        message_ids=[rows[0].id],
                        missing_fields=["output_format"],
                        open_question="Excel or CSV?",
                    )
                ]
            )
        ]
    )
    result = _engine(session, llm).observe("c1", RELEVANT)
    assert result[0].missing_fields == ["output_format"]
    assert result[0].open_question == "Excel or CSV?"


def test_prompt_carries_the_rolling_window_and_existing_candidates(session):
    rows = _seed(session, ["compare universes", "by month end"])
    llm = FakeLLM(
        [
            CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])]),
            CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id, rows[1].id])]),
        ]
    )
    engine = _engine(session, llm)
    engine.observe("c1", RELEVANT)
    engine.observe("c1", RELEVANT)
    second_prompt = llm.calls[1].user
    assert "by month end" in second_prompt          # window
    assert "cand-universe" in second_prompt          # existing candidate state


def test_window_is_capped(session):
    rows = _seed(session, [f"message {i}" for i in range(40)])
    llm = FakeLLM([CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])])])
    engine = Crystallizer(
        llm, MessageRepository(session), CandidateRepository(session), window_size=5
    )
    engine.observe("c1", RELEVANT)
    prompt = llm.calls[0].user
    assert "message 39" in prompt
    assert "message 0\n" not in prompt


def test_promoted_candidate_is_not_resurrected(session):
    rows = _seed(session, ["compare universes"])
    llm = FakeLLM(
        [
            CrystallizerOutput(candidates=[_draft(state="ready", message_ids=[rows[0].id])]),
            CrystallizerOutput(candidates=[_draft(state="ready", message_ids=[rows[0].id])]),
        ]
    )
    engine = _engine(session, llm)
    engine.observe("c1", RELEVANT)
    CandidateRepository(session).mark_promoted(
        CandidateRepository(session).list_for_conversation("c1")[0].id, task_id="t1"
    )
    # Second turn re-reports the same key: it must be ignored, not raise.
    assert engine.observe("c1", RELEVANT) == []


def test_ready_candidate_reopened_as_forming_does_not_crash(session):
    # The rolling window can age out the messages that justified "ready"; the
    # model may then legitimately re-report the same candidate as "forming".
    # That is a backwards, non-terminal move and must not raise.
    rows = _seed(session, ["compare universes"])
    llm = FakeLLM(
        [
            CrystallizerOutput(candidates=[_draft(state="ready", message_ids=[rows[0].id])]),
            CrystallizerOutput(candidates=[_draft(state="forming", message_ids=[rows[0].id])]),
        ]
    )
    engine = _engine(session, llm)
    engine.observe("c1", RELEVANT)
    result = engine.observe("c1", RELEVANT)
    assert len(result) == 1
    assert result[0].state == "forming"


def test_complex_conversation_routes_to_opus(session):
    rows = _seed(session, [f"message {i}" for i in range(15)])
    llm = FakeLLM([CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])])])
    _engine(session, llm).observe("c1", RELEVANT)
    # Long windows are the "hard" signal: escalate off Haiku.
    assert llm.calls[0].choice.model == "claude-opus-5"


def test_short_conversation_stays_on_haiku(session):
    rows = _seed(session, ["compare universes"])
    llm = FakeLLM([CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])])])
    _engine(session, llm).observe("c1", RELEVANT)
    assert llm.calls[0].choice.model == "claude-haiku-4-5"
