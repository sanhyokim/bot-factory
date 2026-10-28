"""探索(SPEC 5.1)。"""
import json

from factory import memo
from factory.hyperliquid.reader import Fill, now_ms
from factory.market import Market
from factory.models import Memo, match_key
from factory.stages import explore
from tests.fakes import DAY, GOOD_HYPOTHESIS, HOUR, board_row, trader_fills


def fill(t, side, sz, px, pnl=0.0, start=0.0, coin="BTC"):
    return Fill(coin, t, px, sz, side, "", pnl, 0.1, start)


def test_round_trips_follow_the_position():
    fills = [
        fill(1, "B", 1.0, 100), fill(2, "B", 1.0, 110, start=1.0),  # 2回に分けて買い(平均105)
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
    conn.execute("INSERT INTO wallet_scans(address, scanned_at, method) VALUES (?, ?, ?)",
                 ("0x" + "c" * 40, "2099-01-01T00:00:00+09:00", explore.SCAN_METHOD))
    rep = explore.scan(conn, Market(conn, reader))
    assert rep.skipped_recent == 1 and rep.checked == 2 and len(rep.passed) == 1
    assert rep.reasons == {"約定が多すぎる(高頻度の取引)": 1}


def test_collect_registers_and_excludes_by_memo_match(conn, fake_ai):
    # 照合キーは 型・銘柄・入る条件の種類・数字(SPEC 6.3)。GOOD_HYPOTHESIS は n=20・x=0
    memo.add_memo(conn, Memo(stage="調査", tag="機会不足", reason="前に落ちた", idea_summary="前の案",
                             symbol="BTC", timeframe="1h", entry_kind="高値抜け",
                             match_key=match_key("値動き", "BTC", "高値抜け", '{"n": 20, "x": 0}')))
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


def test_unclassifiable_hypothesis_is_not_registered(conn, fake_ai):
    fake_ai.hypotheses = [GOOD_HYPOTHESIS, {**GOOD_HYPOTHESIS, "entry_kind": "分類できない",
                                            "summary": "ニュースが出た直後に買う"}]
    explore.submit(conn, [explore.WalletCheck("0x" + "a" * 40, True, "", [])])
    rep = explore.collect(conn)
    assert len(rep.registered) == 1 and len(rep.unclassified) == 1
    assert conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 1
    m = conn.execute("SELECT * FROM memos").fetchone()
    assert (m["stage"], m["tag"], m["reason"]) == ("探索", "ルール不成立", "入る条件の種類を分類できず")
    assert m["idea_id"] is None and m["entry_kind"] == "" and m["idea_summary"] == "ニュースが出た直後に買う"
    # 答えに7語以外の語が来ても同じ扱い
    fake_ai.hypotheses = [{**GOOD_HYPOTHESIS, "entry_kind": "その他"}]
    explore.submit(conn, [explore.WalletCheck("0x" + "b" * 40, True, "", [])])
    assert len(explore.collect(conn).unclassified) == 1


def test_ideas_registered_as_other_are_rejected(conn):
    from factory import db
    from factory.models import Idea

    other = db.insert_idea(conn, Idea(summary="その他の案", symbol="BTC", timeframe="1h", entry_kind="その他"))
    keep = db.insert_idea(conn, Idea(summary="高値抜けの案", symbol="BTC", timeframe="1h", entry_kind="高値抜け"))
    assert explore.reject_registered_other(conn) == [other]
    assert db.get_idea(conn, other)["stage"] == "否決" and db.get_idea(conn, keep)["stage"] == "探索"
    m = conn.execute("SELECT * FROM memos WHERE idea_id = ?", (other,)).fetchone()
    assert m["tag"] == "ルール不成立" and m["reason"] == "入る条件の種類を分類できず"
    assert explore.reject_registered_other(conn) == []


# --- 1日目に順位表の300財布がすべて「取引回数が足りない・約定が多すぎる」で落ちたことへの対応 ---

def test_same_time_fills_returned_in_reverse_order_are_still_counted():
    # 1つの注文が同じ時刻に2回に分かれて約定し、取引所が逆の順で返した場合
    fills = [fill(10, "B", 1.0, 101, start=1.0), fill(10, "B", 1.0, 100, start=0.0),
             fill(20, "A", 1.0, 111, 10, start=1.0), fill(20, "A", 1.0, 110, 10, start=2.0),
             fill(30, "B", 1.0, 100, start=0.0), fill(40, "A", 1.0, 105, 5, start=1.0)]
    trips = explore.round_trips(fills)
    assert [(t.entry_t, t.exit_t) for t in trips] == [(10, 20), (30, 40)]
    assert trips[0].size == 2.0 and trips[0].entry_px == 100.5 and trips[0].exit_px == 110.5


def test_position_follows_the_exchange_after_a_missing_fill():
    fills = [fill(1, "B", 1.0, 100), fill(2, "A", 1.0, 101, 1, start=1.0),
             fill(3, "B", 2.0, 100, start=0.0),          # ここで1.0売った約定が欠けている(本当は保有1.0で残る)
             fill(5, "B", 1.0, 100, start=0.0), fill(6, "A", 1.0, 102, 2, start=1.0)]
    trips = explore.round_trips(fills)
    assert [(t.entry_t, t.exit_t) for t in trips] == [(1, 2), (5, 6)]


def test_wallets_checked_with_the_old_method_are_checked_again(conn, exchange, reader):
    start = now_ms() - 20 * DAY
    a, b = "0x" + "a" * 40, "0x" + "b" * 40
    exchange.board = [board_row(a), board_row(b)]
    exchange.fills = {a: trader_fills("BTC", start, 60), b: trader_fills("BTC", start, 60)}
    recent = "2099-01-01T00:00:00+09:00"
    conn.execute("INSERT INTO wallet_scans(address, scanned_at, method) VALUES (?, ?, 1)", (a, recent))
    conn.execute("INSERT INTO wallet_scans(address, scanned_at, method) VALUES (?, ?, ?)",
                 (b, recent, explore.SCAN_METHOD))
    rep = explore.scan(conn, Market(conn, reader))
    assert rep.skipped_recent == 1 and rep.checked == 1 and [w.address for w in rep.passed] == [a]
    assert conn.execute("SELECT method FROM wallet_scans WHERE address = ?", (a,)).fetchone()[0] == explore.SCAN_METHOD


def test_scan_reports_progress_every_50_wallets(conn, exchange, reader):
    exchange.board = [board_row(f"0x{i:040x}") for i in range(120)]
    seen = []
    explore.scan(conn, Market(conn, reader), progress=lambda i, n: seen.append((i, n)))
    assert seen == [(50, 120), (100, 120)]


def test_candidates_are_wallets_trading_at_a_copyable_size():
    whale = dict(address="0xw", account_value=50_000_000, month_pnl=9_000_000, month_roi=0.2,
                 month_volume=900_000_000)
    small = dict(address="0xs", account_value=3_000, month_pnl=800, month_roi=0.3, month_volume=200_000)
    better = dict(address="0xb", account_value=9_000, month_pnl=2_500, month_roi=0.4, month_volume=150_000)
    idle = dict(address="0xi", account_value=5_000, month_pnl=900, month_roi=0.2, month_volume=2_000)
    losing = dict(address="0xl", account_value=5_000, month_pnl=-50, month_roi=-0.01, month_volume=90_000)
    empty = dict(address="0xe", account_value=20, month_pnl=500, month_roi=25.0, month_volume=60_000)
    # 利益を引き出しながら大きく張る財布(30日の損益が今の資金より大きい)と、取引量が大きすぎる財布
    withdrew = dict(address="0xd", account_value=2_700, month_pnl=258_000, month_roi=1.0, month_volume=900_000)
    busy = dict(address="0xh", account_value=8_000, month_pnl=4_000, month_roi=1.0, month_volume=58_000_000)
    steps = []
    rows = explore.candidates([whale, small, better, idle, losing, empty, withdrew, busy], steps=steps)
    assert [r["address"] for r in rows] == ["0xb", "0xs"]       # 損益の順
    assert [k for _, k in steps] == [7, 5, 3, 2]


def test_scan_checks_up_to_300_wallets_a_day_skipping_recent_ones(conn, exchange, reader, monkeypatch):
    from factory import config

    cfg = config.load_config()
    monkeypatch.setattr(config, "load_config", lambda: {**cfg, "explore": {**cfg["explore"], "top_wallets": 3}})
    addrs = [f"0x{i:040x}" for i in range(6)]
    exchange.board = [board_row(a, month_pnl=1000 - i) for i, a in enumerate(addrs)]
    recent = "2099-01-01T00:00:00+09:00"
    for a in addrs[:2]:
        conn.execute("INSERT INTO wallet_scans(address, scanned_at, method) VALUES (?, ?, ?)",
                     (a, recent, explore.SCAN_METHOD))
    rep = explore.scan(conn, Market(conn, reader))
    assert rep.skipped_recent == 2 and rep.checked == 3       # 上位2件を飛ばし、次の3件を調べる
    done = {r[0] for r in conn.execute("SELECT address FROM wallet_scans")}
    assert done == set(addrs[:5])


def test_explore_check_counts_each_filter_without_recording(conn, exchange, reader):
    from factory import walletcheck

    start = now_ms() - 20 * DAY
    good, few, big = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40
    whale = "0x" + "d" * 40
    exchange.board = [board_row(good, month_pnl=3000), board_row(few, month_pnl=2000),
                      board_row(big, month_pnl=1000), board_row(whale, account_value=5e7, month_pnl=9e6)]
    exchange.fills = {good: trader_fills("BTC", start, 60), few: trader_fills("BTC", start, 20),
                      big: trader_fills("BTC", start, 60, notional=5000)}
    out = walletcheck.run(reader, 30)
    assert "順位表 4財布のうち候補 3財布" in out and "先頭 3財布を読む" in out
    assert "候補の条件ごとに残った数: 損益プラス 4 → 資金100〜100,000ドル 3 →" in out
    assert "全部の条件を通過 1" in out and "取引50回以上 2" in out and "中央値2000ドル以下 2" in out
    assert "取引20回(基準50回以上)" in out and "取引額の中央値" in out
    assert conn.execute("SELECT COUNT(*) FROM wallet_scans").fetchone()[0] == 0
