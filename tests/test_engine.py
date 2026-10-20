import pytest

from factory.botbase import load_bot
from factory.engine import jst_date, simulate
from factory.hyperliquid.reader import Funding
from tests.helpers import STEP, T0, candles, flat, write_bot

ALWAYS_LONG = "def entry_signal(h):\n    return 'long'"
FIRST_BAR_LONG = "def entry_signal(h):\n    return 'long' if len(h) == 1 else None"


def run(bot, cs, funding=(), **kw):
    opts = {"amount_usd": 200, "fee_rate": 0.0005, "size_decimals": 5, "min_order_usd": 10}
    opts.update(kw)
    return simulate(bot, cs, list(funding), **opts)


def test_take_profit_with_fees(tmp_path):
    bot = load_bot(write_bot(tmp_path, FIRST_BAR_LONG))
    cs = candles([(100, 100, 100, 100), (100, 100.5, 99.8, 100.2), (100.2, 101.5, 100, 101)])
    r = run(bot, cs)
    t = r.trades[0]
    assert (t.entry_price, t.exit_price, t.reason, t.qty) == (100, 101, "利益確定", 2.0)
    assert t.fee_usd == pytest.approx(0.0005 * 2 * (100 + 101))
    assert t.net_usd == pytest.approx(2.0 - t.fee_usd)
    assert t.notional <= 200


def test_stop_loss_wins_when_both_hit(tmp_path):
    bot = load_bot(write_bot(tmp_path, FIRST_BAR_LONG))
    cs = candles([(100, 100, 100, 100), (100, 100, 100, 100), (100, 102, 99, 100)])
    t = run(bot, cs).trades[0]
    assert t.reason == "損切り" and t.exit_price == pytest.approx(99.5)


def test_gap_through_stop_fills_at_open(tmp_path):
    bot = load_bot(write_bot(tmp_path, FIRST_BAR_LONG))
    cs = candles([(100, 100, 100, 100), (100, 100, 100, 100), (98, 98, 97, 97)])
    assert run(bot, cs).trades[0].exit_price == 98


def test_short_side(tmp_path):
    bot = load_bot(write_bot(tmp_path, "def entry_signal(h):\n    return 'short' if len(h) == 1 else None"))
    cs = candles([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 98.9, 99)])
    t = run(bot, cs, fee_rate=0).trades[0]
    assert t.reason == "利益確定" and t.net_usd == pytest.approx(2.0)


def test_funding_is_charged_while_holding(tmp_path):
    bot = load_bot(write_bot(tmp_path, FIRST_BAR_LONG))
    cs = flat(6) + candles([(100, 101, 100, 101)], start=T0 + 6 * STEP)
    funding = [Funding(T0 + 2 * STEP, 0.001), Funding(T0 + 3 * STEP, 0.001), Funding(T0 + 99 * STEP, 0.5)]
    t = run(bot, cs, funding, fee_rate=0).trades[0]
    assert t.funding_usd == pytest.approx(2 * 2.0 * 100 * 0.001)
    assert t.net_usd == pytest.approx(2.0 - t.funding_usd)


def test_one_position_and_daily_limit(tmp_path):
    bot = load_bot(write_bot(tmp_path, ALWAYS_LONG, max_trades_per_day=3, take_profit_pct=0.1))
    rows = [(100, 100.2, 99.9, 100)] * 40
    r = run(bot, candles(rows))   # 10時間分。日本時間の日付をまたぐ
    per_day = {}
    for t in r.trades:
        per_day[jst_date(t.entry_t)] = per_day.get(jst_date(t.entry_t), 0) + 1
    assert len(per_day) == 2 and set(per_day.values()) == {3}
    assert all(b.entry_t >= a.exit_t for a, b in zip(r.trades, r.trades[1:]))
    assert r.max_open_positions == 1


def test_min_order_skips_tiny_amount(tmp_path):
    bot = load_bot(write_bot(tmp_path, ALWAYS_LONG))
    assert run(bot, flat(5), amount_usd=5).trades == []


def test_bad_signal_value_raises(tmp_path):
    bot = load_bot(write_bot(tmp_path, "def entry_signal(h):\n    return 'buy'"))
    with pytest.raises(ValueError):
        run(bot, flat(3))
