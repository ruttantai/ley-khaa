from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository


def test_an_empty_dead_letter_list_is_an_empty_array(client):
    response = client.get("/dead-letters")
    assert response.status_code == 200
    assert response.json() == []


def test_dead_letters_are_listed_newest_first(client, session):
    repo = DeadLetterRepository(session)
    repo.record(source="slack", kind="inbound", reason="first")
    repo.record(source="discord", kind="outbound", reason="second")

    body = client.get("/dead-letters").json()

    assert [row["reason"] for row in body] == ["second", "first"]
    assert body[0]["source"] == "discord"
    assert body[0]["kind"] == "outbound"
    assert body[0]["created_at"]


def test_the_limit_is_honoured(client, session):
    repo = DeadLetterRepository(session)
    for i in range(5):
        repo.record(source="slack", kind="inbound", reason=f"r{i}")

    assert len(client.get("/dead-letters?limit=2").json()) == 2


def test_a_nonsense_limit_is_a_422_not_a_500(client):
    assert client.get("/dead-letters?limit=0").status_code == 422
    assert client.get("/dead-letters?limit=-1").status_code == 422


def test_the_endpoint_returns_no_secrets(client, session):
    """The route serves whatever is stored, so the guarantee has to hold at the
    write. Asserted HERE as well as in test_dead_letters.py because this is the
    surface a browser reaches, and §4 says tokens are never returned by an API."""
    DeadLetterRepository(session).record(
        source="slack",
        kind="inbound",
        reason="bad envelope",
        payload={"token": "xoxb-super-secret", "event": {"text": "hi"}},
    )

    body = client.get("/dead-letters").json()

    assert "xoxb-super-secret" not in body[0]["payload"]
    assert "[redacted]" in body[0]["payload"]
