import os

os.environ["LEY_KHAA_DISABLE_STARTUP"] = "1"

import pytest
from fastapi.testclient import TestClient

from ley_khaa.api.app import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c
