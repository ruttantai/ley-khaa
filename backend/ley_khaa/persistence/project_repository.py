"""Projects and the bindings that route conversations into them (spec §3.5)."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .orm import ProjectBindingRow, ProjectRow

# Every task must land somewhere. This project always exists — startup installs
# it (projects/seeds.py) — and it is where a routing miss goes.
DEFAULT_PROJECT = "default"

# The sentinel meaning "this binding covers the whole client". Not NULL: see
# ProjectBindingRow's docstring for why a nullable column here would silently
# disable the unique constraint.
ANY_CONVERSATION = ""


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, name: str, *, display_name: str = "", description: str = "") -> ProjectRow:
        """Idempotent, so startup seeding can run on every boot.

        An existing project is returned untouched rather than overwritten: a
        boot must never quietly revert a description a human edited.
        """
        existing = self.get(name)
        if existing is not None:
            return existing
        row = ProjectRow(
            name=name,
            display_name=display_name or name,
            description=description,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def get(self, name: str) -> ProjectRow | None:
        return self.session.get(ProjectRow, name)

    def active(self) -> list[ProjectRow]:
        return list(
            self.session.scalars(
                select(ProjectRow).where(ProjectRow.active.is_(True)).order_by(ProjectRow.name)
            )
        )

    def binding_for(
        self, source: str, client: str, conversation_id: str
    ) -> ProjectBindingRow | None:
        """The most specific binding for this message, or None.

        Two lookups rather than one ordered query: expressing "most specific
        wins" as an ORDER BY on conversation_id would work only by accident of
        "" sorting before every other string, and would silently invert if the
        sentinel ever changed.
        """
        exact = self.session.scalars(
            select(ProjectBindingRow).where(
                ProjectBindingRow.source == source,
                ProjectBindingRow.client == client,
                ProjectBindingRow.conversation_id == conversation_id,
            )
        ).first()
        if exact is not None:
            return exact
        return self.session.scalars(
            select(ProjectBindingRow).where(
                ProjectBindingRow.source == source,
                ProjectBindingRow.client == client,
                ProjectBindingRow.conversation_id == ANY_CONVERSATION,
            )
        ).first()

    def bind(
        self,
        source: str,
        client: str,
        conversation_id: str,
        project: str,
        *,
        stage: str,
    ) -> ProjectBindingRow:
        """Point a scope at a project, moving an existing binding if there is one.

        Idempotent by scope, not by project: the learning rule can fire twice
        for the same conversation when two workers race it, and the second call
        must not hit the unique constraint.
        """
        existing = self.session.scalars(
            select(ProjectBindingRow).where(
                ProjectBindingRow.source == source,
                ProjectBindingRow.client == client,
                ProjectBindingRow.conversation_id == conversation_id,
            )
        ).first()
        if existing is not None:
            existing.project = project
            existing.created_by_stage = stage
            self.session.commit()
            self.session.refresh(existing)
            return existing

        row = ProjectBindingRow(
            id=str(uuid.uuid4()),
            source=source,
            client=client,
            conversation_id=conversation_id,
            project=project,
            created_by_stage=stage,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def bindings_for_project(self, project: str) -> list[ProjectBindingRow]:
        return list(
            self.session.scalars(
                select(ProjectBindingRow).where(ProjectBindingRow.project == project)
            )
        )
