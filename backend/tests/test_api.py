def test_post_message_returns_intake_ack(client):
    resp = client.post("/messages", json={"text": "compare the universes and send the difference"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"] == "conv-1"
    assert body["message_id"]
    assert len(body["task_ids"]) == 1


def test_post_noise_message_creates_no_task(client):
    body = client.post("/messages", json={"text": "morning all"}).json()
    assert body["task_ids"] == []
    assert client.get("/tasks").json() == []


def test_candidates_endpoint_exposes_state(client):
    client.post("/messages", json={"text": "compare the universes"})
    candidates = client.get("/candidates").json()
    assert len(candidates) == 1
    assert candidates[0]["state"] in {"ready", "promoted"}
    assert candidates[0]["conversation_id"] == "conv-1"


def test_message_with_attachment_is_accepted(client):
    resp = client.post(
        "/messages",
        json={
            "text": "compare these holdings",
            "attachments": [{"kind": "table", "name": "h.csv", "content": "a,b\n1,2"}],
        },
    )
    assert resp.status_code == 200


def test_conversation_messages_endpoint(client):
    client.post("/messages", json={"text": "compare the universes"})
    client.post("/messages", json={"text": "thanks!"})
    rows = client.get("/conversations/conv-1/messages").json()
    assert [r["text"] for r in rows] == ["compare the universes", "thanks!"]


def test_simulate_endpoint_replays_a_fixture(client):
    resp = client.post("/simulate/messy_universe_check")
    assert resp.status_code == 200
    assert resp.json()["messages_ingested"] == 9
    assert len(client.get("/tasks").json()) >= 1


def test_simulate_unknown_fixture_404(client):
    assert client.post("/simulate/nope").status_code == 404


def test_list_tasks_returns_created(client):
    client.post("/messages", json={"text": "compare the universes"})
    titles = {t["title"] for t in client.get("/tasks").json()}
    assert titles


def test_get_task_by_id(client):
    body = client.post("/messages", json={"text": "compare the universes"}).json()
    task_id = body["task_ids"][0]
    assert client.get(f"/tasks/{task_id}").json()["id"] == task_id


def test_get_missing_task_404(client):
    assert client.get("/tasks/does-not-exist").status_code == 404


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_sweep_with_nothing_ready_returns_zero(client):
    resp = client.post("/candidates/sweep")
    assert resp.status_code == 200
    assert resp.json() == {"tasks_created": 0}


def test_sweep_promotes_a_ready_candidate_once_the_conversation_goes_quiet(client, session):
    """Genuine exercise of /candidates/sweep.

    conftest pins the debounce to 0, under which a ready candidate always
    promotes inline at ingest — sweep would never have anything to do. To
    exercise sweep's real job (promoting a candidate whose debounce window
    elapsed only *after* ingest), this test temporarily raises the debounce
    so the candidate lands READY-but-not-promoted, then backdates the
    triggering message's timestamp to simulate the conversation going quiet,
    then asserts the sweep endpoint — and only the sweep endpoint — performs
    the promotion.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from ley_khaa.config import settings
    from ley_khaa.persistence.orm import MessageRow

    original_debounce = settings.crystallizer_debounce_seconds
    object.__setattr__(settings, "crystallizer_debounce_seconds", 5)
    try:
        body = client.post("/messages", json={"text": "compare the universes"}).json()
        assert body["task_ids"] == []

        candidates = client.get("/candidates").json()
        assert len(candidates) == 1
        assert candidates[0]["state"] == "ready"

        # Simulate the conversation having gone quiet long enough to clear
        # the debounce window: backdate the triggering message.
        row = session.scalars(select(MessageRow).where(MessageRow.id == body["message_id"])).first()
        row.timestamp = datetime.now(timezone.utc) - timedelta(seconds=30)
        session.commit()

        resp = client.post("/candidates/sweep")
        assert resp.status_code == 200
        assert resp.json() == {"tasks_created": 1}

        assert client.get("/candidates").json()[0]["state"] == "promoted"
        assert len(client.get("/tasks").json()) == 1
    finally:
        object.__setattr__(settings, "crystallizer_debounce_seconds", original_debounce)
