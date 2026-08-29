"""The default project, installed at startup.

Deliberately described as installed by STARTUP, not by migration 0005: the
migration creates the table only. The seed workflows use the same division, and
a docstring claiming otherwise is the false-statement defect 8cebd1f fixed.

Its description is empty on purpose. A described project is offered to stage-2
routing, and offering `default` there would let the model route into the very
project that exists to mean "the model did not route this".
"""
from sqlalchemy.orm import Session

from ..persistence.orm import ProjectRow
from ..persistence.project_repository import DEFAULT_PROJECT, ProjectRepository


def ensure_default_project(session: Session) -> ProjectRow:
    return ProjectRepository(session).create(DEFAULT_PROJECT, display_name="Default")
