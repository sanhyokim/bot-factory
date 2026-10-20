import pytest

from factory import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "factory.db"
    monkeypatch.setenv("FACTORY_DB", str(path))
    monkeypatch.setenv("FACTORY_AI_BACKEND", "mock")
    c = db.connect(path)
    yield c
    c.close()
