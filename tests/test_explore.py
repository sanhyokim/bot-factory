"""探索(SPEC 5.1)。"""
import json

from factory import memo
from factory.hyperliquid.reader import Fill, now_ms
from factory.market import Market
from factory.models import Memo
from factory.stages import explore
from tests.fakes import DAY, GOOD_HYPOTHESIS, HOUR, board_row, trader_fills


def fill(t, side, sz, px, pnl=0.0, start=0.0, coin="BTC"):
    return Fill(coin, t, px, sz, side, "", pnl, 0.1, start)


def test_round_trips_follow_the_position():
    fills = [
        fill(1, "B", 1.0, 100), fill(2, "B", 1.0, 110),            # 2回に分けて買い(平均105)
        fill(3, "A", 0.5, 120, 7.5, 2.0), fill(4, "A", 1.5, 130, 37.5, 1.5),   # 2回に分けて売り切り
        fill(5, "A", 1.0, 100, 0, 0.0), fill(6, "B", 3.0, 90, 10, -1.0),       # 売り→買い越しで向きが変わる
        fill(7, "A", 2.0, 95, 10, 2.0),
    ]
    trips = explore.round_trips(fills)
    assert [(t.side, t.entry_t, t.exit_t) for t in trips] == [("long", 1, 4), ("short", 5, 6), ("long", 6, 7)]
    first = trips[0]
    assert first.entry_px == 105 and first.exit_px == 127.5 and first.size == 2.0 and first.pnl == 45
    assert trips[2].size == 2.0 and trips[2].entry_px == 90


def test_position_held_before_the_window_is_not_counted():
    fills = [fill(1, "A", 1.0, 100, 5, 1.0), fill(2, "B", 1.0, 100), fill(3, "A", 1.0, 101, 1, 1.0)]
    trips = explore.round_trips(fills)
    assert len(trips) == 1 and trips[0].entry_t == 2


def to_fills(rows):
    return [Fill(r["coin"], r["time"], float(r["px"]), float(r["sz"]), r["side"], r["dir"], float(r["closedPnl"]),
                 float(r["fee"]), float(r["startPosition"])) for r in rows]


def test_wallet_filters_follow_spec():
    start = now_ms() - 20 * DAY
    good = explore.check_wallet("0xg", to_fills(trader_fills("BTC", start, 60)), 10_000)
    assert good.ok and len(good.trips) == 60 and good.median_notional < 2000
    few = explore.check_wallet("0xf", to_fills(trader_fills("BTC", start, 49)), 10_000)
    assert not few.ok and "取引49回" in few.reason
    big = explore.check_wallet("0xb", to_fills(trader_fills("BTC", start, 60, notional=5000)), 1_000_000)
    assert not big.ok and "中央値" in big.reason
    losing = explore.check_wallet("0xl", to_fills(trader_fills("BTC", start, 60, win_every=1)), 10_000)
    assert not losing.ok and "純利益" in losing.reason
    deep = explore.check_wallet("0xd", to_fills(trader_fills("BTC", start, 60, notional=1900)), 10)
    assert not deep.ok and "落ち込み" in deep.reason


def test_prompt_has_the_last_100_trades_in_jst():
    start = now_ms() - 25 * DAY
    trips = explore.round_trips(to_fills(trader_fills("BTC", start, 120, gap=4 * HOUR)))
    prompt = explore.wallet_prompt("0xg", trips)
    assert "直近100件" in prompt and "\n100 BTC 買い" in prompt and "\n101 " not in prompt
    assert "仮説を3つ" in prompt and "300字以内" in prompt


def test_to_idea_keeps_numbers_and_drops_unreadable():
    idea = explore.to_idea(GOOD_HYPOTHESIS, "0xg")
    assert idea.symbol == "BTC" and idea.timeframe == "1h" and idea.entry_kind == "高値抜け"
    assert json.loads(idea.entry_params) == {"n": 20, "x": 0.0} and idea.take_profit_pct == 1.5
    assert explore.to_idea({**GOOD_HYPOTHESIS, "timeframe": "2時間"}, "0xg") is None
    assert explore.to_idea({**GOOD_HYPOTHESIS, "take_profit_pct": "たくさん"}, "0xg") is None


def test_scan_skips_recent_and_high_frequency_wallets(conn, exchange, reader):
    start = now_ms() - 20 * DAY
    exchange.board = [board_row("0x" + c * 40) for c in "abc"]
    exchange.fills = {"0x" + "a" * 40: trader_fills("BTC", start, 60),
                      "0x" + "b" * 40: trader_fills("BTC", start, 3500, gap=HOUR // 8),
                      "0x" + "c" * 40: trader_fills("BTC", start, 60)}
    conn.execute("INSERT INTO wallet_scans VALUES (?, ?)", ("0x" + "c" * 40, "2099-01-01T00:00:00+09:00"))
    rep = explore.scan(conn, Market(conn, reader))
    assert rep.skipped_recent == 1 and rep.checked == 2 and len(rep.passed) == 1
    assert rep.reasons == {"約定が多すぎる(高頻度の取引)": 1}


def test_collect_registers_and_excludes_by_memo_match(conn, fake_ai):
    memo.add_memo(conn, Memo(stage="調査", tag="機会不足", reason="前に落ちた", idea_summary="前の案",
                             symbol="BTC", timeframe="1h", entry_kind="高値抜け"))
    w = explore.WalletCheck("0x" + "a" * 40, True, "", [])
    batch_id, n = explore.submit(conn, [w])
    assert batch_id and n == 1
    rep = explore.collect(conn)
    assert len(rep.registered) == 2 and len(rep.excluded) == 1
    tags = [r["tag"] for r in conn.execute("SELECT tag FROM memos ORDER BY id")]
    assert tags == ["機会不足", "重複"]


def test_daily_input_cap_stops_sending(conn, fake_ai, monkeypatch):
    from factory import config

    cfg = dict(config.load_config())
    cfg["explore"] = {**cfg["explore"], "daily_input_chars": 1000}
    monkeypatch.setattr(config, "load_config", lambda *a: cfg)
    wallets = [explore.WalletCheck("0x" + c * 40, True, "", []) for c in "ab"]
    _, n = explore.submit(conn, wallets)
    assert n == 1
    _, n2 = explore.submit(conn, wallets)
    assert n2 == 0
