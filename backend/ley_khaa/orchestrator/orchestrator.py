from ..domain.models import Message
from ..domain.states import TaskState
from ..persistence.orm import TaskRow
from ..persistence.repository import TaskRepository

# Foundation stub: walk the real lifecycle with no real logic. Later phases
# replace this with crystallizer -> interpreter -> autonomy -> executor.
STUB_PATH: list[TaskState] = [
    TaskState.CLASSIFIED,
    TaskState.INTERPRETED,
    TaskState.EXECUTING,
    TaskState.VALIDATING,
    TaskState.DONE,
]


class Orchestrator:
    def __init__(self, repo: TaskRepository) -> None:
        self.repo = repo

    def ingest(self, message: Message) -> TaskRow:
        task = self.repo.create(
            project="default",
            title=message.text[:80],
            source_message_ids=[message.id],
        )
        for state in STUB_PATH:
            self.repo.update_state(task.id, state)
        return self.repo.get(task.id)
