import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..autonomy.modes import AutonomyMode
from ..config import settings
from ..crystallizer.gate import ReadinessGate
from ..db import SessionLocal, run_migrations
from ..domain.states import InvalidTransition
from ..intake.simulator import Simulator
from ..llm.factory import build_llm
from ..orchestrator.orchestrator import Orchestrator
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.repository import TaskRepository
from .schemas import (
    AnswerIn,
    CandidateOut,
    IntakeOut,
    MessageIn,
    MessageOut,
    ModeIn,
    RejectIn,
    SpecPatchIn,
    TaskOut,
)

logger = logging.getLogger(__name__)


def build_orchestrator(session: Session) -> Orchestrator:
    return Orchestrator(
        TaskRepository(session),
        llm=build_llm(settings.llm_backend),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(settings.crystallizer_debounce_seconds),
    )


def _sweep_once() -> int:
    """One sweep, on its own session. Synchronous: the orchestrator stays sync."""
    session = SessionLocal()
    try:
        orchestrator = build_orchestrator(session)
        promoted = len(orchestrator.sweep())
        # Also re-drive tasks that stalled mid-flight. This is what retries an
        # interpretation that hit a transport failure: the task sits in CLASSIFIED
        # and nothing else would ever pick it up.
        orchestrator.advance_stalled()
        return promoted
    finally:
        session.close()


async def _periodic_sweeper(interval: float, sweep: Callable[[], int] = _sweep_once) -> None:
    """Wake on an interval and give debounced candidates a chance to promote.

    ingest() is invoked BY the newest message, so the conversation is never quiet
    at that moment and a non-zero debounce can only ever clear later. sweep() is
    the trigger for that, and nothing called it: a live user posting to
    /messages watched candidates pile up in READY and never got tasks.

    The sweep itself runs in a worker thread so the sync orchestrator never
    blocks the event loop, and a failing sweep is logged but never kills the loop.
    CancelledError is a BaseException, so shutdown still cancels this cleanly.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            created = await asyncio.to_thread(sweep)
        except Exception:
            logger.exception("periodic candidate sweep failed")
        else:
            if created:
                logger.info("periodic candidate sweep promoted %d candidate(s)", created)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.disable_startup:
        app.state.sweeper = None
        yield
        return
    run_migrations()
    session = SessionLocal()
    try:
        repo = TaskRepository(session)
        if not repo.list():
            Simulator(build_orchestrator(session)).replay("messy_universe_check")
    finally:
        session.close()

    app.state.sweeper = asyncio.create_task(_periodic_sweeper(settings.sweep_interval_seconds))
    try:
        yield
    finally:
        app.state.sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.sweeper
        app.state.sweeper = None


app = FastAPI(title="ley-khaa", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(InvalidTransition)
def _handle_invalid_transition(request, exc: InvalidTransition) -> JSONResponse:
    """Acting on a task another tab already moved is a conflict, not a crash."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ValidationError)
def _handle_validation_error(request, exc: ValidationError) -> JSONResponse:
    """A bad edit_spec patch is the caller's mistake, so 422 rather than 500."""
    return JSONResponse(status_code=422, content={"detail": exc.errors(include_url=False)})


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/messages", response_model=IntakeOut)
def post_message(body: MessageIn, session: Session = Depends(get_session)) -> IntakeOut:
    result = build_orchestrator(session).ingest(body.model_dump())
    return IntakeOut(
        message_id=result.message_id,
        conversation_id=result.conversation_id,
        candidate_ids=[c.id for c in result.candidates],
        task_ids=result.task_ids,
    )


@app.get("/candidates", response_model=list[CandidateOut])
def list_candidates(session: Session = Depends(get_session)) -> list[CandidateOut]:
    return [CandidateOut.model_validate(c) for c in CandidateRepository(session).list_all()]


@app.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
def list_conversation_messages(
    conversation_id: str, session: Session = Depends(get_session)
) -> list[MessageOut]:
    rows = MessageRepository(session).list_for_conversation(conversation_id)
    return [MessageOut.model_validate(r) for r in rows]


@app.post("/simulate/{name}")
def simulate(name: str, session: Session = Depends(get_session)) -> dict[str, int]:
    sim = Simulator(build_orchestrator(session))
    try:
        results = sim.replay(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such conversation fixture")
    return {
        "messages_ingested": len(results),
        "tasks_created": sum(len(r.task_ids) for r in results),
    }


@app.post("/candidates/sweep")
def sweep_candidates(
    conversation_id: str | None = None, session: Session = Depends(get_session)
) -> dict[str, int]:
    task_ids = build_orchestrator(session).sweep(conversation_id)
    return {"tasks_created": len(task_ids)}


@app.get("/tasks", response_model=list[TaskOut])
def list_tasks(session: Session = Depends(get_session)) -> list[TaskOut]:
    return [TaskOut.model_validate(t) for t in TaskRepository(session).list()]


@app.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, session: Session = Depends(get_session)) -> TaskOut:
    row = TaskRepository(session).get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskOut.model_validate(row)


def _require_task(session: Session, task_id: str):
    row = TaskRepository(session).get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return row


@app.post("/tasks/{task_id}/approve", response_model=TaskOut)
def approve_task(task_id: str, session: Session = Depends(get_session)) -> TaskOut:
    _require_task(session, task_id)
    return TaskOut.model_validate(build_orchestrator(session).driver.approve(task_id))


@app.post("/tasks/{task_id}/reject", response_model=TaskOut)
def reject_task(
    task_id: str, body: RejectIn | None = None, session: Session = Depends(get_session)
) -> TaskOut:
    _require_task(session, task_id)
    reason = (body or RejectIn()).reason
    return TaskOut.model_validate(build_orchestrator(session).driver.reject(task_id, reason))


@app.post("/tasks/{task_id}/mode", response_model=TaskOut)
def set_task_mode(
    task_id: str, body: ModeIn, session: Session = Depends(get_session)
) -> TaskOut:
    _require_task(session, task_id)
    mode = AutonomyMode(body.mode) if body.mode is not None else None
    return TaskOut.model_validate(build_orchestrator(session).driver.override(task_id, mode))


@app.patch("/tasks/{task_id}/spec", response_model=TaskOut)
def patch_task_spec(
    task_id: str, body: SpecPatchIn, session: Session = Depends(get_session)
) -> TaskOut:
    _require_task(session, task_id)
    return TaskOut.model_validate(
        build_orchestrator(session).driver.edit_spec(task_id, body.patch)
    )


@app.post("/tasks/{task_id}/answer", response_model=TaskOut)
def answer_task(
    task_id: str, body: AnswerIn, session: Session = Depends(get_session)
) -> TaskOut:
    """Answer a clarification.

    The answer is posted as a real Message carrying reply_to_task_id, so it takes
    exactly the route a Slack thread reply will take — not a private dashboard
    path into the spec.
    """
    task = _require_task(session, task_id)
    sources = MessageRepository(session).get_many(list(task.source_message_ids or []))
    if not sources:
        raise HTTPException(status_code=409, detail="task has no conversation to reply into")

    build_orchestrator(session).ingest(
        {
            "source": "dashboard",
            "client": task.project,
            "conversation_id": sources[0].conversation_id,
            "author": body.author,
            "text": body.text,
            "reply_to_task_id": task_id,
        }
    )
    return TaskOut.model_validate(TaskRepository(session).get(task_id))
