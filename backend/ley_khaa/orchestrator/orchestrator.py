from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..crystallizer.candidate import CandidateState
from ..crystallizer.engine import Crystallizer
from ..crystallizer.gate import ReadinessGate
from ..crystallizer.relevance import RelevanceFilter
from ..domain.states import TaskState
from ..intake.gateway import IntakeGateway
from ..llm.client import LLMClient
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import CandidateRow, MessageRow
from ..persistence.repository import TaskRepository
from .driver import TaskDriver


@dataclass
class IntakeResult:
    message_id: str
    conversation_id: str
    candidates: list[CandidateRow] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    # Set when this message answered an existing task instead of forming a candidate.
    replied_to_task_id: str | None = None


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
        self.driver = TaskDriver(repo, llm=llm, messages=messages, candidates=candidates)

    def ingest(self, raw: dict, *, promote: bool = True) -> IntakeResult:
        row = self.gateway.accept(raw)
        if row.reply_to_task_id:
            return self._route_reply(row)
        verdict = self.relevance.judge(row)
        self.messages.record_verdict(
            row.id,
            relevant=verdict.relevant,
            topic=verdict.topic,
            confidence=verdict.confidence,
        )
        candidates = self.crystallizer.observe(row.conversation_id, verdict)

        result = IntakeResult(
            message_id=row.id,
            conversation_id=row.conversation_id,
            candidates=candidates,
        )

        if not promote:
            # A caller that is about to ingest a whole conversation (e.g. the
            # simulator) wants the gate/crystallizer to see every message before
            # any candidate is judged "settled." Evaluating the gate here, one
            # message at a time, made each arriving message look like the newest
            # thing said long ago (once backdated) — defeating the very debounce
            # the gate exists to enforce, and splitting one request across two
            # promoted (terminal) candidates. Skip promotion; the caller decides
            # when to call sweep() once the whole conversation is in.
            return result

        # The message that triggered this call is always the newest one in the
        # conversation, so this is only ever ~0 seconds quiet. That's fine: it
        # lets debounce_seconds=0 promote inline. A real (non-zero) debounce is
        # only ever satisfied later, by sweep().
        last_at = self.messages.last_timestamp(row.conversation_id)
        now = datetime.now(timezone.utc)
        for candidate in candidates:
            if self.gate.should_emit(candidate, last_message_at=last_at, now=now):
                task_id = self._promote(candidate)
                if task_id is not None:
                    result.task_ids.append(task_id)
        return result

    def _route_reply(self, row: MessageRow) -> IntakeResult:
        """Attach a reply to the task it answers; never form a candidate from it.

        The task's candidate is PROMOTED, which is terminal. Letting this message
        reach stage B would leave it uncovered in the window and invite a SECOND
        candidate — and so a duplicate task — for a request that already has one.

        This is deliberately the route a Slack thread reply takes in Phase 0.5.0:
        the adapter maps thread_ts to the task owning the thread, sets
        reply_to_task_id, and this branch fires unchanged.
        """
        task = self.repo.get(row.reply_to_task_id)
        if task is None:
            raise KeyError(row.reply_to_task_id)

        # Scoped honesty: `relevant` gates whether stage B may consider owning a
        # message for a NEW candidate. This one belongs to a task that already
        # exists, so it is genuinely not material to candidate formation.
        self.messages.record_verdict(
            row.id, relevant=False, topic="task-reply", confidence=1.0
        )
        self.repo.append_source_messages(task.id, [row.id])

        result = IntakeResult(
            message_id=row.id,
            conversation_id=row.conversation_id,
            replied_to_task_id=task.id,
        )
        if TaskState(task.state) is TaskState.NEEDS_CLARIFICATION:
            self.repo.increment_clarification_rounds(task.id)
            self.repo.set_open_question(task.id, None)
            self.repo.claim(
                task.id,
                expected=TaskState.NEEDS_CLARIFICATION,
                target=TaskState.CLASSIFIED,
            )
            self.driver.advance(task.id)
            result.task_ids.append(task.id)
        # A reply to a task that is not currently asking anything is still worth
        # keeping — it is context for the next interpretation — but it does not
        # restart a task the human is already reviewing.
        return result

    def sweep(self, conversation_id: str | None = None) -> list[str]:
        """Re-evaluate READY candidates against the gate with no new message.

        The debounce gate wants "the conversation has gone quiet," which is
        never true inside ingest() itself since ingest() is called BY a new
        message. sweep() is the trigger a poller/scheduler calls later, once
        that message is no longer the newest thing in the conversation, to
        let a candidate's quiet period actually elapse and promote it.
        """
        ready = self.candidates.list_by_state(CandidateState.READY)
        if conversation_id is not None:
            ready = [c for c in ready if c.conversation_id == conversation_id]

        now = datetime.now(timezone.utc)
        task_ids: list[str] = []
        for candidate in ready:
            last_at = self.messages.last_timestamp(candidate.conversation_id)
            if self.gate.should_emit(candidate, last_message_at=last_at, now=now):
                task_id = self._promote(candidate)
                if task_id is not None:
                    task_ids.append(task_id)
        return task_ids

    def _promote(self, candidate: CandidateRow) -> str | None:
        """Promote a ready candidate to a Task, or None if another caller won it.

        The claim comes first and is conditional on the candidate still being
        READY. Creating the task first made concurrent sweeps double-create it and
        left the loser raising InvalidCandidateTransition out of a 500.
        """
        if not self.candidates.claim_for_promotion(candidate.id):
            return None
        task = self.repo.create(
            project="default",
            title=candidate.title,
            source_message_ids=list(candidate.message_ids),
            candidate_id=candidate.id,
        )
        self.candidates.attach_task(candidate.id, task.id)
        # The driver owns everything from here: interpret, score, and either park
        # for a human or (on Auto) run through. The orchestrator's job ends at
        # turning a settled candidate into a task.
        self.driver.advance(task.id)
        return task.id

    def advance_stalled(self) -> list[str]:
        """Re-drive every task that is mid-flight but not waiting on a human.

        This is what retries a task whose interpretation hit a transport failure:
        it stays in CLASSIFIED, and the next sweep picks it up.
        """
        mid_flight = (
            TaskState.RECEIVED,
            TaskState.CLASSIFIED,
            TaskState.INTERPRETED,
            TaskState.EXECUTING,
            TaskState.VALIDATING,
        )
        # Collect first, then drive. Advancing inline while iterating state by
        # state let one sweep find the same task twice — once in RECEIVED, then
        # again in CLASSIFIED after it had just moved there — which burned two
        # retry attempts per sweep instead of one.
        task_ids: list[str] = []
        seen: set[str] = set()
        for state in mid_flight:
            for row in self.repo.list_by_state(state):
                if row.id not in seen:
                    seen.add(row.id)
                    task_ids.append(row.id)
        for task_id in task_ids:
            self.driver.advance(task_id)
        return task_ids
