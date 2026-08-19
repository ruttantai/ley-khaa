from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..crystallizer.engine import Crystallizer
from ..crystallizer.gate import ReadinessGate
from ..crystallizer.relevance import RelevanceFilter
from ..domain.states import TaskState
from ..intake.gateway import IntakeGateway
from ..llm.client import LLMClient
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import CandidateRow
from ..persistence.repository import TaskRepository

# Execution is still a stub: the real executor arrives in phase 0.4.0.
STUB_PATH: list[TaskState] = [
    TaskState.CLASSIFIED,
    TaskState.INTERPRETED,
    TaskState.EXECUTING,
    TaskState.VALIDATING,
    TaskState.DONE,
]


@dataclass
class IntakeResult:
    message_id: str
    conversation_id: str
    candidates: list[CandidateRow] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)


class Orchestrator:
    """intake → stage A → stage B → readiness gate → task."""

    def __init__(
        self,
        repo: TaskRepository,
        *,
        llm: LLMClient,
        messages: MessageRepository,
        candidates: CandidateRepository,
        gate: ReadinessGate | None = None,
    ) -> None:
        self.repo = repo
        self.messages = messages
        self.candidates = candidates
        self.gateway = IntakeGateway(messages)
        self.relevance = RelevanceFilter(llm)
        self.crystallizer = Crystallizer(llm, messages, candidates)
        self.gate = gate or ReadinessGate()

    def ingest(self, raw: dict) -> IntakeResult:
        row = self.gateway.accept(raw)
        verdict = self.relevance.judge(row)
        candidates = self.crystallizer.observe(row.conversation_id, verdict)

        result = IntakeResult(
            message_id=row.id,
            conversation_id=row.conversation_id,
            candidates=candidates,
        )

        last_at = self.messages.last_timestamp(row.conversation_id) or row.timestamp
        now = datetime.now(timezone.utc)
        for candidate in candidates:
            if self.gate.should_emit(candidate, last_message_at=last_at, now=now):
                result.task_ids.append(self._promote(candidate))
        return result

    def _promote(self, candidate: CandidateRow) -> str:
        task = self.repo.create(
            project="default",
            title=candidate.title,
            source_message_ids=list(candidate.message_ids),
        )
        for state in STUB_PATH:
            self.repo.update_state(task.id, state)
        self.candidates.mark_promoted(candidate.id, task_id=task.id)
        return task.id
