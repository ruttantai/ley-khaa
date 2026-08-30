from ley_khaa.crystallizer.candidate import CandidateState
from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.project_repository import ProjectRepository
from ley_khaa.persistence.repository import TaskRepository
from ley_khaa.projects.seeds import ensure_default_project


def test_projects_lists_queue_depth(client, session):
    ensure_default_project(session)
    ProjectRepository(session).create("acme", description="Acme's books")
    repo = TaskRepository(session)
    row = repo.create(project="acme", title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)

    body = client.get("/projects").json()
    acme = next(p for p in body if p["name"] == "acme")
    assert acme["queue_depth"] == 1
    assert acme["in_flight"] is None


def test_projects_shows_which_task_is_leased(client, session):
    ensure_default_project(session)
    ProjectRepository(session).create("acme", description="Acme's books")
    repo = TaskRepository(session)
    row = repo.create(project="acme", title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim_lease(row.id, owner="w1", ttl_seconds=60)

    acme = next(p for p in client.get("/projects").json() if p["name"] == "acme")
    assert acme["in_flight"] == row.id
    assert acme["queue_depth"] == 0, "a leased task is being worked, not queued"


def test_creating_a_project_without_a_description_is_refused(client, session):
    ensure_default_project(session)
    response = client.post("/projects", json={"name": "acme", "description": "  "})
    assert response.status_code == 422
    assert "description" in response.json()["detail"].lower()


def test_creating_a_duplicate_project_is_a_conflict(client, session):
    ensure_default_project(session)
    body = {"name": "acme", "description": "Acme's books"}
    assert client.post("/projects", json=body).status_code == 201
    assert client.post("/projects", json=body).status_code == 409


def test_the_project_queue_is_in_fifo_order(client, session):
    ensure_default_project(session)
    ProjectRepository(session).create("acme", description="Acme's books")
    repo = TaskRepository(session)
    first = repo.create(project="acme", title="first", source_message_ids=[])
    second = repo.create(project="acme", title="second", source_message_ids=[])

    ids = [t["id"] for t in client.get("/projects/acme/queue").json()]
    assert ids == [first.id, second.id]


def _parked(session):
    from ley_khaa.persistence.candidate_repository import CandidateRepository

    repo = TaskRepository(session)
    target = repo.create(project="default", title="universe check", source_message_ids=["m1"])
    candidates = CandidateRepository(session)
    candidate = candidates.upsert(
        conversation_id="C1",
        candidate_key="k",
        title="also flag duplicates",
        summary="s",
        state=CandidateState.READY,
        message_ids=["m2"],
        missing_fields=[],
        open_question=None,
    )
    candidates.claim_for_triage(
        candidate.id, task_id=target.id, reason="also flag dupes", confidence=0.9
    )
    return candidate, target


def test_triage_lists_parked_proposals_with_their_reason(client, session):
    candidate, target = _parked(session)
    body = client.get("/triage").json()
    assert len(body) == 1
    assert body[0]["candidate_id"] == candidate.id
    assert body[0]["amends_task_id"] == target.id
    assert body[0]["amends_task_title"] == "universe check"
    assert "also flag dupes" in body[0]["reason"]


def test_folding_from_the_api_merges_the_messages(client, session):
    candidate, target = _parked(session)
    TaskRepository(session).claim(
        target.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED
    )
    response = client.post(f"/candidates/{candidate.id}/fold")
    assert response.status_code == 200
    assert response.json()["id"] == target.id
    assert "m2" in TaskRepository(session).get(target.id).source_message_ids


def test_separating_from_the_api_creates_its_own_task(client, session):
    ensure_default_project(session)
    candidate, target = _parked(session)
    response = client.post(f"/candidates/{candidate.id}/separate")
    assert response.status_code == 200
    assert response.json()["id"] != target.id


def test_posting_a_message_reports_where_the_work_went(client, session):
    """Spec §4: /messages no longer returns a task that has finished, so it has
    to say what it DID do — which project the work landed in, and whether it was
    queued rather than run."""
    ensure_default_project(session)
    body = client.post(
        "/messages",
        json={
            "text": "compare the bloomberg universe against the factset universe, csv",
            "conversation_id": "C1",
        },
    ).json()
    assert "project" in body
    assert "queued" in body
    assert body["project"] == "default"


def test_folding_a_candidate_that_is_not_parked_is_a_conflict(client, session):
    from ley_khaa.persistence.candidate_repository import CandidateRepository

    candidate = CandidateRepository(session).upsert(
        conversation_id="C1",
        candidate_key="k",
        title="t",
        summary="s",
        state=CandidateState.READY,
        message_ids=[],
        missing_fields=[],
        open_question=None,
    )
    assert client.post(f"/candidates/{candidate.id}/fold").status_code == 409
