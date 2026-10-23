"""価格の保管(market.py)・入る条件の数え方(patterns.py)・読み取りの重さの制限。"""
import pytest

from factory.botbase import History
from factory.engine import simulate
from factory.hyperliquid.reader import Funding, Reader, TooManyFills, now_ms
from factory.market import Market
from factory.patterns import count_episodes, flags
from tests.fakes import DAY, HOUR, trader_fills
from tests.helpers import candles, flat, write_bot


def test_candles_are_kept_and_only_missing_bars_are_fetched(conn, exchange, reader):
    m = Market(conn, reader)
    now = now_ms()
    first = m.candles("BTC", "1h", now - 3 * DAY, now)
    assert len(first) >= 71 and all(c.t + HOUR <= now for c in first)    # 閉じた足だけ
    calls = exchange.count("candleSnapshot")
    again = Market(conn, reader).candles("BTC", "1h", now - 3 * DAY, now)
    assert again == first and exchange.count("candleSnapshot") == calls  # 帳簿から読んだ
    older = Market(conn, reader).candles("BTC", "1h", now - 5 * DAY, now)
    assert len(older) >= 119 and exchange.count("candleSnapshot") == calls + 1   # 足りない頭だけ取った


def test_unknown_symbol_is_not_fetched_twice_in_one_run(conn, exchange, reader):
    m = Market(conn, reader)
    now = now_ms()
    assert m.candles("NOPE", "1h", now - DAY, now) == []
    m.candles("NOPE", "1h", now - DAY, now)
    assert exchange.count("candleSnapshot") == 1


def test_funding_is_cached(conn, exchange, reader):
    m = Market(conn, reader)
    now = now_ms()
    f = m.funding("BTC", now - 2 * DAY, now)
    assert len(f) >= 47
    Market(conn, reader).funding("BTC", now - 2 * DAY, now)
    assert exchange.count("fundingHistory") == 1


def test_a_hole_in_the_middle_is_filled(conn, exchange, reader):
    """短い期間だけ取った日が離れてあっても、長い期間を読む時に途中の抜けを埋める
    (入口2が直近24時間だけ取り、数日後に調査が30日分を読むと、抜けた分だけ機会不足に見えていた)。"""
    now = now_ms()
    stale = Market(conn, reader, clock=lambda: now - 5 * DAY)
    stale.funding("ETH", now - 6 * DAY, now - 5 * DAY)                   # 5日前に、その時の直近24時間だけ取った
    stale.candles("ETH", "1h", now - 6 * DAY, now - 5 * DAY)
    m = Market(conn, reader)
    m.funding("ETH", now - DAY, now)                                     # 今日、直近24時間だけ取った
    m.candles("ETH", "1h", now - DAY, now)
    rates = Market(conn, reader).funding("ETH", now - 30 * DAY, now)
    assert len(rates) >= 30 * 24 - 1 and all(b.t - a.t == HOUR for a, b in zip(rates, rates[1:]))
    bars = Market(conn, reader).candles("ETH", "1h", now - 30 * DAY, now)
    assert len(bars) >= 30 * 24 - 2 and all(b.t - a.t == HOUR for a, b in zip(bars, bars[1:]))
    # 埋めたあとは、もう取りに行かない
    calls = exchange.count("fundingHistory"), exchange.count("candleSnapshot")
    Market(conn, reader).funding("ETH", now - 30 * DAY, now)
    Market(conn, reader).candles("ETH", "1h", now - 30 * DAY, now)
    assert (exchange.count("fundingHistory"), exchange.count("candleSnapshot")) == calls


def test_a_hole_the_exchange_cannot_fill_is_tried_once_per_run(conn, exchange, reader):
    now = now_ms()
    m = Market(conn, reader)
    m.funding("ETH", now - 3 * DAY, now)
    conn.execute("DELETE FROM funding WHERE symbol = 'ETH' AND t BETWEEN ? AND ?", (now - 2 * DAY, now - DAY))
    exchange.fail.add("fundingHistory")             # 取引所に断られて埋まらない
    calls = exchange.count("fundingHistory")
    m2 = Market(conn, reader)
    for _ in range(3):
        try:
            m2.funding("ETH", now - 3 * DAY, now)
        except Exception as e:  # noqa: BLE001 - ReaderError を確かめる
            assert type(e).__name__ == "ReaderError"
    assert exchange.count("fundingHistory") == calls + 1


def test_breakout_episodes_are_counted_once_per_run():
    rows = [(100, 101, 99, 100)] * 5 + [(100, 103, 100, 102.5), (102.5, 104, 102, 103.5)] + \
           [(103, 103, 101, 101)] * 5 + [(101, 106, 101, 105)]
    cs = candles(rows)
    fl = flags("高値抜け", "long", 5, 0, cs)
    assert fl[5] and fl[6] and fl[12] and not fl[7]
    assert count_episodes("高値抜け", "long", 5, 0, cs) == 2
    assert count_episodes("高値抜け", "long", 5, 0, cs, since=cs[8].t) == 1
    assert count_episodes("分類できない", "long", 5, 0, cs) is None


def test_match_key_vocabulary_is_seven_words():
    from factory.patterns import KINDS

    assert list(KINDS) == ["高値抜け", "安値割れ", "平均回帰", "急落", "急騰", "出来高急増", "資金調達料"]
    assert "その他" not in KINDS


def test_mean_reversion_depends_on_side():
    cs = candles([(100, 100, 100, 100)] * 4 + [(100, 100, 95, 95)])
    assert flags("平均回帰", "long", 5, 3, cs)[-1] is True
    assert flags("平均回帰", "short", 5, 3, cs)[-1] is False


def test_funding_kind_uses_rates_known_at_the_close():
    cs = flat(8, step=HOUR)
    funding = [Funding(cs[0].t + HOUR * i, -0.0002 if i == 3 else 0.00001) for i in range(8)]
    fl = flags("資金調達料", "long", 1, 0.01, cs, funding, step=HOUR)
    assert fl.index(True) == 2      # 3本目が閉じた時刻に −0.02% が決まる


def test_history_shows_funding_up_to_the_close():
    cs = flat(4, step=HOUR)
    funding = [Funding(cs[0].t + HOUR * i, i / 10000) for i in range(6)]
    h = History(cs, 2, funding, [f.t for f in funding], HOUR)
    assert h.funding_rates(10) == [0.0, 0.0001, 0.0002]


def test_simulate_does_not_trade_before_trade_from(tmp_path):
    bot = __import__("factory.botbase", fromlist=["load_bot"]).load_bot(
        write_bot(tmp_path, 'def entry_signal(history):\n    return "long"\n'))
    cs = flat(40)
    sim = simulate(bot, cs, [], amount_usd=200, fee_rate=0, trade_from=cs[20].t)
    assert sim.start_t == cs[20].t
    assert sim.signal_times and min(sim.signal_times) >= cs[20].t
    assert all(t.entry_t >= cs[20].t for t in sim.trades)


def test_throttle_waits_when_the_minute_is_full(exchange):
    clock = {"t": 0.0}
    waits = []

    def sleep(s):
        waits.append(s)
        clock["t"] += s

    r = Reader(client=exchange.client(), weight_per_minute=50, clock=lambda: clock["t"], sleep=sleep)
    r.size_decimals()
    r.size_decimals()
    assert waits == []
    r.size_decimals()       # 3回目で 60 > 50 になるので1分待つ
    assert len(waits) == 1 and waits[0] == pytest.approx(60.05)


def test_too_many_fills_stops_at_the_page_limit(exchange, reader):
    start = now_ms() - 30 * DAY
    exchange.fills = {"0xw": trader_fills("BTC", start, 1500, gap=HOUR // 3)}
    with pytest.raises(TooManyFills):
        reader.user_fills("0xw", start, now_ms(), max_pages=1)
    assert len(reader.user_fills("0xw", start, now_ms(), max_pages=2)) == 3000
    assert all(f.start_position > 0 for f in reader.user_fills("0xw", start, now_ms()) if f.side == "A")
