import pytest
from sqlalchemy.exc import IntegrityError

from ley_khaa.persistence.orm import CandidateRow, ProjectBindingRow, ProjectRow, TaskRow


def test_a_project_stores_the_description_stage_two_reasons_over(session):
    session.add(ProjectRow(name="acme", display_name="Acme", description="Acme's equity books"))
    session.commit()
    row = session.get(ProjectRow, "acme")
    assert row.description == "Acme's equity books"
    assert row.active is True


def test_two_client_wide_bindings_for_one_client_cannot_both_exist(session):
    """The whole point of conversation_id="" rather than NULL.

    With NULL here, SQL treats the two rows as distinct and both insert, so
    "most specific wins" would silently become "whichever row came back first".
    """
    session.add(
        ProjectBindingRow(
            id="b1", source="slack", client="acme", conversation_id="", project="acme"
        )
    )
    session.commit()
    session.add(
        ProjectBindingRow(
            id="b2", source="slack", client="acme", conversation_id="", project="other"
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_conversation_binding_and_a_client_binding_coexist(session):
    session.add(
        ProjectBindingRow(
            id="b1", source="slack", client="acme", conversation_id="", project="acme"
        )
    )
    session.add(
        ProjectBindingRow(
            id="b2", source="slack", client="acme", conversation_id="C9", project="special"
        )
    )
    session.commit()
    assert session.query(ProjectBindingRow).count() == 2


def test_a_new_task_starts_with_no_lease_and_no_attempts(session):
    row = TaskRow(id="t1", project="default", state="received", title="x", source_message_ids=[])
    session.add(row)
    session.commit()
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.lease_attempts == 0


def test_a_candidate_starts_with_no_amendment_proposal(session):
    row = CandidateRow(
        id="c1", conversation_id="C1", candidate_key="k", state="ready", message_ids=[]
    )
    session.add(row)
    session.commit()
    assert row.amends_task_id is None
    assert row.amendment_confidence is None
