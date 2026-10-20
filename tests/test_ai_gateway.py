import pytest

from factory import ai_gateway


def test_ask_logs_one_cost_row(conn):
    r = ai_gateway.ask(conn, "manual", "こんにちは", idea_id="I-00001")
    assert r.model == "haiku"
    rows = conn.execute("SELECT * FROM ai_costs").fetchall()
    assert len(rows) == 1 and rows[0]["stage"] == "manual" and rows[0]["idea_id"] == "I-00001"


def test_price_uses_config():
    assert ai_gateway.price("haiku", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert ai_gateway.price("sonnet", 1_000_000, 0, batch=True) == pytest.approx(1.00)


def test_budget_stops_calls(conn):
    conn.execute("""INSERT INTO ai_costs(created_at, stage, model, backend, input_tokens, output_tokens, cost_usd)
                    VALUES (?, 'explore', 'haiku', 'api', 0, 0, 3.5)""",
                 (ai_gateway.now_jst().isoformat(timespec="seconds"),))
    with pytest.raises(ai_gateway.BudgetExceeded):
        ai_gateway.ask(conn, "explore", "q")


def test_secret_is_never_sent(conn, monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xdeadbeefdeadbeef")
    with pytest.raises(ai_gateway.SecretLeak):
        ai_gateway.ask(conn, "manual", "鍵は 0xdeadbeefdeadbeef です")
    assert conn.execute("SELECT COUNT(*) FROM ai_costs").fetchone()[0] == 0


def test_missing_api_key_gives_short_message(conn, monkeypatch, capsys):
    from factory.__main__ import main

    monkeypatch.setenv("FACTORY_AI_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr("factory.config.load_env", lambda: None)
    assert main(["ask", "1+1は?"]) == 1
    err = capsys.readouterr().err
    assert "APIキーが設定されていない" in err and "Traceback" not in err
    assert conn.execute("SELECT COUNT(*) FROM ai_costs").fetchone()[0] == 0
