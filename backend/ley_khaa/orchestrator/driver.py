import logging
from collections.abc import Callable

from pydantic import ValidationError

from ..autonomy.engine import recommend
from ..autonomy.modes import AutonomyMode
from ..config import settings
from ..domain.states import WAITING as _WAITING
from ..domain.states import InvalidTransition, TaskState
from ..executor.runner import ExecutionRunner
from ..executor.sandbox import SandboxUnavailable
from ..interpreter.interpreter import Interpreter, MalformedSpec
from ..interpreter.spec import TaskSpec
from ..llm.client import LLMClient
from ..memory.fingerprint import request_fingerprint
from ..memory.matcher import MemoryMatcher
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.memory_repository import MemoryRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import MemoryRow, TaskRow
from ..persistence.repository import TaskRepository
from ..persistence.workflow_repository import WorkflowRepository

logger = logging.getLogger(__name__)

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
        workflows: WorkflowRepository | None = None,
        memories: MemoryRepository | None = None,
    ) -> None:
        self.repo = repo
        self.messages = messages
        self.candidates = candidates
        self.interpreter = Interpreter(llm, messages)
        # Constructing this is cheap: the sandbox itself is resolved on first
        # use, so a driver built for a request that executes nothing never
        # probes the Docker daemon.
        self.executor = ExecutionRunner(llm=llm, messages=messages, workflows=workflows)
        self.memories = memories
        self.memory = MemoryMatcher(memories, llm) if memories is not None else None

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

    def hand_off(self, task_id: str) -> TaskRow:
        """Carry on after something made this task runnable.

        Inline mode drives it here, on the caller's thread — the behaviour every
        release before 0.6.0 had. In workers mode the task is now runnable and
        the dispatcher will lease it, so this returns immediately: an HTTP
        request must not block through two Opus calls and a sandbox run.

        advance() itself is unchanged and is what the dispatcher calls. The split
        is only about who does the driving, never about what driving means.
        """
        if settings.dispatch_mode == "inline":
            return self.advance(task_id)
        return self.repo.get(task_id)

    # --- human actions ----------------------------------------------------

    def approve(self, task_id: str) -> TaskRow:
        if not self.repo.claim(
            task_id, expected=TaskState.AWAITING_APPROVAL, target=TaskState.EXECUTING
        ):
            raise InvalidTransition(f"task {task_id} is not awaiting approval")
        return self.hand_off(task_id)

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
        return self.hand_off(task_id)

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
        return self.hand_off(task_id)

    # --- automatic steps --------------------------------------------------

    def _classify(self, row: TaskRow) -> bool:
        # Classification already happened: the crystallizer decided this was a
        # real work request before it became a Task. The state is kept because
        # §5.9 names it. Project routing (§5.4, shipped in the routing/queues/
        # amendments phase) turned out not to hang off it: Orchestrator._promote
        # decides the project via _route() before TaskRepository.create, so a
        # task is already routed the moment it — and this CLASSIFIED state —
        # first exist.
        return self.repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)

    def _interpret(self, row: TaskRow) -> bool:
        remembered = self._recall(row)
        if remembered is not None:
            # Only the shape is reused. source_message_ids is re-pointed at THIS
            # task's messages, and `inputs` are names that the resolver resolves
            # against this task at execution time — last week's spec must not be
            # able to quietly reuse last week's file.
            spec = TaskSpec.model_validate(remembered.spec).model_copy(
                update={"source_message_ids": list(row.source_message_ids or [])}
            )
            won = self._after_spec(row, spec)
            if won:
                # Same ordering rule: attribution belongs to the caller that
                # actually took the task down the remembered path.
                self.repo.save_memory_hit(
                    row.id,
                    source_task_id=remembered.source_task_id,
                    familiarity=remembered.times_seen,
                )
            return won

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

        return self._after_spec(row, spec)

    def _recall(self, row: TaskRow) -> MemoryRow | None:
        if self.memory is None:
            return None
        rows = self.messages.get_many(list(row.source_message_ids or []))
        return self.memory.recall(row.project, [r.text for r in rows])

    def _after_spec(self, row: TaskRow, spec: TaskSpec) -> bool:
        """Everything that happens once a spec exists, however it was obtained.

        The claim comes FIRST. Writing the spec before winning the transition
        left a task that lost the race carrying a spec for a path it never took
        — the same inversion c043c46 fixed in reject(), and backlog item 6.
        _remember already gets this right and is the model followed here.

        This trades one race for a narrower one, and does not eliminate it:
        the claim and save_spec commit are two separate transactions, so
        between them the row is briefly INTERPRETED (or NEEDS_CLARIFICATION)
        with spec still None — the old bug was a spec without the state; this
        is a state without the spec. A reader landing in that exact window
        would hit TaskSpec.model_validate(None). Closing it for real needs the
        claim and the write in one transaction, which is out of scope here.
        The window is narrow in practice: workers mode only lets a leased
        task's owner drive it further (_runnable_where excludes leased rows),
        advance_stalled is inline-only, there is no I/O between the two
        commits, and the next sweep self-heals a task caught mid-gap — but the
        gap is real, not merely theoretical, and is left open rather than
        silently assumed away.
        """
        asking = bool(spec.missing_fields) and (
            (row.clarification_rounds or 0) < _MAX_CLARIFICATION_ROUNDS
        )
        target = TaskState.NEEDS_CLARIFICATION if asking else TaskState.INTERPRETED
        if not self.repo.claim(row.id, expected=TaskState.CLASSIFIED, target=target):
            return False

        self.repo.save_spec(row.id, spec)
        self.repo.set_open_question(
            row.id, _question_for(spec.missing_fields) if asking else None
        )
        return True

    def _gate(self, row: TaskRow) -> bool:
        spec = TaskSpec.model_validate(row.spec)
        candidate = self.candidates.get(row.candidate_id) if row.candidate_id else None
        recommendation = recommend(
            spec,
            candidate_missing_fields=list(candidate.missing_fields or []) if candidate else [],
            familiarity=row.familiarity or 0,
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
        # (A concurrent override arriving mid-scoring IS reachable now, in the
        # default workers mode: this runs on a dispatcher worker thread
        # (Dispatcher._work_one -> asyncio.to_thread -> driver.advance), while
        # POST /tasks/{id}/mode is served on FastAPI's own threadpool. An
        # override committed after save_recommendation's refresh above and
        # before the claim below is not reflected in `effective` — `updated`
        # was already read — so this scoring pass can decide EXECUTING vs
        # AWAITING_APPROVAL as though the override were never set. The claim
        # below still protects the STATE transition itself, not this decision;
        # not closed here.)
        effective = updated.effective_mode
        target = (
            TaskState.EXECUTING
            if effective == AutonomyMode.AUTO.value
            else TaskState.AWAITING_APPROVAL
        )
        return self.repo.claim(row.id, expected=TaskState.INTERPRETED, target=target)

    def _execute(self, row: TaskRow) -> bool:
        try:
            spec = TaskSpec.model_validate(row.spec or {})
        except ValidationError:
            # Nothing to execute, and no question worth asking: a task reaching
            # EXECUTING without a valid spec is our bug, not the human's.
            logger.exception("task %s reached execution with no usable spec", row.id)
            if self.repo.claim(row.id, expected=TaskState.EXECUTING, target=TaskState.FAILED):
                self.repo.record_failure(row.id, "no valid specification to execute")
            return False

        try:
            outcome = self.executor.run(row, spec)
        except SandboxUnavailable as exc:
            # Infrastructure, not the request. A dead daemon is not a question a
            # human can answer (spec §6), so this fails rather than escalating.
            logger.exception("sandbox unavailable while executing task %s", row.id)
            if self.repo.claim(row.id, expected=TaskState.EXECUTING, target=TaskState.FAILED):
                self.repo.record_failure(row.id, f"the sandbox was unavailable: {exc}")
            return False

        self.repo.save_execution(
            row.id,
            workspace_path=outcome.workspace_path,
            verdict={
                "ok": outcome.verdict.ok,
                "reason": outcome.verdict.reason,
                "checks": outcome.verdict.checks,
                "attempts": outcome.attempts,
            },
        )
        return self.repo.claim(row.id, expected=TaskState.EXECUTING, target=TaskState.VALIDATING)

    def _validate(self, row: TaskRow) -> bool:
        """Act on the verdict _execute just recorded.

        Deliberately thin. The alternative — deciding inside _execute — needs an
        EXECUTING -> NEEDS_CLARIFICATION edge, and adding one is what makes an
        execute/validate loop possible in the first place (decision 6).
        """
        verdict = row.execution_verdict or {}
        if verdict.get("ok"):
            self.repo.set_open_question(row.id, None)
            claimed = self.repo.claim(
                row.id, expected=TaskState.VALIDATING, target=TaskState.DONE
            )
            if claimed:
                # Only a proven run is remembered — the same rule promotion
                # follows. Recording before the claim would remember a spec for
                # a task another caller had already moved.
                self._remember(row)
            return claimed

        self.repo.set_open_question(
            row.id, verdict.get("reason") or "The run did not produce a usable result."
        )
        return self.repo.claim(
            row.id, expected=TaskState.VALIDATING, target=TaskState.NEEDS_CLARIFICATION
        )

    def _remember(self, row: TaskRow) -> None:
        if self.memories is None or not row.spec:
            return
        try:
            rows = self.messages.get_many(list(row.source_message_ids or []))
            texts = [r.text for r in rows]
            fingerprint = request_fingerprint(texts)
            if not fingerprint:
                return
            spec = TaskSpec.model_validate(row.spec)
            self.memories.record(
                project=row.project,
                fingerprint=fingerprint,
                intent=spec.intent,
                spec=spec,
                task_id=row.id,
            )
        except Exception:
            # Remembering is a bonus, never a reason for a finished task to fail.
            logger.exception("could not remember task %s", row.id)


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
