import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.states import TaskState, ensure_transition
from .orm import TaskRow


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, *, project: str, title: str, source_message_ids: list[str]) -> TaskRow:
        row = TaskRow(
            id=str(uuid.uuid4()),
            project=project,
            state=TaskState.RECEIVED.value,
            title=title,
            source_message_ids=source_message_ids,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def get(self, task_id: str) -> TaskRow | None:
        return self.session.get(TaskRow, task_id)

    def list(self) -> list[TaskRow]:
        return list(self.session.scalars(select(TaskRow).order_by(TaskRow.created_at)))

    def update_state(self, task_id: str, target: TaskState) -> TaskRow:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        ensure_transition(TaskState(row.state), target)
        row.state = target.value
        self.session.commit()
        self.session.refresh(row)
        return row
