import os

os.environ["LEY_KHAA_DISABLE_STARTUP"] = "1"

import pytest
from fastapi.testclient import TestClient

from ley_khaa.api.app import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


import pytest as _pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ley_khaa.db import Base
from ley_khaa.persistence import orm  # noqa: F401 — register TaskRow


@_pytest.fixture
def session():
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(test_engine)
    TestingSession = sessionmaker(
        bind=test_engine, autoflush=False, expire_on_commit=False, future=True
    )
    s = TestingSession()
    try:
        yield s
    finally:
        s.close()
