"""価格の受け取り口は銘柄ごとに1本(SPEC 3.1)。paper評価の全BOTがそれを読み、BOTごとに取引所へ行かない。"""
import pytest

from factory import config, db
from factory.hyperliquid import feed
from factory.hyperliquid.feed import Feeds
from factory.hyperliquid.reader import now_ms
from factory.market import Market
from factory.models import Idea
from factory.stages import implement, paper
from tests.fakes import HOUR

FUNDING_CODE = '''def entry_signal(history):
    rates = history.funding_rates(1)
    if rates and rates[-1] >= 0.0001:
        return "short"
    return None


def exit_signal(history, position):
    return False
'''
BREAKOUT = 'def entry_signal(history):\n    return "long" if history[-1].c > history[-2].c else None\n'


def add_bot(conn, bot_id, symbol, *, type_="値動き", code=BREAKOUT, days=3):
    idea = db.insert_idea(conn, Idea(summary=f"{symbol}の案", symbol=symbol, timeframe="1h", entry_kind="高値抜け",
                                     type=type_, source="funding" if type_ == "歪み" else "wallet"))
    distortion = type_ == "歪み"
    conn.execute(
        """INSERT INTO rules(id, symbol, timeframe, entry_condition, exit_condition, max_amount_usd, max_trades_per_day,
               take_profit_pct, stop_loss_pct, type, max_hold_hours, funding_x, funding_y, continuity, competition)
           VALUES (?,?,'1h','入る','出る',200,3,?,2.0,?,6,?,?,'常にある','競争あり')""",
        (idea, symbol, None if distortion else 1.0, type_, 0.01 if distortion else None,
         0.002 if distortion else None))
    rule = conn.execute("SELECT * FROM rules WHERE id = ?", (idea,)).fetchone()
    folder = config.bots_dir() / bot_id
    version = implement.copy_template(folder)
    implement.write_yaml(folder, rule)
    (folder / "strategy.py").write_text(code, encoding="utf-8")
    now = now_ms()
    conn.execute("""INSERT INTO bots(id, rule_id, code_path, status, created_at, updated_at, paper_start_ms, base_version)
                    VALUES (?, ?, ?, 'paper評価中', 'x', 'x', ?, ?)""",
                 (bot_id, idea, str(folder), now - now % HOUR - days * 24 * HOUR, version))
    db.set_idea_state(conn, idea, "paper評価", "継続")
    conn.commit()
    return idea


def spy_market(monkeypatch):
    calls = []
    candles, funding = Market.candles, Market.funding

    def c(self, symbol, timeframe, start, end=None):
        calls.append(("価格", symbol, timeframe))
        return candles(self, symbol, timeframe, start, end)

    def f(self, symbol, start, end=None):
        calls.append(("資金調達料", symbol))
        return funding(self, symbol, start, end)

    monkeypatch.setattr(Market, "candles", c)
    monkeypatch.setattr(Market, "funding", f)
    return calls


def test_paper_reads_one_feed_per_symbol(conn, fake_ai, exchange, reader, monkeypatch):
    for i, symbol in enumerate(["BTC", "BTC", "ETH", "BTC"], start=1):
        add_bot(conn, f"B-{i:04d}", symbol, days=2 + i)       # 始めた日がBOTごとに違う
    calls = spy_market(monkeypatch)
    rep = paper.run_bots(conn, Market(conn, reader))
    assert len(rep.running) + len(rep.failed) == 4, rep
    assert sorted(calls) == sorted([("価格", "BTC", "1h"), ("資金調達料", "BTC"),
                                    ("価格", "ETH", "1h"), ("資金調達料", "ETH")])
    # 取引所へ取りに行ったのも銘柄ごと(BOTの本数ぶん増えない)
    assert {b["coin"] for t, b in exchange.calls if t == "fundingHistory"} == {"BTC", "ETH"}


def test_same_symbol_gets_the_same_feed(conn, reader):
    feeds = Feeds(Market(conn, reader))
    assert feeds.get("BTC") is feeds.get("BTC") and feeds.get("BTC") is not feeds.get("ETH")


def test_a_failed_symbol_is_not_fetched_again_in_the_same_run(conn, exchange, reader):
    exchange.fail.add("candleSnapshot")
    feeds = Feeds(Market(conn, reader))
    f = feeds.get("BTC")
    for _ in range(3):
        try:
            f.candles("1h", now_ms() - 10 * HOUR)
        except Exception as e:  # noqa: BLE001 - ReaderError を確かめる
            assert type(e).__name__ == "ReaderError"
    assert exchange.count("candleSnapshot") == 1


def test_feed_process_refreshes_symbols_of_running_bots(conn, exchange, reader, monkeypatch):
    add_bot(conn, "B-0001", "BTC")
    add_bot(conn, "B-0002", "BTC")
    calls = spy_market(monkeypatch)
    feeds = feed.refresh_once(conn, Market(conn, reader), warmup_bars=10, log=lambda s: None)
    assert feeds.symbols() == ["BTC"] and sorted(calls) == [("価格", "BTC", "1h"), ("資金調達料", "BTC")]
    assert conn.execute("SELECT COUNT(*) FROM candles WHERE symbol = 'BTC'").fetchone()[0] > 0


def test_distortion_bot_in_paper_records_received_funding_and_slippage(conn, fake_ai, exchange, reader):
    exchange.funding_fn["ETH"] = lambda t: 0.0003
    add_bot(conn, "B-0001", "ETH", type_="歪み", code=FUNDING_CODE, days=2)
    rep = paper.run_bots(conn, Market(conn, reader))
    assert rep.running == ["B-0001"] or rep.failed == ["B-0001"], rep
    rows = conn.execute("SELECT * FROM trades WHERE bot_id = 'B-0001'").fetchall()
    assert rows, "取引がない"
    for t in rows:
        assert t["funding_usd"] > 0 and t["slippage_usd"] > 0          # 売りで受け取った・滑りを記録した
        gross = (t["entry_price"] - t["exit_price"]) * t["entry_qty"]       # 売りの損益(滑りは値段に含む)
        assert t["net_pnl_usd"] == pytest.approx(gross - t["fee_usd"] + t["funding_usd"])
