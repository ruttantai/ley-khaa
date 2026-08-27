import asyncio
import io
import logging
import zipfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..autonomy.modes import AutonomyMode
from ..config import settings
from ..crystallizer.gate import ReadinessGate
from ..db import SessionLocal, run_migrations
from ..domain.states import InvalidTransition
from ..executor.workspace import Workspace
from ..intake.simulator import Simulator
from ..llm.factory import build_llm
from ..orchestrator.orchestrator import ForeignReplyTarget, Orchestrator
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.repository import TaskRepository
from ..persistence.workflow_repository import WorkflowRepository
from ..registry.seeds import ensure_seed_workflows
from .schemas import (
    AnswerIn,
    BundleOut,
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
        workflows=WorkflowRepository(session),
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
        # The registry ships with two proven workflows so a fresh clone can show
        # the fast path before anyone has promoted anything.
        ensure_seed_workflows(session)
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


@app.exception_handler(KeyError)
def _handle_missing_entity(request, exc: KeyError) -> JSONResponse:
    """A reference to an id that does not exist (e.g. an unknown
    reply_to_task_id) is the caller's mistake, so 404 rather than 500."""
    missing = exc.args[0] if exc.args else str(exc)
    return JSONResponse(status_code=404, content={"detail": f"not found: {missing}"})


@app.exception_handler(ForeignReplyTarget)
def _handle_foreign_reply_target(request, exc: ForeignReplyTarget) -> JSONResponse:
    """A reply naming a task from a different conversation is a conflict
    between what the client asked for and what it is allowed to touch."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


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


# A generator script is a few KB. Anything this size is not source code, and
# streaming it into a JSON string would be a denial of service on the browser.
_MAX_INLINE_BYTES = 1_000_000

# The zip is built in memory, and what goes into it was written by synthesized
# code — a script that fills deliverable/ with gigabytes would otherwise be
# buffering them inside the backend process. Refused rather than truncated: half
# a bundle is not a bundle, and the individual-file routes still work.
_MAX_BUNDLE_BYTES = 100_000_000


def _bundle_root(session: Session, task_id: str) -> Path:
    row = _require_task(session, task_id)
    if not row.workspace_path:
        raise HTTPException(status_code=404, detail="this task has no bundle yet")
    root = Path(row.workspace_path)
    if not root.is_dir():
        # The row points at a bundle that is no longer on disk — a wiped volume,
        # or a database restored beside a different workspace root.
        raise HTTPException(status_code=404, detail="the bundle is no longer on disk")
    return root


def _contained(root: Path, candidate: Path) -> Path | None:
    """Resolve `candidate` and return it, but only if it stays inside `root`.

    resolve() follows symlinks, which is the point: the sandboxed generator
    that fills a bundle is untrusted code, and `os.symlink("/etc/passwd",
    "deliverable/out.csv")` is one line inside it. A symlink planted anywhere
    under the bundle — including inside a directory that is itself a
    symlink — resolves outside root and is rejected here exactly like a
    "../.." traversal is. Every route that reads bundle contents (the
    listing, the file viewer, the deliverable download, the zip download)
    must run every candidate path through this before touching it.
    """
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        return None
    return resolved


@app.get("/tasks/{task_id}/bundle", response_model=BundleOut)
def get_bundle(task_id: str, session: Session = Depends(get_session)) -> BundleOut:
    root = _bundle_root(session, task_id)
    files = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and _contained(root, path) is not None
    )
    return BundleOut(
        task_id=task_id,
        root=str(root),
        manifest=Workspace(root).read_manifest(),
        files=files,
        deliverables=[name for name in files if name.startswith("deliverable/")],
    )


@app.get("/tasks/{task_id}/bundle/file")
def get_bundle_file(
    task_id: str, path: str, session: Session = Depends(get_session)
) -> dict[str, str]:
    root = _bundle_root(session, task_id)
    target = _contained(root, root / path)
    if target is None:
        raise HTTPException(status_code=400, detail="path escapes the bundle")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="no such file in the bundle")
    if target.stat().st_size > _MAX_INLINE_BYTES:
        raise HTTPException(status_code=413, detail="file too large to view; download it instead")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=415, detail="not a text file; use the download endpoints"
        )
    return {"path": path, "content": content}


@app.get("/tasks/{task_id}/bundle/deliverable")
def download_deliverable(task_id: str, session: Session = Depends(get_session)) -> FileResponse:
    """The deliverable itself (spec §5.2). Separate from bundle/file because an
    .xlsx is not text and the code viewer's JSON envelope cannot carry it."""
    root = _bundle_root(session, task_id)
    produced = [
        path for path in Workspace(root).deliverables() if _contained(root, path) is not None
    ]
    if not produced:
        raise HTTPException(status_code=404, detail="this bundle has no deliverable")
    primary = produced[0]
    return FileResponse(primary, filename=primary.name)


@app.get("/tasks/{task_id}/bundle/download")
def download_bundle(task_id: str, session: Session = Depends(get_session)) -> StreamingResponse:
    root = _bundle_root(session, task_id)
    members = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and _contained(root, path) is not None
    ]
    total = sum(path.stat().st_size for path in members)
    if total > _MAX_BUNDLE_BYTES:
        raise HTTPException(
            status_code=413,
            detail="this bundle is too large to zip; fetch its files individually",
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            archive.write(path, path.relative_to(root))
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="task-{task_id}-bundle.zip"'},
    )
