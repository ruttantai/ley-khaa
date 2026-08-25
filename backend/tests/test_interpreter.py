import pytest
from pydantic import ValidationError

from ley_khaa.domain.models import Message
from ley_khaa.interpreter.interpreter import Interpreter, MalformedSpec
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.router import OPUS
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two universes",
        inputs=["bloomberg", "factset"],
        operation="set_difference",
        output_format="xlsx",
        certainty=0.9,
    )
    return TaskSpec(**{**base, **overrides})


def _task_with_messages(session, texts):
    messages = MessageRepository(session)
    rows = [
        messages.add(Message(source="s", client="c", conversation_id="conv-1", author="boss", text=t))
        for t in texts
    ]
    task = TaskRepository(session).create(
        project="default", title="compare universes", source_message_ids=[r.id for r in rows]
    )
    return task, rows


def test_interpret_returns_a_validated_spec(session):
    task, _ = _task_with_messages(session, ["compare bloomberg against factset"])
    llm = FakeLLM([_spec()])
    spec = Interpreter(llm, MessageRepository(session)).interpret(task)
    assert spec.operation == "set_difference"


def test_interpret_routes_to_opus(session):
    task, _ = _task_with_messages(session, ["compare bloomberg against factset"])
    llm = FakeLLM([_spec()])
    Interpreter(llm, MessageRepository(session)).interpret(task)
    assert llm.calls[0].choice.model == OPUS
    assert llm.calls[0].choice.supports_thinking is True


def test_the_prompt_carries_the_tasks_own_messages(session):
    task, rows = _task_with_messages(session, ["compare bloomberg against factset", "as excel"])
    llm = FakeLLM([_spec()])
    Interpreter(llm, MessageRepository(session)).interpret(task)
    user = llm.calls[0].user
    assert "compare bloomberg against factset" in user
    assert "as excel" in user
    assert rows[0].id in user


def test_malformed_output_is_re_prompted_once_then_succeeds(session):
    task, _ = _task_with_messages(session, ["compare bloomberg against factset"])
    bad = ValidationError.from_exception_data("TaskSpec", [])
    llm = FakeLLM([bad, _spec()])
    spec = Interpreter(llm, MessageRepository(session)).interpret(task)
    assert spec.operation == "set_difference"
    assert len(llm.calls) == 2
    # The retry says something the first prompt did not, or it is not a retry.
    assert llm.calls[1].system != llm.calls[0].system


def test_malformed_twice_raises_rather_than_looping(session):
    task, _ = _task_with_messages(session, ["compare bloomberg against factset"])
    bad = ValidationError.from_exception_data("TaskSpec", [])
    llm = FakeLLM([bad, bad])
    with pytest.raises(MalformedSpec):
        Interpreter(llm, MessageRepository(session)).interpret(task)
    assert len(llm.calls) == 2


def test_transport_failure_propagates_without_consuming_the_retry(session):
    """A network error is not bad content: the driver retries it, not the interpreter."""
    task, _ = _task_with_messages(session, ["compare bloomberg against factset"])
    llm = FakeLLM([ConnectionError("boom")])
    with pytest.raises(ConnectionError):
        Interpreter(llm, MessageRepository(session)).interpret(task)
    assert len(llm.calls) == 1


def test_hallucinated_message_ids_are_dropped(session):
    """Same lesson as the crystallizer: model-supplied ids are untrusted."""
    task, rows = _task_with_messages(session, ["compare bloomberg against factset"])
    llm = FakeLLM([_spec(source_message_ids=[rows[0].id, "not-a-real-id"])])
    spec = Interpreter(llm, MessageRepository(session)).interpret(task)
    assert spec.source_message_ids == [rows[0].id]
