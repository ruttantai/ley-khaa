from ley_khaa.crystallizer.candidate import CandidateState
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.project_repository import ProjectRepository
from ley_khaa.projects.seeds import ensure_default_project


def test_the_default_project_is_seeded_idempotently(session):
    ensure_default_project(session)
    ensure_default_project(session)
    projects = ProjectRepository(session)
    assert projects.get("default") is not None
    assert len(projects.active()) == 1


def test_a_bound_conversation_puts_its_task_in_that_project(session, stub_execution):
    """The DoD line: a message from a bound client lands in that client's project,
    not in `default`."""
    from ley_khaa.api.app import build_orchestrator

    ensure_default_project(session)
    projects = ProjectRepository(session)
    projects.create("acme", description="Acme's equity books")
    projects.bind("simulator", "acme", "C-acme", "acme", stage="manual")

    orchestrator = build_orchestrator(session)
    orchestrator.ingest(
        {
            "text": "compare the bloomberg universe against the factset universe, csv",
            "conversation_id": "C-acme",
            "client": "acme",
        }
    )
    orchestrator.sweep()

    tasks = orchestrator.repo.list()
    assert tasks, "the conversation produced no task"
    assert {t.project for t in tasks} == {"acme"}


def test_two_clients_land_in_two_projects(session, stub_execution):
    from ley_khaa.api.app import build_orchestrator

    ensure_default_project(session)
    projects = ProjectRepository(session)
    projects.create("acme", description="Acme's books")
    projects.create("globex", description="Globex's books")
    projects.bind("simulator", "acme", "", "acme", stage="manual")
    projects.bind("simulator", "globex", "", "globex", stage="manual")

    orchestrator = build_orchestrator(session)
    for client, conversation in (("acme", "C-a"), ("globex", "C-g")):
        orchestrator.ingest(
            {
                "text": "compare the bloomberg universe against the factset universe, csv",
                "conversation_id": conversation,
                "client": client,
            }
        )
    orchestrator.sweep()

    by_project = {t.project for t in orchestrator.repo.list()}
    assert by_project == {"acme", "globex"}


def test_an_unroutable_conversation_still_produces_a_task(session, stub_execution):
    """Routing must never drop work — the offline HeuristicLLM never matches, so
    this is the ordinary fresh-clone path."""
    from ley_khaa.api.app import build_orchestrator

    ensure_default_project(session)
    orchestrator = build_orchestrator(session)
    orchestrator.ingest(
        {
            "text": "compare the bloomberg universe against the factset universe, csv",
            "conversation_id": "C-unknown",
            "client": "nobody",
        }
    )
    orchestrator.sweep()

    tasks = orchestrator.repo.list()
    assert tasks
    assert {t.project for t in tasks} == {"default"}


def test_a_candidate_with_no_messages_routes_to_default_rather_than_raising(session):
    """Defensive: _promote reads source/client off the candidate's messages, and
    a candidate with none must not take intake down with an IndexError."""
    from ley_khaa.api.app import build_orchestrator

    ensure_default_project(session)
    orchestrator = build_orchestrator(session)
    candidate = orchestrator.candidates.upsert(
        conversation_id="C-empty",
        candidate_key="k",
        title="orphan",
        summary="",
        state=CandidateState.READY,
        message_ids=[],
        missing_fields=[],
        open_question=None,
    )
    task_id = orchestrator._promote(candidate)
    assert orchestrator.repo.get(task_id).project == "default"
