import logging
from collections.abc import Callable

from pydantic import ValidationError

from ..adapters.base import Destination
from ..adapters.notifier import Notifier, NullNotifier, message_for
from ..autonomy.engine import recommend
from ..autonomy.modes import AutonomyMode
from ..config import settings
from ..domain.states import WAITING as _WAITING
from ..domain.states import InvalidTransition, TaskState
from ..executor.runner import ExecutionRunner
from ..executor.sandbox import SandboxUnavailable
from ..interpreter.interpreter import Interpreter, MalformedSpec
from ..interpreter.spec import TaskSpec
from ..llm.client import EmptyModelResponse, LLMClient
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
        notifier: Notifier | None = None,
        extractor=None,
    ) -> None:
        self.repo = repo
        self.messages = messages
        self.candidates = candidates
        self.interpreter = Interpreter(llm, messages, extractor=extractor)
        # Constructing this is cheap: the sandbox itself is resolved on first
        # use, so a driver built for a request that executes nothing never
        # probes the Docker daemon.
        self.executor = ExecutionRunner(
            llm=llm, messages=messages, workflows=workflows, extractor=extractor
        )
        self.memories = memories
        self.memory = MemoryMatcher(memories, llm) if memories is not None else None
        # NullNotifier by default, so every existing test and every token-free
        # run behaves exactly as it did before this phase.
        self.notifier = notifier or NullNotifier()

    def advance(self, task_id: str) -> TaskRow:
        """Push a task as far as it can go unattended, then return where it landed.

        One exit point, so no return path can forget to announce. The driving
        itself is unchanged and lives in _drive.
        """
        row = self._drive(task_id)
        self._announce(row)
        return row

    def _drive(self, task_id: str) -> TaskRow:
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
        row = self.repo.get(task_id)
        # reject() moves a task to FAILED on its own, so advance()'s single exit
        # point does not cover it. Without this the human who was waiting on the
        # question is never told the task is over.
        self._announce(row)
        return row

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
        if not self.repo.set_override(
            task_id, mode.value if mode is not None else None, expected=state
        ):
            # The check above and the write are two statements, so the task can
            # leave `state` in between — a dispatcher worker approving it, a
            # second tab rejecting it. Unconditional, the write landed anyway:
            # the mode a human chose for work that was still pending ended up
            # stamped on work that had already run. Reporting the loss (a 409 at
            # the route) is the only honest answer — the instruction did not take.
            raise InvalidTransition(
                f"task {task_id} moved on from {state.value} before the mode change applied"
            )
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
        except EmptyModelResponse as exc:
            # A content problem, not a broken connection: the model returned
            # no parsed output (most likely max_tokens truncating an
            # oversized prompt). This must not be recorded as "unavailable"
            # — that label sends an operator hunting for a network or
            # API-key problem that does not exist, and the real cause (the
            # prompt was too long) appears nowhere. Same retry bookkeeping as
            # any other interpretation failure (see _fail_interpret); whether
            # to retry with a shortened prompt or escalate to a human sooner
            # is a design decision, filed to the phase's closure task — out
            # of scope here.
            return self._fail_interpret(row, cause=str(exc))
        except Exception:
            # A broken connection is not a broken request. Leave the task in
            # CLASSIFIED and let the sweeper try again — that retry loop already
            # exists, so no backoff machinery is needed here.
            return self._fail_interpret(row, cause=None)

        return self._after_spec(row, spec)

    def _fail_interpret(self, row: TaskRow, *, cause: str | None) -> bool:
        """Shared bookkeeping for every interpretation failure: same attempt
        counter, same retry threshold, same claim-before-record ordering
        (c043c46) regardless of what went wrong. Only `cause` differs by
        branch — when known, it replaces the generic "unavailable" label so
        the recorded reason names what actually happened rather than lumping
        every failure into one bucket.
        """
        attempts = self.repo.increment_interpret_attempts(row.id)
        logger.exception("interpreting task %s failed (attempt %d)", row.id, attempts)
        if attempts >= _MAX_INTERPRET_ATTEMPTS:
            # Claim before recording: the same inversion c043c46 fixed in
            # reject(). Recording first would stamp a failure_reason onto a
            # task whose transition to FAILED then lost the race (another
            # caller already moved it), corrupting the record of whatever
            # that caller's outcome was.
            if self.repo.claim(row.id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED):
                reason = (
                    f"interpreter failed after {attempts} attempts: {cause}"
                    if cause is not None
                    else f"interpreter unavailable after {attempts} attempts"
                )
                self.repo.record_failure(row.id, reason)
        return False

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
        #
        # `updated` is read before the claim below, so an override committing in
        # between would not be reflected in `effective`. No override CAN commit
        # in that window: TaskRepository.set_override writes under
        # `WHERE state = <the state override() observed>`, and override() only
        # ever observes a state in _ACTIONABLE (AWAITING_APPROVAL,
        # NEEDS_CLARIFICATION). This method runs only on a task in INTERPRETED —
        # _STEPS dispatches on the row's state and the claim below re-checks it —
        # and a task is in exactly one state, so any override racing this pass
        # matches zero rows and is reported to its caller as a 409 rather than
        # silently lost. That is the whole of the guarantee: it says nothing
        # about an override that commits BEFORE this pass reads the row (which
        # is correctly honoured) or AFTER the claim (which re-enters scoring via
        # override()'s own AWAITING_APPROVAL -> INTERPRETED claim).
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

    def _announce(self, row: TaskRow | None) -> None:
        """Tell the originating channel, at most once per state (spec §3.6).

        Everything here is best-effort and nothing here can fail a task: a
        wedged platform API must not be able to stop work from completing. The
        order is claim-then-send — mark_notified is a compare-and-swap, so a
        re-entrant advance() or a second worker cannot repeat the message.
        """
        if row is None:
            return
        try:
            text = message_for(row)
            if text is None:
                return
            dest = self._destination(row)
            if dest is None:
                # No originating message means no channel to answer into. A
                # task created directly (a test, a future CLI) is not a failure.
                return
            # The question is only part of the compare-and-swap key for
            # NEEDS_CLARIFICATION (backlog item 17: a second, different
            # question asked without leaving the state must still be
            # delivered). Every other NOTIFY_STATE keeps the original
            # state-only behaviour — open_question is not guaranteed cleared
            # by the time e.g. AWAITING_APPROVAL is reached, and folding it
            # into the key there is not what this fix is for.
            question = (
                row.open_question
                if row.state == TaskState.NEEDS_CLARIFICATION.value
                else None
            )
            if not self.repo.mark_notified(row.id, row.state, question):
                return
            self.notifier.notify(dest, text)
        except Exception:
            logger.exception("could not announce task %s", row.id)

    def _destination(self, row: TaskRow) -> Destination | None:
        """Where this task's channel conversation is.

        No mapping table (§3.6): the originating MessageRow already carries
        source, conversation_id and external_id. The FIRST source message is the
        anchor — it is the one that started the thread, and its external_id is
        what a threaded reply hangs under.
        """
        sources = self.messages.get_many(list(row.source_message_ids or []))
        if not sources:
            return None
        first = sources[0]
        return Destination(
            source=first.source,
            conversation_id=first.conversation_id,
            external_id=first.external_id,
        )


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
