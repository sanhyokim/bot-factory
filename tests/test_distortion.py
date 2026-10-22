"""歪み型(資金調達料)の流れ: 探索の入口2(SPEC 5.1)・調査(5.2)・新ロジック開発(5.3)・実装(5.4)・資金の上限(9章)。"""
import json
import math
from datetime import timedelta

import pytest

from factory import config, db, memo
from factory.market import Market
from factory.models import Idea, now_jst
from factory.stages import design, explore_funding, implement, investigate
from tests.fakes import HOUR


def funding_idea(conn, symbol="ETH", **params):
    p = {"x": 0.01, **params}
    return db.insert_idea(conn, Idea(summary=f"{symbol}の資金調達料が極端", symbol=symbol, timeframe="1h",
                                     entry_kind="資金調達料", side="short", source="funding", type="歪み",
                                     entry_params=json.dumps(p)))


def state(conn, idea_id):
    r = db.get_idea(conn, idea_id)
    return r["stage"], r["verdict"]


def memo_of(conn, idea_id):
    return conn.execute("SELECT * FROM memos WHERE idea_id = ?", (idea_id,)).fetchone()


# --- 探索の入口2 ---

def test_entry_two_finds_liquid_symbols_with_extreme_funding(conn, exchange, reader):
    exchange.coins["SOL"] = (150.0, 2)
    exchange.funding_fn["BTC"] = lambda t: -0.0002 if (t // HOUR) % 6 == 0 else 0.0   # 24時間で4時間
    exchange.funding_fn["SOL"] = lambda t: 0.0005                                      # ずっと極端だが薄い
    exchange.volumes["SOL"] = 400_000
    rep = explore_funding.run(conn, Market(conn, reader))
    assert (rep.listed, rep.liquid) == (3, 2)
    assert [c.symbol for c in rep.candidates] == ["BTC"] and rep.candidates[0].side == "long"
    idea = db.get_idea(conn, rep.registered[0])
    assert (idea["type"], idea["source"], idea["entry_kind"], idea["timeframe"]) == ("歪み", "funding", "資金調達料", "1h")
    assert idea["summary"] == "BTCの資金調達料が極端(直近24時間の最大 -0.0200%/時)。受け取る側で入り、戻ったら出る"
    assert json.loads(idea["entry_params"]) == {"x": 0.01} and "買いで持てば" in idea["hypothesis"]
    # 3日間は同じ銘柄を作り直さない(取引所にも取りに行かない)
    before = exchange.count("fundingHistory")
    again = explore_funding.run(conn, Market(conn, reader))
    assert again.skipped_recent == ["BTC"] and again.registered == [] and exchange.count("fundingHistory") == before


def test_entry_two_uses_the_tightened_hours(conn, exchange, reader):
    exchange.funding_fn["BTC"] = lambda t: 0.0002 if (t // HOUR) % 6 == 0 else 0.0
    conn.execute("INSERT INTO tuning_log(created_at, top_tag, setting, value, note, type) VALUES (?,?,?,?,?,?)",
                 (now_jst().isoformat(), "機会不足", "min_hours", 6, "test", "歪み"))
    rep = explore_funding.find(conn, Market(conn, reader))
    assert rep.min_hours == 6 and rep.candidates == []


def test_entry_two_matches_only_distortion_memos(conn, exchange, reader):
    exchange.funding_fn["BTC"] = lambda t: 0.0003
    exchange.funding_fn["ETH"] = lambda t: 0.0003
    exchange.volumes["ETH"] = 0               # 1回目は出来高で外す
    key_params = json.dumps({"x": 0.01})
    price_idea = db.insert_idea(conn, Idea(summary="値動き型", symbol="BTC", timeframe="1h", entry_kind="資金調達料",
                                           entry_params=key_params))
    memo.memo_for_idea(conn, price_idea, "調査", "機会不足", "値動き型のメモ")
    assert len(explore_funding.run(conn, Market(conn, reader)).registered) == 1      # 型が違うので照合しない
    old = funding_idea(conn, "ETH")
    memo.memo_for_idea(conn, old, "調査", "機会不足", "歪み型のメモ")
    conn.execute("UPDATE ideas SET created_at = ? WHERE id = ?", ((now_jst() - timedelta(days=5)).isoformat(), old))
    exchange.volumes["ETH"] = 5_000_000
    rep = explore_funding.run(conn, Market(conn, reader))
    assert rep.registered == [] and len(rep.excluded) == 1


# --- 調査(歪み型) ---

def test_distortion_investigation_passes_and_keeps_the_30_day_max(conn, fake_ai, exchange, reader):
    exchange.funding_fn["ETH"] = lambda t: 0.0002 + 0.0001 * math.sin(t / (7 * HOUR))
    idea = funding_idea(conn)
    rep = investigate.run(conn, Market(conn, reader))
    assert rep.passed == [idea] and state(conn, idea) == ("調査", "継続")
    params = json.loads(db.get_idea(conn, idea)["entry_params"])
    assert params["max30"] == pytest.approx(0.03, abs=1e-4) and params["hours30"] >= 700


def test_distortion_investigation_rejects_rare_and_fee_losing(conn, fake_ai, exchange, reader):
    exchange.coins["SOL"] = (150.0, 2)
    exchange.funding_fn["ETH"] = lambda t: 0.00005                                     # 0.005% はしきい値の下
    exchange.funding_fn["SOL"] = lambda t: 0.00011 * (-1) ** (t // HOUR)                # 毎時向きが変わる
    rare, flip = funding_idea(conn, "ETH"), funding_idea(conn, "SOL")
    investigate.run(conn, Market(conn, reader))
    assert memo_of(conn, rare)["tag"] == "機会不足" and "0時間、基準30時間未満" in memo_of(conn, rare)["reason"]
    m = memo_of(conn, flip)
    assert m["tag"] == "手数料負け" and m["type"] == "歪み" and "往復0.14%×" in m["reason"]


def test_distortion_investigation_checks_capital(conn, fake_ai, exchange, reader):
    exchange.coins["BIG"] = (300_000.0, 0)
    exchange.funding_fn["BIG"] = lambda t: 0.0003
    idea = funding_idea(conn, "BIG")
    investigate.run(conn, Market(conn, reader))
    assert memo_of(conn, idea)["tag"] == "資金不足"


def test_distortion_uses_its_own_tuning(conn, fake_ai, exchange, reader):
    exchange.funding_fn["ETH"] = lambda t: 0.00012 if (t // HOUR) % 2 else 0.0           # 1時間おき・1回ずつ
    conn.execute("INSERT INTO tuning_log(created_at, top_tag, setting, value, note, type) VALUES (?,?,?,?,?,?)",
                 (now_jst().isoformat(), "手数料負け", "funding_fee_multiple", 3, "test", "歪み"))
    idea = funding_idea(conn)
    investigate.run(conn, Market(conn, reader))
    # 1回あたり 0.012% の受け取りに対し、往復0.14% の3倍 → 手数料負け
    assert memo_of(conn, idea)["tag"] == "手数料負け" and "の3倍" in memo_of(conn, idea)["reason"]


# --- 新ロジック開発(歪み型・資金の上限) ---

def test_distortion_design_sets_x_from_the_30_day_max(conn, fake_ai):
    idea = funding_idea(conn, max30=0.03, hours30=720)
    db.set_idea_state(conn, idea, "調査", "継続")
    fake_ai.distortion_rule["symbol"] = "ETH"
    rep = design.run(conn)
    assert rep.done == [idea]
    rule = conn.execute("SELECT * FROM rules WHERE id = ?", (idea,)).fetchone()
    assert (rule["type"], rule["timeframe"], rule["take_profit_pct"]) == ("歪み", "1h", None)
    assert rule["funding_x"] == pytest.approx(0.015) and rule["funding_y"] == pytest.approx(0.0005)
    assert rule["max_hold_hours"] == 24 and rule["stop_loss_pct"] == 2.0
    assert (rule["continuity"], rule["competition"]) == ("常にある", "競争なし")
    assert "+0.015% 以上なら売りで入る" in rule["entry_condition"] and "最長24時間" in rule["exit_condition"]


def test_distortion_design_rejects_y_not_below_x(conn, fake_ai):
    idea = funding_idea(conn, max30=0.03)
    db.set_idea_state(conn, idea, "調査", "継続")
    fake_ai.distortion_rule = {**fake_ai.distortion_rule, "symbol": "ETH", "funding_y_pct": 0.02}
    rep = design.run(conn)
    assert rep.rejected == [idea] and fake_ai.calls.count("design") == 2
    assert "x(0.015%)より小さく" in memo_of(conn, idea)["reason"]


def test_price_design_needs_max_hold_and_tags():
    from tests.fakes import RULE
    assert any("保有の上限時間" in p for p in design.problems({**RULE, "max_hold_hours": None}, "BTC"))
    assert any("継続性の札" in p for p in design.problems({**RULE, "continuity": "たまに"}, "BTC"))
    assert any("競争の札" in p for p in design.problems({k: v for k, v in RULE.items() if k != "competition"}, "BTC"))


def test_rule_over_the_capital_cap_is_rejected_before_asking_ai(conn, fake_ai, exchange, reader):
    exchange.coins["BIG"] = (300_000.0, 0)
    idea = db.insert_idea(conn, Idea(summary="BIGの高値抜け", symbol="BIG", timeframe="1h", entry_kind="高値抜け",
                                     side="long", entry_params='{"n": 20, "x": 0}', take_profit_pct=1.5))
    db.set_idea_state(conn, idea, "調査", "継続")
    rep = design.run(conn, Market(conn, reader))
    assert rep.rejected == [idea] and "design" not in fake_ai.calls
    m = memo_of(conn, idea)
    assert (m["stage"], m["tag"]) == ("新ロジック開発", "資金不足") and "ドルが1本の上限200ドルを超える" in m["reason"]


def test_capital_needed_is_saved_with_the_rule(conn, fake_ai, reader):
    idea = db.insert_idea(conn, Idea(summary="BTCの高値抜け", symbol="BTC", timeframe="1h", entry_kind="高値抜け",
                                     side="long", entry_params='{"n": 20, "x": 0}', take_profit_pct=1.5))
    db.set_idea_state(conn, idea, "調査", "継続")
    design.run(conn, Market(conn, reader))
    assert conn.execute("SELECT min_capital_usd FROM rules").fetchone()[0] == 10       # 最小注文10ドル


# --- 実装 ---

def test_rules_from_before_3b_go_back_to_design(conn, fake_ai, reader):
    idea = db.insert_idea(conn, Idea(summary="古いルール", symbol="BTC", timeframe="1h", entry_kind="高値抜け"))
    conn.execute("""INSERT INTO rules(id, symbol, timeframe, entry_condition, exit_condition, max_amount_usd,
                        max_trades_per_day, take_profit_pct, stop_loss_pct) VALUES (?,?,?,?,?,200,4,1.5,0.8)""",
                 (idea, "BTC", "1h", "高値抜けで買い", "+1.5%で確定"))
    db.set_idea_state(conn, idea, "新ロジック開発", "完了")
    rep = implement.run(conn, Market(conn, reader))
    assert rep.started == [] and state(conn, idea) == ("新ロジック開発", "保留")
    assert conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0] == 0
    design.run(conn)                     # 新しい定型で作り直す
    rule = conn.execute("SELECT * FROM rules WHERE id = ?", (idea,)).fetchone()
    assert rule["max_hold_hours"] == 12 and state(conn, idea) == ("新ロジック開発", "完了")


def test_distortion_ideas_are_not_starved_by_the_price_backlog():
    rows = [{"id": f"I-{i:05d}", "type": "値動き"} for i in range(1, 9)] + [{"id": "I-00009", "type": "歪み"},
                                                                          {"id": "I-00010", "type": "歪み"}]
    picked = [r["id"] for r in db.interleave_types(rows, 4)]
    assert picked == ["I-00009", "I-00001", "I-00010", "I-00002"]


def test_implement_copies_the_base_and_records_tags(conn, fake_ai, reader):
    from tests.test_implement_paper import ready_idea

    ready_idea(conn)
    implement.run(conn, Market(conn, reader))
    b = conn.execute("SELECT * FROM bots").fetchone()
    folder = config.bots_dir() / b["id"]
    assert (folder / "base.py").read_text(encoding="utf-8") == (implement.TEMPLATE_DIR / "base.py").read_text(
        encoding="utf-8")
    text = (folder / "bot.yaml").read_text(encoding="utf-8")
    assert "type: 値動き" in text and "max_hold_hours: 12.0" in text and "leverage: 1" in text
    assert (b["base_version"], b["made_by"], b["continuity"], b["competition"]) == (1, "sonnet", "常にある", "競争あり")


def test_refute_doubles_fee_and_slippage():
    from factory.stages import refute
    from tests.helpers import DAY, T0, trade

    # 手数料だけを倍にすると黒字(30 × (0.3 − 0.1) > 0)、滑りも倍にすると赤字(30 × (0.3 − 0.1 − 0.25) < 0)
    trades = [trade(T0 + (i + 1) * HOUR * 10, 0.3, fee=0.1, slippage=0.25) for i in range(30)]
    c = refute.refute(trades, T0, T0 + 13 * DAY).checks[1]
    assert not c.ok and c.tag == "手数料2倍で赤字" and "手数料と滑りを2倍" in c.detail


def test_recent_symbols_window(conn):
    funding_idea(conn, "ETH")
    conn.execute("UPDATE ideas SET created_at = ?", ((now_jst() - timedelta(days=4)).isoformat(),))
    funding_idea(conn, "SOL")
    assert explore_funding.recent_symbols(conn, 3) == {"sol"}
