"""調査(SPEC 5.2)・週1回の棚卸し(6.3 用途3)・新ロジック開発(5.3)。"""
import json
from datetime import timedelta

from factory import db, memo, tuning
from factory.market import Market
from factory.models import Idea, Memo, now_jst
from factory.stages import design, investigate


def add(conn, **kw):
    fields = dict(summary="BTCの高値抜けで買い", symbol="BTC", timeframe="1h", entry_kind="高値抜け", side="long",
                  entry_params=json.dumps({"n": 20, "x": 0}), take_profit_pct=1.5, stop_loss_pct=0.8)
    fields.update(kw)
    return db.insert_idea(conn, Idea(**fields))


def state(conn, idea_id):
    r = db.get_idea(conn, idea_id)
    return r["stage"], r["verdict"]


def memo_of(conn, idea_id):
    return conn.execute("SELECT * FROM memos WHERE idea_id = ?", (idea_id,)).fetchone()


def test_fee_rule_uses_three_times_fee_plus_slippage(conn, fake_ai, reader):
    low = add(conn, take_profit_pct=0.41)      # 3 × (0.09 + 0.05) = 0.42
    ok = add(conn, take_profit_pct=0.43)
    rep = investigate.run(conn, Market(conn, reader))
    assert memo_of(conn, low)["tag"] == "手数料負け" and "0.42%" in memo_of(conn, low)["reason"]
    assert state(conn, ok) == ("調査", "継続") and rep.passed == [ok]


def test_too_few_occasions_and_unknown_symbols(conn, fake_ai, reader):
    rare = add(conn, entry_kind="急落", side="long", entry_params=json.dumps({"n": 1, "x": 30}))
    unknown = add(conn, symbol="NOPE")
    no_numbers = db.insert_idea(conn, Idea(summary="手で書いた案", symbol="BTC", timeframe="1時間", entry_kind="平均回帰"))
    other = add(conn, entry_kind="その他")
    investigate.run(conn, Market(conn, reader))
    assert memo_of(conn, rare)["tag"] == "機会不足" and "30日で0回" in memo_of(conn, rare)["reason"]
    assert memo_of(conn, unknown)["tag"] == "未対応"
    assert memo_of(conn, no_numbers)["tag"] == "未対応"
    assert state(conn, other) == ("調査", "継続")       # 数えられない種類は2番を飛ばす


def test_capital_check(conn, fake_ai, exchange, reader):
    exchange.coins["BIG"] = (500.0, 0)       # 1枚500ドル・小数なし → 200ドルでは買えない
    big = add(conn, symbol="BIG")
    investigate.run(conn, Market(conn, reader))
    assert memo_of(conn, big)["tag"] == "資金不足"


def test_data_failure_holds_then_gives_up_after_three(conn, fake_ai, exchange, reader):
    exchange.fail.add("meta")
    idea = add(conn)
    for _ in range(2):
        investigate.run(conn, Market(conn, reader))
        assert state(conn, idea) == ("調査", "保留")
    investigate.run(conn, Market(conn, reader))
    assert memo_of(conn, idea)["tag"] == "未対応" and "3回" in memo_of(conn, idea)["reason"]


def test_reasons_are_rewritten_by_ai_in_a_batch(conn, fake_ai, reader):
    low = add(conn, take_profit_pct=0.2)
    investigate.run(conn, Market(conn, reader))
    assert "0.42%" in memo_of(conn, low)["reason"]
    assert investigate.collect_reasons(conn) == 1
    assert memo_of(conn, low)["reason"] == "値幅が小さく、手数料と滑りで利益が消えるため見送り"
    assert conn.execute("SELECT stage FROM ai_costs").fetchone()["stage"] == "investigate"


def test_weekly_stocktake_tightens_and_reverts(conn):
    def memos(tag, n, at):
        for i in range(n):
            memo.add_memo(conn, Memo(stage="調査", tag=tag, reason="r", idea_summary="s"), at=at)

    week1 = now_jst() - timedelta(days=14)
    memos("重複", 9, week1)            # 重複は数えない
    memos("手数料負け", 3, week1)
    note = tuning.weekly(conn, now=week1 + timedelta(hours=1))
    assert "手数料負け" in note and tuning.current(conn)["fee_multiple"] == 4
    assert tuning.weekly(conn, now=week1 + timedelta(hours=2)) is None     # 同じ週は1回
    week2 = week1 + timedelta(days=7)
    memos("機会不足", 2, week2)
    tuning.weekly(conn, now=week2 + timedelta(hours=1))
    assert tuning.current(conn) == {"fee_multiple": 3, "min_occurrences": 15}
    week3 = week2 + timedelta(days=7)
    memos("連敗", 5, week3)
    note = tuning.weekly(conn, now=week3 + timedelta(hours=1))
    assert "元に戻した" in note and tuning.current(conn) == {"fee_multiple": 3, "min_occurrences": 10}


def test_tightened_fee_multiple_is_used(conn, fake_ai, reader):
    conn.execute("INSERT INTO tuning_log(created_at, top_tag, setting, value, note) VALUES (?,?,?,?,?)",
                 (now_jst().isoformat(), "手数料負け", "fee_multiple", 4, "test"))
    idea = add(conn, take_profit_pct=0.5)     # 4 × 0.14 = 0.56
    investigate.run(conn, Market(conn, reader))
    assert memo_of(conn, idea)["tag"] == "手数料負け"


def passed_idea(conn):
    idea = add(conn)
    db.set_idea_state(conn, idea, "調査", "継続")
    return idea


def test_design_saves_one_rule_with_notes_from_memos(conn, fake_ai, monkeypatch):
    memo.add_memo(conn, Memo(stage="paper評価", tag="連敗", reason="8連敗した", idea_summary="BTCの別の案",
                             symbol="BTC", source="wallet"))
    idea = passed_idea(conn)
    prompts = []
    original = fake_ai.__call__

    def spy(stage, prompt, schema):
        prompts.append(prompt)
        return original(stage, prompt, schema)

    from factory import ai_gateway
    monkeypatch.setattr(ai_gateway, "mock_responder", spy)
    rep = design.run(conn)
    assert rep.done == [idea] and state(conn, idea) == ("新ロジック開発", "完了")
    rule = conn.execute("SELECT * FROM rules WHERE id = ?", (idea,)).fetchone()
    assert rule["max_amount_usd"] == 200 and rule["leverage"] == 1 and rule["max_positions"] == 1
    assert rule["take_profit_pct"] == 1.5 and rule["max_trades_per_day"] == 4
    assert "8連敗した" in prompts[0]


def test_design_retries_once_then_rejects(conn, fake_ai):
    idea = passed_idea(conn)
    fake_ai.rule = {**fake_ai.rule, "entry_condition": "いい感じの所で買い"}
    rep = design.run(conn)
    assert rep.rejected == [idea] and fake_ai.calls.count("design") == 2
    m = memo_of(conn, idea)
    assert m["tag"] == "ルール不成立" and m["stage"] == "新ロジック開発" and "入る条件に数字がない" in m["reason"]


def test_rule_problems():
    from tests.fakes import RULE
    assert design.problems(RULE, "BTC") == []
    assert "銘柄が案と違う" in design.problems({**RULE, "symbol": "ETH"}, "BTC")
    assert "損切りの%が範囲外" in design.problems({**RULE, "stop_loss_pct": 0}, "BTC")
    assert any("1日の最大取引回数" in p for p in design.problems({**RULE, "max_trades_per_day": 0}, "BTC"))
    assert "入る条件に買い・売りの向きがない" in design.problems({**RULE, "entry_condition": "20本の高値を抜けた"}, "BTC")


def test_symbol_is_matched_to_the_exchange_spelling(conn, fake_ai, exchange, reader):
    exchange.coins["kPEPE"] = (0.01, 0)
    idea = add(conn, symbol="KPEPE", take_profit_pct=0.5)
    investigate.run(conn, Market(conn, reader))
    assert db.get_idea(conn, idea)["symbol"] == "kPEPE"
