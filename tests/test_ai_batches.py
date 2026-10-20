"""AI窓口の段階3の追加: 答えの形(JSON)と一括処理(半額)。"""
import json
from types import SimpleNamespace

import pytest

from factory import ai_gateway

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "string", "enum": ["x", "y"]}},
          "required": ["a", "b"], "additionalProperties": False}


def test_mock_answers_in_the_schema_shape(conn):
    r = ai_gateway.ask(conn, "design", "q", schema=SCHEMA)
    assert r.data == {"a": 1, "b": "x"}


def test_mock_responder_is_used(conn, monkeypatch):
    monkeypatch.setattr(ai_gateway, "mock_responder", lambda stage, prompt, schema: '```json\n{"a": 5, "b": "y"}\n```')
    assert ai_gateway.ask(conn, "design", "q", schema=SCHEMA).data == {"a": 5, "b": "y"}


def test_broken_json_is_an_ai_error(conn, monkeypatch):
    monkeypatch.setattr(ai_gateway, "mock_responder", lambda *a: "ごめんなさい")
    with pytest.raises(ai_gateway.AIError):
        ai_gateway.ask(conn, "design", "q", schema=SCHEMA)


def test_local_batch_is_collected_the_same_way(conn):
    items = [ai_gateway.BatchItem(key=f"k{i}", prompt=f"p{i}", meta={"i": i}, idea_id="I-00001") for i in range(3)]
    batch_id = ai_gateway.submit_batch(conn, "explore", "test", items, schema=SCHEMA)
    assert batch_id.startswith("local-")
    outs = ai_gateway.collect_batches(conn, "test")
    assert sorted(o.meta["i"] for o in outs) == [0, 1, 2] and all(o.result.data["a"] == 1 for o in outs)
    rows = conn.execute("SELECT * FROM ai_costs").fetchall()
    assert len(rows) == 3 and all(r["batch"] == 1 and r["idea_id"] == "I-00001" for r in rows)
    assert ai_gateway.collect_batches(conn, "test") == []    # 2回は受け取らない


def test_batch_refuses_bad_keys_and_secrets(conn, monkeypatch):
    with pytest.raises(ValueError):
        ai_gateway.submit_batch(conn, "explore", "t", [ai_gateway.BatchItem(key="0x 1", prompt="p")])
    monkeypatch.setenv("SOME_API_KEY", "sk-abcdefghijklmnop")
    with pytest.raises(ai_gateway.SecretLeak):
        ai_gateway.submit_batch(conn, "explore", "t", [ai_gateway.BatchItem(key="a", prompt="sk-abcdefghijklmnop")])
    assert conn.execute("SELECT COUNT(*) FROM ai_batches").fetchone()[0] == 0


class FakeBatches:
    """anthropic の messages.batches の代わり。"""

    def __init__(self):
        self.created = None
        self.status = "in_progress"

    def create(self, requests):
        self.created = list(requests)
        return SimpleNamespace(id="msgbatch_1")

    def retrieve(self, batch_id):
        return SimpleNamespace(processing_status=self.status)

    def results(self, batch_id):
        def ok(custom_id, text):
            msg = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
                                  usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0))
            return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=msg))
        return [ok("a", json.dumps({"a": 2, "b": "y"})),
                SimpleNamespace(custom_id="b", result=SimpleNamespace(type="errored"))]


def test_api_batch_is_half_price_and_waits_until_ended(conn, monkeypatch):
    batches = FakeBatches()
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    monkeypatch.setenv("FACTORY_AI_BACKEND", "api")
    monkeypatch.setattr(ai_gateway, "_client", lambda: client)
    items = [ai_gateway.BatchItem(key="a", prompt="p", meta={"w": 1}), ai_gateway.BatchItem(key="b", prompt="q")]
    assert ai_gateway.submit_batch(conn, "explore", "t", items, schema=SCHEMA) == "msgbatch_1"
    params = batches.created[0]["params"]
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"]["format"]["schema"] == SCHEMA and params["output_config"]["effort"] == "low"
    assert ai_gateway.collect_batches(conn, "t") == []          # まだ処理中
    batches.status = "ended"
    outs = {o.key: o for o in ai_gateway.collect_batches(conn, "t")}
    assert outs["a"].result.data == {"a": 2, "b": "y"} and outs["a"].meta == {"w": 1}
    assert outs["b"].result is None and "errored" in outs["b"].error
    cost = conn.execute("SELECT cost_usd, batch FROM ai_costs").fetchone()
    assert cost["batch"] == 1 and cost["cost_usd"] == pytest.approx(0.05)    # Haiku 入力100万 0.10ドルの半額
