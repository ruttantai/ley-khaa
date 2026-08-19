from datetime import datetime, timedelta, timezone

from ley_khaa.crystallizer.engine import CandidateDraft, CrystallizerOutput
from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.crystallizer.relevance import RelevanceVerdict
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _orch(session, llm, debounce=0):
    return Orchestrator(
        TaskRepository(session),
        llm=llm,
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(debounce_seconds=debounce),
    )


def test_noise_message_creates_no_task(session):
    result = _orch(session, HeuristicLLM()).ingest({"text": "morning all"})
    assert result.task_ids == []
    assert TaskRepository(session).list() == []


def test_request_message_creates_one_task(session):
    result = _orch(session, HeuristicLLM()).ingest({"text": "compare the universes and send the difference"})
    assert len(result.task_ids) == 1
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.state == TaskState.DONE.value


def test_task_owns_only_the_candidates_messages(session):
    orch = _orch(session, HeuristicLLM())
    orch.ingest({"text": "morning all"})
    result = orch.ingest({"text": "please compare the universes"})
    task = TaskRepository(session).get(result.task_ids[0])
    assert len(task.source_message_ids) == 1


def test_candidate_is_marked_promoted(session):
    result = _orch(session, HeuristicLLM()).ingest({"text": "compare the universes"})
    candidate = CandidateRepository(session).list_for_conversation("conv-1")[0]
    assert candidate.state == "promoted"
    assert candidate.task_id == result.task_ids[0]


def test_debounce_holds_a_ready_candidate_back(session):
    result = _orch(session, HeuristicLLM(), debounce=600).ingest({"text": "compare the universes"})
    assert result.task_ids == []
    assert CandidateRepository(session).list_for_conversation("conv-1")[0].state == "ready"


def test_unready_candidate_creates_no_task(session):
    llm = FakeLLM(
        [
            RelevanceVerdict(relevant=True, topic="t", confidence=0.9),
            CrystallizerOutput(
                candidates=[
                    CandidateDraft(
                        candidate_key="k",
                        title="Partial",
                        summary="s",
                        message_ids=["m1"],
                        state="crystallizing",
                        missing_fields=["output_format"],
                        open_question="Excel or CSV?",
                    )
                ]
            ),
        ]
    )
    result = _orch(session, llm).ingest({"text": "send me the differences"})
    assert result.task_ids == []
    assert result.candidates[0].open_question == "Excel or CSV?"


def test_ingest_is_idempotent_per_external_id(session):
    orch = _orch(session, HeuristicLLM())
    first = orch.ingest({"text": "compare the universes", "external_id": "slack-7"})
    second = orch.ingest({"text": "compare the universes", "external_id": "slack-7"})
    assert first.message_id == second.message_id
    assert len(MessageRepository(session).list_for_conversation("conv-1")) == 1


def test_result_reports_conversation_id(session):
    result = _orch(session, HeuristicLLM()).ingest({"text": "compare universes", "conversation_id": "c9"})
    assert result.conversation_id == "c9"
