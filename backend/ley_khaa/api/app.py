from collections.abc import Iterator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, init_db
from ..domain.models import Message
from ..orchestrator.orchestrator import Orchestrator
from ..persistence.repository import TaskRepository
from .schemas import MessageIn, TaskOut

app = FastAPI(title="ley-khaa")
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


@app.post("/messages", response_model=TaskOut)
def post_message(body: MessageIn, session: Session = Depends(get_session)) -> TaskOut:
    orch = Orchestrator(TaskRepository(session))
    task = orch.ingest(Message(**body.model_dump()))
    return TaskOut.model_validate(task)


@app.get("/tasks", response_model=list[TaskOut])
def list_tasks(session: Session = Depends(get_session)) -> list[TaskOut]:
    return [TaskOut.model_validate(t) for t in TaskRepository(session).list()]


@app.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, session: Session = Depends(get_session)) -> TaskOut:
    row = TaskRepository(session).get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskOut.model_validate(row)


@app.on_event("startup")
def _startup() -> None:
    if settings.disable_startup:
        return
    init_db()
    session = SessionLocal()
    try:
        repo = TaskRepository(session)
        if not repo.list():
            Orchestrator(repo).ingest(
                Message(
                    source="simulator",
                    client="demo",
                    conversation_id="conv-seed",
                    author="boss",
                    text="Compare the Bloomberg universe against FactSet and send me what's missing.",
                )
            )
    finally:
        session.close()
