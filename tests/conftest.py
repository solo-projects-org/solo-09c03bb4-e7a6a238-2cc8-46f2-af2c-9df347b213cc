"""Test bootstrap: keep the module-level app from creating ./data/lights.db."""

import os
import tempfile

import pytest

os.environ.setdefault(
    "LIGHTS_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="lights-tests-"), "collect.db")
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("LIGHTS_DB_PATH", str(db_path))
    app = create_app(Settings(db_path=str(db_path)))
    with TestClient(app) as c:
        yield c
