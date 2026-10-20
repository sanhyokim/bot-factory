"""実装(SPEC 5.4)・修理(5.7)・毎日のpaper評価と反証(5.5・5.6)。"""
from factory import config, db
from factory.hyperliquid.reader import Candle, now_ms
from factory.market import Market
from factory.models import Idea
from factory.stages import implement, paper
from tests.fakes import BREAKOUT_CODE, RULE, HOUR

BROKEN = "def entry_signal(history):\n    return history[-1].close > 0\n"
NEVER = "def entry_signal(history):\n    return None\n"


def ready_idea(conn, **rule):
    idea = db.insert_idea(conn, Idea(summary="BTCの高値抜け", symbol="BTC", timeframe="1h", entry_kind="高値抜け",
                                     side="long", take_profit_pct=1.5, stop_loss_pct=0.8))
    r = {**RULE, **rule}
    conn.execute("""INSERT INTO rules(id, symbol, timeframe, entry_condition, exit_condition, max_amount_usd,
                        max_trades_per_day, take_profit_pct, stop_loss_pct) VALUES (?,?,?,?,?,200,?,?,?)""",
                 (idea, r["symbol"], r["timeframe"], r["entry_condition"], r["exit_condition"],
                  r["max_trades_per_day"], r["take_profit_pct"], r["stop_loss_pct"]))
    db.set_idea_state(conn, idea, "新ロジック開発", "完了")
    return idea


def bot(conn):
    return conn.execute("SELECT * FROM bots").fetchone()


def test_implement_starts_paper_from_the_next_bar(conn, fake_ai, reader):
    idea = ready_idea(conn)
    rep = implement.run(conn, Market(conn, reader))
    b = bot(conn)
    assert rep.started == ["B-0001"] and b["status"] == "paper評価中" and b["attempts"] == 0
    assert b["paper_start_ms"] % HOUR == 0 and 0 < b["paper_start_ms"] - now_ms() <= HOUR
    folder = config.bots_dir() / "B-0001"
    assert (folder / "strategy.py").read_text(encoding="utf-8") == BREAKOUT_CODE
    assert "max_trades_per_day: 4" in (folder / "bot.yaml").read_text(encoding="utf-8")
    assert db.get_idea(conn, idea)["stage"] == "paper評価"


def test_failing_code_is_repaired(conn, fake_ai, reader):
    ready_idea(conn)
    fake_ai.code = BROKEN
    implement.run(conn, Market(conn, reader))
    b = bot(conn)
    assert b["status"] == "paper評価中" and b["attempts"] == 1 and fake_ai.calls.count("repair") == 1


def test_three_repairs_then_rejected(conn, fake_ai, reader):
    idea = ready_idea(conn)
    fake_ai.code = NEVER
    fake_ai.repair_answers = [f"```python\n{NEVER}```"] * 3
    implement.run(conn, Market(conn, reader))
    b = bot(conn)
    assert b["status"] == "否決" and b["attempts"] == 3 and b["last_error"].startswith("実装不良")
    m = conn.execute("SELECT * FROM memos WHERE idea_id = ?", (idea,)).fetchone()
    assert m["tag"] == "実装不良" and m["stage"] == "修理" and "修理3回" in m["reason"]


def test_repair_can_refuse_to_change_the_rule(conn, fake_ai, reader):
    idea = ready_idea(conn)
    fake_ai.code = NEVER
    fake_ai.repair_answers = ["直せない: 入る条件の数字を変えないと成立しない"]
    implement.run(conn, Market(conn, reader))
    assert bot(conn)["attempts"] == 1
    m = conn.execute("SELECT * FROM memos WHERE idea_id = ?", (idea,)).fetchone()
    assert m["tag"] == "実装不良" and "入る条件の数字" in m["reason"]


def test_forbidden_code_goes_to_repair(conn, fake_ai, reader):
    ready_idea(conn)
    fake_ai.code = "import os\n\ndef entry_signal(history):\n    return None\n"
    implement.run(conn, Market(conn, reader))
    assert bot(conn)["status"] == "paper評価中" and bot(conn)["attempts"] == 1


# --- 毎日のpaper評価: 価格を帳簿に直接入れて、取引所には行かずに判定させる ---

ALWAYS_LONG = 'def entry_signal(history):\n    return "long"\n'


def started_bot(conn, tmp_path, code, *, days=15):
    idea = ready_idea(conn, max_trades_per_day=10, take_profit_pct=1.0, stop_loss_pct=0.5)
    folder = config.bots_dir() / "B-0001"
    folder.mkdir(parents=True)
    implement.write_yaml(folder, conn.execute("SELECT * FROM rules").fetchone())
    (folder / "strategy.py").write_text(code, encoding="utf-8")
    now = now_ms()
    start = now - now % HOUR - days * 24 * HOUR
    conn.execute("""INSERT INTO bots(id, rule_id, code_path, status, created_at, updated_at, paper_start_ms)
                    VALUES ('B-0001', ?, ?, 'paper評価中', 'x', 'x', ?)""", (idea, str(folder), start))
    db.set_idea_state(conn, idea, "paper評価", "継続")
    return idea, start


def fill_prices(conn, start, step_pct):
    """start の10本前から今まで、毎時 step_pct% ずつ動く足を帳簿に入れる。"""
    now = now_ms()
    t, p, rows = start - 10 * HOUR, 100.0, []
    while t + HOUR <= now:
        c = p * (1 + step_pct / 100)
        rows.append(Candle(t, p, max(p, c) * 1.001, min(p, c) * 0.999, c, 1.0))
        t, p = t + HOUR, c
    conn.executemany("INSERT INTO candles VALUES ('BTC', '1h', ?, ?, ?, ?, ?, ?)",
                     [(c.t, c.o, c.h, c.l, c.c, c.v) for c in rows])
    conn.executemany("INSERT INTO funding VALUES ('BTC', ?, 0.0)", [(c.t,) for c in rows])
    conn.commit()


def test_paper_pass_goes_through_refute_to_gate_one(conn, fake_ai, reader, tmp_path):
    idea, start = started_bot(conn, tmp_path, ALWAYS_LONG)
    fill_prices(conn, start, 1.2)
    rep = paper.run_bots(conn, Market(conn, reader))
    b = bot(conn)
    assert rep.refute_passed == ["B-0001"], rep
    assert b["status"] == "本番待ち" and "前後半検査○" in b["refute_note"] and "手数料が倍でも黒字" in b["refute_note"]
    assert db.get_idea(conn, idea)["stage"] == "反証" and db.get_idea(conn, idea)["verdict"] == "完了"
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE kind = 'paper'").fetchone()[0] >= 30
    assert "refute" in fake_ai.calls


def test_paper_failure_leaves_a_memo(conn, fake_ai, reader, tmp_path):
    idea, start = started_bot(conn, tmp_path, ALWAYS_LONG, days=3)
    fill_prices(conn, start, -0.6)
    rep = paper.run_bots(conn, Market(conn, reader))
    assert rep.failed == ["B-0001"] and bot(conn)["status"] == "否決"
    m = conn.execute("SELECT * FROM memos WHERE idea_id = ?", (idea,)).fetchone()
    assert m["stage"] == "paper評価" and m["tag"] == "連敗"


def test_paper_keeps_running_and_replaces_trades(conn, fake_ai, reader, tmp_path):
    idea, start = started_bot(conn, tmp_path, ALWAYS_LONG, days=2)
    fill_prices(conn, start, 1.2)
    paper.run_bots(conn, Market(conn, reader))
    n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    paper.run_bots(conn, Market(conn, reader))
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == n > 0
    assert bot(conn)["status"] == "paper評価中" and db.get_idea(conn, idea)["verdict"] == "継続"


def test_exception_during_paper_is_repaired(conn, fake_ai, reader, tmp_path):
    crash = 'def entry_signal(history):\n    if len(history) > 30:\n        return 1 / 0\n    return None\n'
    started_bot(conn, tmp_path, crash, days=2)
    fill_prices(conn, conn.execute("SELECT paper_start_ms FROM bots").fetchone()[0], 0.1)
    rep = paper.run_bots(conn, Market(conn, reader))
    assert rep.repaired == ["B-0001"] and bot(conn)["attempts"] == 1
    assert "ZeroDivisionError" in bot(conn)["last_error"]


def test_missing_prices_hold_the_bot(conn, fake_ai, exchange, reader, tmp_path):
    exchange.fail.update({"candleSnapshot", "fundingHistory", "meta"})
    idea, _ = started_bot(conn, tmp_path, ALWAYS_LONG, days=2)
    rep = paper.run_bots(conn, Market(conn, reader))
    assert rep.held == ["B-0001"] and db.get_idea(conn, idea)["verdict"] == "保留"
    assert bot(conn)["status"] == "paper評価中"
