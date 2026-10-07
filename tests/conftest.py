import pytest
from fastapi.testclient import TestClient

from intrallm_sandbox.api import create_app
from intrallm_sandbox.config import Settings

ADMIN = "test-admin-token"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        runtime="local",
        db_path=str(tmp_path / "db.sqlite"),
        local_root=str(tmp_path / "sandboxes"),
        admin_token=ADMIN,
        background=False,
        max_per_owner=2,
        max_total=3,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {ADMIN}"
        yield c


@pytest.fixture
def make_token(client):
    def _make(principal, role):
        r = client.post("/api/v1/tokens", json={"principal": principal, "role": role})
        assert r.status_code == 201, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    return _make
