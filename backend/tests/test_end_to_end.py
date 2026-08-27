from pathlib import Path

from ley_khaa.domain.states import TaskState


def test_a_messy_conversation_parks_for_a_human_and_the_dial_releases_it(client):
    """Headline #2: the autonomy dial changes what the system does, not just what it says."""
    client.post("/simulate/messy_universe_check")

    tasks = client.get("/tasks").json()
    assert len(tasks) == 1, "the noisy conversation should yield exactly one task"
    task = tasks[0]

    # It stopped on its own and can explain why.
    assert task["state"] == TaskState.AWAITING_APPROVAL.value
    assert task["spec"]["operation"] == "set_difference"
    assert task["spec"]["output_format"] == "xlsx"
    assert task["recommended_mode"] != "auto"
    assert "→" in task["autonomy_reason"]

    # One click on the dial, and the same task runs without further approval.
    released = client.post(f"/tasks/{task['id']}/mode", json={"mode": "auto"}).json()
    assert released["state"] == TaskState.DONE.value
    assert released["mode_override"] == "auto"
    # Phase 3: "done" now means a file exists, not that a stub walked the states.
    assert Path(released["workspace_path"], "deliverable", "output.xlsx").is_file()


def test_a_gap_is_asked_about_answered_in_the_conversation_and_closed(client):
    """The clarification loop, over the same message path a Slack reply will take."""
    client.post("/simulate/ambiguous_report_request")

    task = next(
        t for t in client.get("/tasks").json()
        if t["state"] == TaskState.NEEDS_CLARIFICATION.value
    )
    assert task["spec"]["missing_fields"] == ["output_format"]
    assert "output_format" in task["open_question"]

    answered = client.post(f"/tasks/{task['id']}/answer", json={"text": "as a csv please"}).json()
    assert answered["state"] == TaskState.AWAITING_APPROVAL.value
    assert answered["spec"]["output_format"] == "csv"
    assert answered["open_question"] is None

    # The answer is a real message in the conversation, not a private side channel.
    texts = [m["text"] for m in client.get("/conversations/conv-report/messages").json()]
    assert "as a csv please" in texts

    # And no duplicate candidate was formed for a request that already had a task.
    keys = {c["candidate_key"] for c in client.get("/candidates").json()}
    assert len(keys) == len(client.get("/candidates").json())

    approved = client.post(f"/tasks/{task['id']}/approve").json()
    assert approved["state"] == TaskState.DONE.value
    assert Path(approved["workspace_path"], "deliverable", "output.csv").is_file()
