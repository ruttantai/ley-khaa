import logging
from collections.abc import Callable

from ..autonomy.engine import recommend
from ..autonomy.modes import AutonomyMode
from ..domain.states import InvalidTransition, TaskState
from ..interpreter.interpreter import Interpreter, MalformedSpec
from ..interpreter.spec import TaskSpec
from ..llm.client import LLMClient
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import TaskRow
from ..persistence.repository import TaskRepository

logger = logging.getLogger(__name__)

# Where a task comes to rest on its own: finished, or a human owes it something.
_WAITING = {
    TaskState.AWAITING_APPROVAL,
    TaskState.NEEDS_CLARIFICATION,
    TaskState.DONE,
    TaskState.FAILED,
}

# Each pass performs at most one transition, so this only has to exceed the
# longest automatic run (received → classified → interpreted → executing →
# validating → done). It exists so that a future bug cannot spin forever.
_MAX_STEPS = 10

# States in which a human can still act on a task: it hasn't started
# irreversible work yet, or it's paused asking a question. Approve/reject/
# override/edit_spec all refuse to touch a task outside this set — rewriting
# or re-scoring finished (or mid-flight) work is not a "correction," it's data
# loss.
_ACTIONABLE = {TaskState.AWAITING_APPROVAL, TaskState.NEEDS_CLARIFICATION}

# A transport failure is retried by the sweeper, not in a tight loop.
_MAX_INTERPRET_ATTEMPTS = 3
# After this many rounds, stop asking and let the human decide with the gaps
# visible. Without a cap, a model that keeps reporting the same gap and a human
# who keeps not answering it will ping-pong forever.
_MAX_CLARIFICATION_ROUNDS = 3


class TaskDriver:
    """The single place that knows how far a task can go without a human.

    advance() is re-entrant: each human action performs its own small transition
    and then calls it again, so "what happens after approval" and "what happens
    after a clarification answer" are the same code and cannot drift apart.
    """

    def __init__(
        self,
        repo: TaskRepository,
        *,
        llm: LLMClient,
        messages: MessageRepository,
        candidates: CandidateRepository,
    ) -> None:
        self.repo = repo
        self.candidates = candidates
        self.interpreter = Interpreter(llm, messages)

    def advance(self, task_id: str) -> TaskRow:
        """Push a task as far as it can go unattended, then return where it landed."""
        for _ in range(_MAX_STEPS):
            row = self.repo.get(task_id)
            if row is None:
                raise KeyError(task_id)
            state = TaskState(row.state)
            if state in _WAITING:
                return row
            if not _STEPS[state](self, row):
                # No progress: a lost claim (another caller won the race) or a
                # retryable failure. Either way, stop here.
                return self.repo.get(task_id)
        logger.warning("task %s hit the step ceiling; leaving it where it is", task_id)
        return self.repo.get(task_id)

    # --- human actions ----------------------------------------------------

    def approve(self, task_id: str) -> TaskRow:
        if not self.repo.claim(
            task_id, expected=TaskState.AWAITING_APPROVAL, target=TaskState.EXECUTING
        ):
            raise InvalidTransition(f"task {task_id} is not awaiting approval")
        return self.advance(task_id)

    def reject(self, task_id: str, reason: str = "rejected by the human") -> TaskRow:
        row = self.repo.get(task_id)
        if row is None:
            raise KeyError(task_id)
        state = TaskState(row.state)
        if state not in _ACTIONABLE:
            raise InvalidTransition(f"task {task_id} cannot be rejected from {row.state}")
        # Claim before recording: writing the reason first left it stamped on tasks
        # whose rejection was then refused, corrupting records of work that succeeded.
        if not self.repo.claim(task_id, expected=state, target=TaskState.FAILED):
            raise InvalidTransition(f"task {task_id} cannot be rejected from {row.state}")
        self.repo.record_failure(task_id, reason)
        return self.repo.get(task_id)

    def override(self, task_id: str, mode: AutonomyMode | None) -> TaskRow:
        """Pin the mode, or pass None to clear the pin and follow the recommendation."""
        row = self.repo.get(task_id)
        if row is None:
            raise KeyError(task_id)
        state = TaskState(row.state)
        if state not in _ACTIONABLE:
            # Same class of bug c043c46 fixed for reject(): stamping
            # mode_override on finished (or mid-flight) work is not a
            # correction, it's silent data loss.
            raise InvalidTransition(f"task {task_id} cannot change mode from {row.state}")
        self.repo.set_override(task_id, mode.value if mode is not None else None)
        row = self.repo.get(task_id)
        if TaskState(row.state) is TaskState.AWAITING_APPROVAL:
            # Send it back through the gate so the new mode is actually applied.
            # This is what makes flipping the dial to Auto release a parked task.
            self.repo.claim(
                task_id, expected=TaskState.AWAITING_APPROVAL, target=TaskState.INTERPRETED
            )
        return self.advance(task_id)

    def edit_spec(self, task_id: str, patch: dict) -> TaskRow:
        row = self.repo.get(task_id)
        if row is None:
            raise KeyError(task_id)
        if not row.spec:
            raise InvalidTransition(f"task {task_id} has no spec to edit yet")
        state = TaskState(row.state)
        if state not in _ACTIONABLE:
            # Same class of bug c043c46 fixed for reject(): rewriting the spec
            # of work that is already done (or mid-flight) is not a correction.
            raise InvalidTransition(f"task {task_id} cannot edit spec from {row.state}")

        # extra="forbid" on TaskSpec turns a misspelled key into a ValidationError
        # here rather than a silently dropped edit. The API maps it to a 422.
        spec = TaskSpec.model_validate({**row.spec, **patch})
        self.repo.save_spec(task_id, spec)

        # Re-enter scoring, NOT interpretation: an edit changes confidence and
        # risk, so the recommendation must be recomputed — but re-running the
        # interpreter would overwrite the human's correction with the model's
        # original reading.
        self.repo.claim(task_id, expected=state, target=TaskState.INTERPRETED)
        return self.advance(task_id)

    # --- automatic steps --------------------------------------------------

    def _classify(self, row: TaskRow) -> bool:
        # Classification already happened: the crystallizer decided this was a
        # real work request before it became a Task. The state is kept because
        # §5.9 names it and project routing will hang off it in Phase 0.5.0.
        return self.repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)

    def _interpret(self, row: TaskRow) -> bool:
        try:
            spec = self.interpreter.interpret(row)
        except MalformedSpec:
            logger.info("task %s produced no valid spec; handing it to a human", row.id)
            self.repo.set_open_question(
                row.id,
                "I could not turn this into a specification. What exactly should I do?",
            )
            return self.repo.claim(
                row.id, expected=TaskState.CLASSIFIED, target=TaskState.NEEDS_CLARIFICATION
            )
        except Exception:
            # A broken connection is not a broken request. Leave the task in
            # CLASSIFIED and let the sweeper try again — that retry loop already
            # exists, so no backoff machinery is needed here.
            attempts = self.repo.increment_interpret_attempts(row.id)
            logger.exception("interpreting task %s failed (attempt %d)", row.id, attempts)
            if attempts >= _MAX_INTERPRET_ATTEMPTS:
                # Claim before recording: the same inversion c043c46 fixed in
                # reject(). Recording first would stamp a failure_reason onto a
                # task whose transition to FAILED then lost the race (another
                # caller already moved it), corrupting the record of whatever
                # that caller's outcome was.
                if self.repo.claim(
                    row.id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
                ):
                    self.repo.record_failure(
                        row.id, f"interpreter unavailable after {attempts} attempts"
                    )
            return False

        self.repo.save_spec(row.id, spec)
        if spec.missing_fields and (row.clarification_rounds or 0) < _MAX_CLARIFICATION_ROUNDS:
            self.repo.set_open_question(row.id, _question_for(spec.missing_fields))
            return self.repo.claim(
                row.id, expected=TaskState.CLASSIFIED, target=TaskState.NEEDS_CLARIFICATION
            )

        # Either the spec is complete, or we have asked enough times. The gaps
        # stay visible in spec.missing_fields; we simply stop asking about them.
        self.repo.set_open_question(row.id, None)
        return self.repo.claim(
            row.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED
        )

    def _gate(self, row: TaskRow) -> bool:
        spec = TaskSpec.model_validate(row.spec)
        candidate = self.candidates.get(row.candidate_id) if row.candidate_id else None
        recommendation = recommend(
            spec,
            candidate_missing_fields=list(candidate.missing_fields or []) if candidate else [],
        )
        updated = self.repo.save_recommendation(
            row.id,
            mode=recommendation.mode.value,
            confidence=recommendation.confidence,
            risk=recommendation.risk,
            reason=recommendation.reason,
        )
        # effective_mode is only meaningful once the recommendation is stored, and
        # a human override set earlier must still beat what we just computed.
        # save_recommendation returns the updated row, so no second read is needed.
        # (A concurrent override arriving mid-scoring is not reachable while the
        # orchestrator is synchronous; per-project concurrency is Phase 0.5.0.)
        effective = updated.effective_mode
        target = (
            TaskState.EXECUTING
            if effective == AutonomyMode.AUTO.value
            else TaskState.AWAITING_APPROVAL
        )
        return self.repo.claim(row.id, expected=TaskState.INTERPRETED, target=target)

    def _execute(self, row: TaskRow) -> bool:
        # Still a stub. Phase 0.4.0 replaces this with the synthesis-first
        # executor and the Docker sandbox (§5.10).
        return self.repo.claim(row.id, expected=TaskState.EXECUTING, target=TaskState.VALIDATING)

    def _validate(self, row: TaskRow) -> bool:
        return self.repo.claim(row.id, expected=TaskState.VALIDATING, target=TaskState.DONE)


_STEPS: dict[TaskState, Callable[[TaskDriver, TaskRow], bool]] = {
    TaskState.RECEIVED: TaskDriver._classify,
    TaskState.CLASSIFIED: TaskDriver._interpret,
    TaskState.INTERPRETED: TaskDriver._gate,
    TaskState.EXECUTING: TaskDriver._execute,
    TaskState.VALIDATING: TaskDriver._validate,
}


def _question_for(missing_fields: list[str]) -> str:
    return (
        f"Before I start, I still need: {', '.join(missing_fields)}. Can you fill those in?"
    )
