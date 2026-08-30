from ley_khaa.persistence.project_repository import DEFAULT_PROJECT, ProjectRepository


def test_a_conversation_binding_beats_a_client_wide_one(session):
    """Most-specific-wins, stated as a test rather than trusted to row order."""
    repo = ProjectRepository(session)
    repo.create("acme")
    repo.create("special")
    repo.bind("slack", "acme", "", "acme", stage="manual")
    repo.bind("slack", "acme", "C9", "special", stage="manual")

    assert repo.binding_for("slack", "acme", "C9").project == "special"
    assert repo.binding_for("slack", "acme", "C1").project == "acme"


def test_no_binding_at_all_is_a_miss_not_a_default(session):
    """The repository reports absence; deciding what absence MEANS is the
    router's job. Returning DEFAULT_PROJECT here would hide stage-2 misses."""
    repo = ProjectRepository(session)
    assert repo.binding_for("slack", "nobody", "C1") is None


def test_binding_is_idempotent_and_rebinds_rather_than_duplicating(session):
    """The learning rule can fire twice for one conversation if two workers
    race it. The second call must move the binding, not raise on the unique
    constraint and not leave two rows."""
    repo = ProjectRepository(session)
    repo.create("acme")
    repo.create("other")
    repo.bind("slack", "acme", "C9", "acme", stage="model")
    repo.bind("slack", "acme", "C9", "other", stage="model")

    assert repo.binding_for("slack", "acme", "C9").project == "other"
    assert len(repo.bindings_for_project("other")) == 1
    assert repo.bindings_for_project("acme") == []


def test_active_excludes_deactivated_projects(session):
    repo = ProjectRepository(session)
    repo.create("acme")
    inactive = repo.create("old")
    inactive.active = False
    session.commit()
    assert [p.name for p in repo.active()] == ["acme"]


def test_create_is_idempotent_so_startup_seeding_can_repeat(session):
    repo = ProjectRepository(session)
    first = repo.create(DEFAULT_PROJECT, display_name="Default")
    second = repo.create(DEFAULT_PROJECT, display_name="Ignored")
    assert first.name == second.name
    assert second.display_name == "Default"
    assert len(repo.active()) == 1
