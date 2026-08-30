from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.project_repository import ProjectRepository
from ley_khaa.projects.models import ProjectChoice
from ley_khaa.projects.router import ProjectRouter


def _repo(session):
    repo = ProjectRepository(session)
    repo.create("default", description="")
    repo.create("acme", description="Acme's equity books and universe checks")
    return repo


def test_a_bound_conversation_routes_free(session):
    projects = _repo(session)
    projects.bind("slack", "acme", "C9", "acme", stage="manual")
    llm = FakeLLM(responses=[])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="acme", conversation_id="C9", title="t", summary="s"
    )
    assert decision.project == "acme"
    assert decision.stage == "binding"
    # The load-bearing assertion: stage 1 is FREE. If this list is non-empty the
    # binding did not short-circuit and every later message pays for a model call.
    assert llm.calls == []


def test_an_unbound_conversation_asks_the_model_once(session):
    projects = _repo(session)
    llm = FakeLLM(responses=[ProjectChoice(project="acme", confidence=0.9, reason="equity books")])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="universe check", summary="s"
    )
    assert decision.project == "acme"
    assert decision.stage == "model"
    assert len(llm.calls) == 1


def test_a_confident_model_match_writes_a_binding_so_the_next_message_is_free(session):
    """The learning rule (spec §3.5), and the thing memory gets wrong: this
    updates the routing for that conversation instead of forking a second row."""
    projects = _repo(session)
    llm = FakeLLM(responses=[ProjectChoice(project="acme", confidence=0.9, reason="equity books")])
    router = ProjectRouter(projects, llm)
    router.route(source="slack", client="newco", conversation_id="C1", title="t", summary="s")

    assert projects.binding_for("slack", "newco", "C1").project == "acme"
    assert projects.binding_for("slack", "newco", "C1").created_by_stage == "model"

    second = router.route(
        source="slack", client="newco", conversation_id="C1", title="t2", summary="s2"
    )
    assert second.stage == "binding"
    # Still exactly one call in total — the second route paid nothing.
    assert len(llm.calls) == 1


def test_a_low_confidence_answer_falls_back_to_default_and_writes_no_binding(session):
    projects = _repo(session)
    llm = FakeLLM(responses=[ProjectChoice(project="acme", confidence=0.5, reason="maybe")])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="t", summary="s"
    )
    assert decision.project == "default"
    assert decision.stage == "default"
    assert projects.binding_for("slack", "newco", "C1") is None


def test_a_hallucinated_project_name_falls_back_rather_than_routing_nowhere(session):
    projects = _repo(session)
    llm = FakeLLM(responses=[ProjectChoice(project="ghost", confidence=0.99, reason="sure")])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="t", summary="s"
    )
    assert decision.project == "default"
    assert projects.binding_for("slack", "newco", "C1") is None
    # Distinguishes this from the routing-failed catch-all below: an unknown
    # name is a handled case, not a caught exception. Without this assertion a
    # missing `chosen is None` guard is invisible — the AttributeError it would
    # otherwise raise gets laundered by the outer `except Exception` into the
    # same project/binding outcome, so the two asserts above pass either way.
    assert decision.reason == "routed to an unknown project"


def test_an_inactive_project_is_not_offered_and_not_accepted(session):
    projects = _repo(session)
    acme = projects.get("acme")
    acme.active = False
    session.commit()
    llm = FakeLLM(responses=[ProjectChoice(project="acme", confidence=0.99, reason="sure")])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="t", summary="s"
    )
    assert decision.project == "default"


def test_a_failing_model_call_routes_to_default_instead_of_blocking_intake(session):
    """Routing must never drop a request. A misrouted task is recoverable by a
    human; a request that never became a task is not."""
    projects = _repo(session)
    llm = FakeLLM(responses=[RuntimeError("transport exploded")])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="t", summary="s"
    )
    assert decision.project == "default"
    assert decision.stage == "default"


def test_projects_without_a_description_are_not_shown_to_the_model(session):
    """A project with no description is unroutable by stage 2 by construction —
    the model would be guessing from a slug. `default` is exactly such a row."""
    projects = _repo(session)
    llm = FakeLLM(responses=[ProjectChoice(project=None, confidence=0.0, reason="no match")])
    ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="t", summary="s"
    )
    prompt = llm.calls[0].user
    assert "acme" in prompt
    assert "default" not in prompt
