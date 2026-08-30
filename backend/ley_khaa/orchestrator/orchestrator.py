import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..autonomy.engine import recommend_fold
from ..autonomy.modes import AutonomyMode
from ..crystallizer.candidate import CandidateState
from ..crystallizer.engine import Crystallizer
from ..crystallizer.gate import ReadinessGate
from ..crystallizer.relevance import RelevanceFilter
from ..domain.states import TaskState
from ..intake.gateway import IntakeGateway
from ..llm.client import LLMClient
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.memory_repository import MemoryRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import CandidateRow, MessageRow, TaskRow
from ..persistence.project_repository import DEFAULT_PROJECT, ProjectRepository
from ..persistence.repository import TaskRepository
from ..persistence.workflow_repository import WorkflowRepository
from ..projects.router import ProjectRouter
from .amendment import AmendmentDetector, AmendmentProposal
from .driver import TaskDriver

logger = logging.getLogger(__name__)


class ForeignReplyTarget(Exception):
    """`reply_to_task_id` names a task that belongs to a different conversation.

    A reply must only ever extend the task it names with a message from that
    task's OWN conversation. Accepting a cross-conversation id would let a
    foreign message join task.source_message_ids and, from there, the spec the
    interpreter reads on its next pass — the exact "a candidate owns only its
    own message ids" guarantee Phase 1 established, one field away from being
    violated.
    """


@dataclass
class IntakeResult:
    message_id: str
    conversation_id: str
    candidates: list[CandidateRow] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    # Set when this message answered an existing task instead of forming a candidate.
    replied_to_task_id: str | None = None
    # Which project the routing decision (in _promote) landed the work in.
    # None when no candidate reached promotion this call (still debouncing, or
    # this was a reply, which never routes).
    project: str | None = None


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
        workflows: WorkflowRepository | None = None,
        memories: MemoryRepository | None = None,
        projects: ProjectRepository | None = None,
    ) -> None:
        self.repo = repo
        self.messages = messages
        self.candidates = candidates
        self.gateway = IntakeGateway(messages)
        self.relevance = RelevanceFilter(llm)
        self.crystallizer = Crystallizer(llm, messages, candidates)
        self.gate = gate or ReadinessGate()
        self.driver = TaskDriver(
            repo, llm=llm, messages=messages, candidates=candidates,
            workflows=workflows, memories=memories,
        )
        self.projects = projects
        self.router = ProjectRouter(projects, llm) if projects is not None else None
        self.amendments = AmendmentDetector(repo, llm)

    def ingest(self, raw: dict, *, promote: bool = True) -> IntakeResult:
        row = self.gateway.accept(raw)
        if row.reply_to_task_id:
            return self._route_reply(row, promote=promote)
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
                task_id = self._promote(candidate, result=result)
                if task_id is not None:
                    result.task_ids.append(task_id)
        return result

    def _route_reply(self, row: MessageRow, *, promote: bool = True) -> IntakeResult:
        """Attach a reply to the task it answers; never form a candidate from it.

        The task's candidate is PROMOTED, which is terminal. Letting this message
        reach stage B would leave it uncovered in the window and invite a SECOND
        candidate — and so a duplicate task — for a request that already has one.

        This is deliberately the route a Slack thread reply takes in Phase 0.5.0:
        the adapter maps thread_ts to the task owning the thread, sets
        reply_to_task_id, and this branch fires unchanged.

        `promote` is honored the same way ingest() documents it elsewhere: with
        it False, the task is still linked to the reply but is not driven any
        further here. Unreachable today (no caller replies while replaying with
        promote=False), but the contract stated on ingest() must hold if one ever
        does.
        """
        task = self.repo.get(row.reply_to_task_id)
        if task is None:
            # gateway.accept() already committed this message before we get here.
            # Leaving its `relevant` column NULL would make the crystallizer
            # window (which treats NULL as "not yet judged" and keeps it) hang
            # onto an orphaned reply forever. Mark it noise before raising so no
            # conversation's window is polluted by a message naming a task that
            # never existed.
            self.messages.record_verdict(
                row.id, relevant=False, topic="task-reply", confidence=1.0
            )
            raise KeyError(row.reply_to_task_id)

        task_conversation_id = self._task_conversation_id(task)
        if task_conversation_id is not None and task_conversation_id != row.conversation_id:
            # Same reasoning as the unknown-task case above: don't leave this
            # message looking relevant-by-default, and never let it touch a task
            # it does not belong to.
            self.messages.record_verdict(
                row.id, relevant=False, topic="task-reply", confidence=1.0
            )
            raise ForeignReplyTarget(
                f"task {task.id} belongs to conversation {task_conversation_id!r}, "
                f"not {row.conversation_id!r}"
            )

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
        if not promote:
            return result
        if TaskState(task.state) is TaskState.NEEDS_CLARIFICATION:
            self.repo.increment_clarification_rounds(task.id)
            self.repo.set_open_question(task.id, None)
            self.repo.claim(
                task.id,
                expected=TaskState.NEEDS_CLARIFICATION,
                target=TaskState.CLASSIFIED,
            )
            self.driver.hand_off(task.id)
            result.task_ids.append(task.id)
        # A reply to a task that is not currently asking anything is still worth
        # keeping — it is context for the next interpretation — but it does not
        # restart a task the human is already reviewing.
        return result

    def _task_conversation_id(self, task) -> str | None:
        """The conversation a task's own source messages live in.

        TaskRow carries no conversation_id of its own; it is derived from the
        messages that formed it, the same lookup app.py's answer_task endpoint
        already does. None means the task has no source messages to check
        against (should not happen for a task that reached a human), in which
        case the mismatch check is skipped rather than blocking a reply.
        """
        sources = self.messages.get_many(list(task.source_message_ids or []))
        return sources[0].conversation_id if sources else None

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

    def _promote(
        self, candidate: CandidateRow, *, result: IntakeResult | None = None
    ) -> str | None:
        """Turn a settled candidate into work: a new task, a fold into a running
        one, or a decision parked for a human.

        Returns the task id the candidate's request now lives in — which for a
        fold is the EXISTING task — or None when it created no work (a lost
        claim, or a proposal parked for triage).

        Routing and detection run BEFORE any claim, which is forced rather than
        careless: the two outcomes take different claims (claim_for_promotion vs
        claim_for_triage), so which one to attempt is not known until the
        proposal exists. The cost is that a caller who then loses the race has
        paid for a routing call; the claims still guarantee exactly one of them
        creates work.

        `result`, when given, is stamped with the routed project — the caller's
        IntakeResult, filled in here rather than by re-routing (which could,
        for the model-backed router, answer differently on a second call and
        report a project the task was never actually created in).
        """
        project = self._route(candidate)
        if result is not None:
            result.project = project
        proposal = self.amendments.detect(
            project=project, title=candidate.title, summary=candidate.summary
        )
        if proposal is not None:
            return self._handle_amendment(candidate, proposal)

        if not self.candidates.claim_for_promotion(candidate.id):
            return None
        task = self.repo.create(
            project=project,
            title=candidate.title,
            source_message_ids=list(candidate.message_ids),
            candidate_id=candidate.id,
        )
        self.candidates.attach_task(candidate.id, task.id)
        self.driver.hand_off(task.id)
        return task.id

    def _handle_amendment(
        self, candidate: CandidateRow, proposal: AmendmentProposal
    ) -> str | None:
        target = self.repo.get(proposal.task_id)
        if target is None:
            return None
        spec = target.spec or {}
        decision = recommend_fold(
            mode=AutonomyMode(target.effective_mode) if target.effective_mode else None,
            detector_confidence=proposal.confidence,
            target_state=TaskState(target.state),
            target_missing_fields=list(spec.get("missing_fields") or []),
        )
        if not decision.fold:
            # Park it. The candidate holds the decision, so no placeholder task
            # is created for work nobody has agreed to do yet.
            if not self.candidates.claim_for_triage(
                candidate.id,
                task_id=target.id,
                reason=f"{proposal.reason} — {decision.reason}",
                confidence=proposal.confidence,
            ):
                return None
            logger.info("candidate %s parked as an amendment to %s", candidate.id, target.id)
            return None

        return self._fold(candidate, target, claim=self.candidates.claim_for_promotion)

    def _fold(self, candidate: CandidateRow, target: TaskRow, *, claim) -> str | None:
        """Merge a candidate into a task. Shared by the automatic and human paths.

        The candidate is claimed FIRST: a fold that loses the candidate race must
        not have already changed the target.
        """
        if not claim(candidate.id):
            return None
        if not self.repo.fold_into(
            target.id,
            message_ids=list(candidate.message_ids),
            expected=TaskState(target.state),
        ):
            # The target moved on. Put the candidate back where a human can see
            # it rather than dropping the request.
            self.candidates.return_to_triage(candidate.id)
            return None
        self.candidates.attach_task(candidate.id, target.id)
        self.driver.hand_off(target.id)
        return target.id

    # --- human triage actions ----------------------------------------------

    def fold(self, candidate_id: str) -> str | None:
        """Fold a parked candidate into the task it amends.

        None covers two cases, both a 409 at the route rather than a 404:
        the candidate has no amends_task_id (it was never parked on an
        amendment decision), or its target row is gone (the task it amends
        has moved on — e.g. deleted — since it was parked). Both are "this
        candidate is not answerable right now", not "the candidate is
        unknown" — that KeyError is reserved for a genuinely unknown
        candidate id, below. This also matches separate(), which is already
        correct: it claims first, and claim_for_fold's AWAITING_TRIAGE WHERE
        clause returns False -> None -> 409 for the same "not parked" case.
        """
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        if not candidate.amends_task_id:
            return None
        target = self.repo.get(candidate.amends_task_id)
        if target is None:
            return None
        return self._fold(candidate, target, claim=self.candidates.claim_for_fold)

    def separate(self, candidate_id: str) -> str | None:
        """Promote a parked candidate as its own task after all."""
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        if not self.candidates.claim_for_fold(candidate.id):
            return None
        task = self.repo.create(
            project=self._route(candidate),
            title=candidate.title,
            source_message_ids=list(candidate.message_ids),
            candidate_id=candidate.id,
        )
        self.candidates.attach_task(candidate.id, task.id)
        self.driver.hand_off(task.id)
        return task.id

    def _route(self, candidate: CandidateRow) -> str:
        """Which project this candidate's work belongs to (spec §5.4).

        source and client live on the MESSAGES, not on the candidate — the
        candidate only knows its conversation. A candidate with no messages
        cannot be routed and goes to the default project rather than taking
        intake down with an IndexError.
        """
        if self.router is None:
            return DEFAULT_PROJECT
        messages = self.messages.get_many(list(candidate.message_ids or []))
        if not messages:
            return DEFAULT_PROJECT
        first = messages[0]
        decision = self.router.route(
            source=first.source,
            client=first.client,
            conversation_id=candidate.conversation_id,
            title=candidate.title,
            summary=candidate.summary,
        )
        logger.info(
            "task from candidate %s routed to %s by %s (%s)",
            candidate.id, decision.project, decision.stage, decision.reason,
        )
        return decision.project

    def advance_stalled(self) -> list[str]:
        """Re-drive every task that is mid-flight but not waiting on a human.

        This is what retries a task whose interpretation hit a transport failure:
        it stays in CLASSIFIED, and the next sweep picks it up.

        EXECUTING is deliberately NOT in this set. Every other step here is cheap
        and idempotent — the state claim at the end of each one makes a duplicate
        pass a no-op. `_execute` is neither: nothing claims the task on the way
        IN, so a task still synthesizing when the sweeper comes round (every 15s,
        against a lane that can take two Opus calls and two sandbox runs) would
        get a second full lane on the same workspace, both writing the same
        attempt files and the same deliverable/. The persisted verdict would then
        belong to one run and the bundle on disk to the other, and every sweep
        would add another pair of model calls with no attempt ceiling to stop it.
        A task whose backend died mid-execution therefore stays in EXECUTING and
        stays visible, rather than being silently re-run at cost; recovering it
        automatically needs a lease on the row, not a timer.
        """
        mid_flight = (
            TaskState.RECEIVED,
            TaskState.CLASSIFIED,
            TaskState.INTERPRETED,
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
