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


def test_empty_text_is_rejected_with_422(client):
    resp = client.post("/messages", json={"text": ""})
    assert resp.status_code == 422


def test_whitespace_only_text_is_rejected_with_422(client):
    resp = client.post("/messages", json={"text": "   "})
    assert resp.status_code == 422
    assert client.get("/conversations/conv-1/messages").json() == []


def test_missing_text_is_rejected_with_422(client):
    assert client.post("/messages", json={}).status_code == 422


def test_two_requests_in_one_conversation_yield_two_tasks_over_http(client):
    """The bug as reproduced: every request after the first returned task_ids []."""
    first = client.post("/messages", json={"text": "compare the Bloomberg universe against FactSet"})
    second = client.post("/messages", json={"text": "also build the risk report and send it"})
    assert len(first.json()["task_ids"]) == 1
    assert len(second.json()["task_ids"]) == 1
    assert len(client.get("/tasks").json()) == 2


def _parked_task(client):
    """Drive a complete request to a task waiting on a human.

    Deliberately NOT /simulate/messy_universe_check: under this test env's
    debounce_seconds=0, the offline heuristic marks a candidate "ready" the
    instant it owns one relevant message, so the readiness gate fires on the
    FIRST relevant message in that fixture rather than waiting for the second
    to arrive. The two-message request the fixture spreads across "compare the
    Bloomberg universe against FactSet" / "...send it as an Excel file" can
    therefore never assemble into one task here — it always splits into two
    single-message tasks, one missing output_format and the other missing
    inputs, and BOTH land in needs_clarification (see
    test_messy_conversation_yields_tasks_that_exclude_the_chatter in
    test_simulator.py, which documents and asserts exactly this split as
    intended Task 5 behaviour). A single message that states the whole
    request reaches awaiting_approval instead, with the operation/format/mode
    this suite's assertions expect.
    """
    client.post(
        "/messages",
        json={
            "text": (
                "compare the Bloomberg universe against FactSet and send "
                "the difference as an Excel file"
            )
        },
    )
    tasks = client.get("/tasks").json()
    assert tasks, "the request produced no task"
    return tasks[0]


def test_a_task_exposes_its_spec_and_recommendation(client):
    task = _parked_task(client)
    assert task["state"] == "awaiting_approval"
    assert task["spec"]["operation"] == "set_difference"
    assert task["recommended_mode"] in {"suggest", "copilot", "auto"}
    assert task["effective_mode"] == task["recommended_mode"]
    assert "→" in task["autonomy_reason"]


def test_approve_runs_the_task(client):
    task = _parked_task(client)
    response = client.post(f"/tasks/{task['id']}/approve")
    assert response.status_code == 200
    assert response.json()["state"] == "done"


def test_approving_twice_is_a_409(client):
    task = _parked_task(client)
    client.post(f"/tasks/{task['id']}/approve")
    assert client.post(f"/tasks/{task['id']}/approve").status_code == 409


def test_reject_records_the_reason(client):
    task = _parked_task(client)
    response = client.post(f"/tasks/{task['id']}/reject", json={"reason": "wrong universe"})
    assert response.json()["state"] == "failed"
    assert response.json()["failure_reason"] == "wrong universe"


def test_overriding_the_mode_to_auto_releases_the_task(client):
    task = _parked_task(client)
    response = client.post(f"/tasks/{task['id']}/mode", json={"mode": "auto"})
    assert response.status_code == 200
    assert response.json()["state"] == "done"
    assert response.json()["mode_override"] == "auto"


def test_clearing_the_override_is_accepted(client):
    task = _parked_task(client)
    client.post(f"/tasks/{task['id']}/mode", json={"mode": "suggest"})
    response = client.post(f"/tasks/{task['id']}/mode", json={"mode": None})
    assert response.json()["mode_override"] is None


def test_an_unknown_mode_is_a_422(client):
    task = _parked_task(client)
    assert client.post(f"/tasks/{task['id']}/mode", json={"mode": "yolo"}).status_code == 422


def test_editing_the_spec_rescores(client):
    task = _parked_task(client)
    response = client.patch(f"/tasks/{task['id']}/spec", json={"patch": {"output_format": "csv"}})
    assert response.status_code == 200
    assert response.json()["spec"]["output_format"] == "csv"


def test_a_misspelled_patch_key_is_a_422(client):
    task = _parked_task(client)
    response = client.patch(f"/tasks/{task['id']}/spec", json={"patch": {"outupt_format": "csv"}})
    assert response.status_code == 422


def test_answering_posts_a_real_message_and_advances_the_task(client):
    client.post("/simulate/ambiguous_report_request")
    task = next(t for t in client.get("/tasks").json() if t["state"] == "needs_clarification")
    assert task["open_question"]

    response = client.post(f"/tasks/{task['id']}/answer", json={"text": "as a csv please"})
    assert response.status_code == 200
    assert response.json()["state"] == "awaiting_approval"
    assert response.json()["spec"]["output_format"] == "csv"

    texts = [m["text"] for m in client.get("/conversations/conv-report/messages").json()]
    assert "as a csv please" in texts


def test_a_blank_answer_is_a_422(client):
    client.post("/simulate/ambiguous_report_request")
    task = next(t for t in client.get("/tasks").json() if t["state"] == "needs_clarification")
    assert client.post(f"/tasks/{task['id']}/answer", json={"text": "   "}).status_code == 422


def test_actions_on_an_unknown_task_are_404(client):
    assert client.post("/tasks/nope/approve").status_code == 404
    assert client.post("/tasks/nope/mode", json={"mode": "auto"}).status_code == 404


def test_a_reply_to_an_unknown_task_is_a_404_not_a_500(client):
    """I1: reply_to_task_id is on the public MessageIn schema, so any client can
    trigger a bare KeyError out of the orchestrator. It must surface as 404."""
    resp = client.post(
        "/messages", json={"text": "hello", "reply_to_task_id": "nope"}
    )
    assert resp.status_code == 404


def test_a_reply_naming_a_task_from_another_conversation_is_a_409(client):
    """I2: replying with a task id from a different conversation must be
    refused, not silently attached to a task it does not belong to."""
    task_a = _parked_task(client)  # lives in conv-1

    resp = client.post(
        "/messages",
        json={
            "conversation_id": "conv-2",
            "text": "actually make it csv",
            "reply_to_task_id": task_a["id"],
        },
    )
    assert resp.status_code == 409
    # The foreign message never joined the task it tried to answer.
    assert client.get(f"/tasks/{task_a['id']}").json()["source_message_ids"] == (
        task_a["source_message_ids"]
    )


def test_editing_the_spec_of_a_finished_task_is_a_409(client):
    task = _parked_task(client)
    client.post(f"/tasks/{task['id']}/approve")
    resp = client.patch(
        f"/tasks/{task['id']}/spec", json={"patch": {"output_format": "csv"}}
    )
    assert resp.status_code == 409


def test_setting_the_mode_of_a_finished_task_is_a_409(client):
    task = _parked_task(client)
    client.post(f"/tasks/{task['id']}/approve")
    resp = client.post(f"/tasks/{task['id']}/mode", json={"mode": "auto"})
    assert resp.status_code == 409


def test_a_task_asking_a_question_can_be_rejected(client):
    """M3: a task stuck in needs_clarification must be killable, not stuck."""
    client.post("/simulate/ambiguous_report_request")
    task = next(t for t in client.get("/tasks").json() if t["state"] == "needs_clarification")
    resp = client.post(f"/tasks/{task['id']}/reject", json={"reason": "cannot answer"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "failed"
    assert resp.json()["failure_reason"] == "cannot answer"
