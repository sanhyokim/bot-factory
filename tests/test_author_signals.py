"""著者の記事(2026-10-21〜24)から足した3つ: AIの失敗の記録・調査で見送った率・否決メモが使われているか。"""
import json

import pytest
from fastapi.testclient import TestClient

from factory import ai_gateway, db, memo
from factory.dashboard.app import app
from factory.market import Market
from factory.models import Idea, Memo
from factory.stages import design, investigate

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


def failures(conn):
    return conn.execute("SELECT * FROM ai_failures ORDER BY id").fetchall()


def test_ai_failure_is_recorded_and_still_raised(conn, monkeypatch):
    def broken(*a):
        raise ai_gateway.AIError("AIに接続できない。インターネット接続を確認する")

    monkeypatch.setattr(ai_gateway, "_call_mock", broken)
    with pytest.raises(ai_gateway.AIError):
        ai_gateway.ask(conn, "design", "q", idea_id="I-00001")
    rows = failures(conn)
    assert len(rows) == 1 and rows[0]["stage"] == "design" and rows[0]["model"] == "sonnet"
    assert rows[0]["idea_id"] == "I-00001" and "接続できない" in rows[0]["reason"]
    assert conn.execute("SELECT COUNT(*) FROM ai_costs").fetchone()[0] == 0


def test_unreadable_answer_is_a_failure_but_costs_are_kept(conn, monkeypatch):
    monkeypatch.setattr(ai_gateway, "mock_responder", lambda *a: "ごめんなさい")
    with pytest.raises(ai_gateway.AIError):
        ai_gateway.ask(conn, "design", "q", schema=SCHEMA)
    assert len(failures(conn)) == 1
    assert conn.execute("SELECT COUNT(*) FROM ai_costs").fetchone()[0] == 1   # 答えは返ったので費用はつく


def test_budget_and_secret_stops_are_not_failures(conn, monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xdeadbeefdeadbeef")
    with pytest.raises(ai_gateway.SecretLeak):
        ai_gateway.ask(conn, "manual", "鍵は 0xdeadbeefdeadbeef")
    assert failures(conn) == []


def test_failed_batch_items_are_recorded(conn, monkeypatch):
    calls = iter(["{\"a\": 1}", "読めない答え"])
    monkeypatch.setattr(ai_gateway, "mock_responder", lambda *a: next(calls))
    items = [ai_gateway.BatchItem(key=f"k{i}", prompt="p", idea_id=f"I-0000{i}") for i in range(2)]
    ai_gateway.submit_batch(conn, "explore", "test", items, schema=SCHEMA)
    outs = ai_gateway.collect_batches(conn, "test")
    assert sum(1 for o in outs if o.error) == 1
    rows = failures(conn)
    assert len(rows) == 1 and rows[0]["idea_id"] == "I-00001" and rows[0]["stage"] == "explore"


def add(conn, **kw):
    fields = dict(summary="BTCの高値抜けで買い", symbol="BTC", timeframe="1h", entry_kind="高値抜け", side="long",
                  entry_params=json.dumps({"n": 20, "x": 0}), take_profit_pct=1.5, stop_loss_pct=0.8)
    fields.update(kw)
    return db.insert_idea(conn, Idea(**fields))


def test_weekly_skip_rate_leaves_out_unsupported(conn, fake_ai, reader):
    add(conn, take_profit_pct=0.41)          # 手数料負け
    add(conn, take_profit_pct=0.43)          # 継続
    add(conn, take_profit_pct=0.43)          # 継続
    add(conn, symbol="NOPE")                 # 未対応(数えない)
    investigate.run(conn, Market(conn, reader))
    week = memo.weekly_metrics(conn, 1)[0]
    assert (week["skipped"], week["investigated"]) == (1, 3)
    assert week["skip_rate"] == pytest.approx(1 / 3)
    assert memo.weekly_metrics(conn, 1, type_="歪み")[0]["skip_rate"] is None


def test_notes_given_to_design_are_counted(conn, fake_ai):
    used = memo.add_memo(conn, Memo(stage="paper評価", tag="連敗", reason="8連敗した", idea_summary="BTCの別の案",
                                    symbol="BTC", source="wallet"))
    memo.add_memo(conn, Memo(stage="paper評価", tag="連敗", reason="r", idea_summary="ETHの案", symbol="ETH",
                             source="wallet"))
    memo.add_memo(conn, Memo(stage="探索", tag="重複", reason="r", idea_summary="除外の記録", symbol="SOL"))
    assert memo.unused_memos(conn) == {"total": 2, "unused": 2}
    idea = add(conn)
    db.set_idea_state(conn, idea, "調査", "継続")
    design.run(conn)
    uses = conn.execute("SELECT * FROM memo_uses").fetchall()
    assert [(u["memo_id"], u["idea_id"]) for u in uses] == [(used, idea)]
    assert memo.weekly_metrics(conn, 1)[0]["notes_passed"] == 1
    assert memo.unused_memos(conn) == {"total": 2, "unused": 1}


def test_dashboard_shows_the_three(conn, monkeypatch):
    monkeypatch.setattr(ai_gateway, "mock_responder", lambda *a: "ごめんなさい")
    with pytest.raises(ai_gateway.AIError):
        ai_gateway.ask(conn, "design", "q", schema=SCHEMA)
    memo.add_memo(conn, Memo(stage="paper評価", tag="連敗", reason="r", idea_summary="x", symbol="BTC"))
    html = TestClient(app).get("/").text
    assert "AIが答えを返せなかった回数" in html and "今日 <b>1</b>回" in html
    assert "新ロジック開発</td><td>sonnet</td><td>1</td>" in html
    assert "<th>調査で見送り</th><th>渡したメモ</th>" in html
    assert "一度もAIに渡されていないメモ: 1件 / 1件" in html
