def test_post_message_creates_done_task(client):
    resp = client.post("/messages", json={"text": "Reconcile the holdings list"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "done"
    assert body["title"] == "Reconcile the holdings list"


def test_list_tasks_returns_created(client):
    client.post("/messages", json={"text": "task one"})
    client.post("/messages", json={"text": "task two"})
    resp = client.get("/tasks")
    assert resp.status_code == 200
    titles = {t["title"] for t in resp.json()}
    assert {"task one", "task two"} <= titles


def test_get_task_by_id(client):
    created = client.post("/messages", json={"text": "fetch me"}).json()
    resp = client.get(f"/tasks/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_get_missing_task_404(client):
    assert client.get("/tasks/nope").status_code == 404
