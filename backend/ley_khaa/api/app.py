from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from ..config import settings
from ..crystallizer.gate import ReadinessGate
from ..db import SessionLocal, init_db
from ..intake.simulator import Simulator
from ..llm.factory import build_llm
from ..orchestrator.orchestrator import Orchestrator
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.repository import TaskRepository
from .schemas import CandidateOut, IntakeOut, MessageIn, MessageOut, TaskOut


def build_orchestrator(session: Session) -> Orchestrator:
    return Orchestrator(
        TaskRepository(session),
        llm=build_llm(settings.llm_backend),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(settings.crystallizer_debounce_seconds),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.disable_startup:
        yield
        return
    init_db()
    session = SessionLocal()
    try:
        repo = TaskRepository(session)
        if not repo.list():
            Simulator(build_orchestrator(session)).replay("messy_universe_check")
    finally:
        session.close()
    yield


app = FastAPI(title="ley-khaa", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
