import pytest

from factory import ai_gateway, db
from factory.hyperliquid.reader import Reader

from tests.fakes import FakeAI, FakeExchange


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "factory.db"
    monkeypatch.setenv("FACTORY_DB", str(path))
    monkeypatch.setenv("FACTORY_AI_BACKEND", "mock")
    monkeypatch.setenv("FACTORY_BOTS_DIR", str(tmp_path / "bots"))
    c = db.connect(path)
    yield c
    c.close()


@pytest.fixture
def fake_ai(monkeypatch):
    ai = FakeAI()
    monkeypatch.setattr(ai_gateway, "mock_responder", ai)
    return ai


@pytest.fixture
def exchange():
    return FakeExchange()


@pytest.fixture
def reader(exchange):
    return Reader(client=exchange.client(), retries=1, weight_per_minute=0)
