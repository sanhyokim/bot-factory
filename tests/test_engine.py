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
    cs = candles([(100, 100, 100, 100), (100, 100.5, 99.8, 100.2), (100.2, 101.5, 100, 101), (101, 101, 101, 101)])
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
    cs = candles([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 98.9, 99), (99, 99, 99, 99)])
    t = run(bot, cs, fee_rate=0).trades[0]
    assert t.reason == "利益確定" and t.net_usd == pytest.approx(2.0)


def test_funding_is_charged_while_holding(tmp_path):
    bot = load_bot(write_bot(tmp_path, FIRST_BAR_LONG))
    cs = flat(6) + candles([(100, 101, 100, 101), (101, 101, 101, 101)], start=T0 + 6 * STEP)
    funding = [Funding(T0 + 2 * STEP, 0.001), Funding(T0 + 3 * STEP, 0.001), Funding(T0 + 99 * STEP, 0.5)]
    t = run(bot, cs, funding, fee_rate=0).trades[0]
    assert t.funding_usd == pytest.approx(-2 * 2.0 * 100 * 0.001)   # 買いで率がプラス → 払う(マイナス)
    assert t.net_usd == pytest.approx(2.0 + t.funding_usd)


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



# --- 約定の値段の決まり(指示書A 項目3) ---

def test_entry_fills_at_the_open_of_the_next_bar(tmp_path):
    # 足N(3本目)が閉じた時に入る条件が成立 → 足N+1(4本目)の始値で入る
    bot = load_bot(write_bot(tmp_path, "def entry_signal(h):\n    return 'long' if len(h) == 3 else None"))
    cs = candles([(100, 100, 100, 100), (101, 101, 101, 101), (102, 102, 102, 103), (104, 104.5, 104, 104.2),
                  (104.2, 104.2, 104.2, 104.2), (105, 106, 105, 106), (107, 107, 107, 107)])
    t = run(bot, cs, fee_rate=0).trades[0]
    assert t.entry_t == cs[3].t and t.entry_price == 104      # 足Nの終値103ではなく、足N+1の始値104


def test_exit_condition_fills_at_the_open_of_the_next_bar(tmp_path):
    exit_on_third = ("def entry_signal(h):\n    return 'long' if len(h) == 1 else None\n\n"
                     "def exit_signal(h, p):\n    return p.bars_held >= 2\n")
    bot = load_bot(write_bot(tmp_path, exit_on_third, take_profit_pct=50, stop_loss_pct=50))
    cs = candles([(100, 100, 100, 100), (100, 100, 100, 100.5), (100.5, 101, 100.5, 101), (101.5, 102, 101, 102)])
    t = run(bot, cs, fee_rate=0).trades[0]
    assert t.reason == "出る条件" and t.exit_t == cs[3].t and t.exit_price == 101.5   # 足N+1の始値


def test_take_profit_fills_at_the_open_of_the_next_bar(tmp_path):
    bot = load_bot(write_bot(tmp_path, FIRST_BAR_LONG))      # 利益確定 +1%(101)
    cs = candles([(100, 100, 100, 100), (100, 100, 100, 100), (100, 101.2, 100, 100.4), (100.6, 100.8, 100.5, 100.7)])
    t = run(bot, cs, fee_rate=0).trades[0]
    assert t.reason == "利益確定" and t.exit_t == cs[3].t and t.exit_price == 100.6   # 101ではなく次の足の始値


def test_stop_loss_fills_inside_the_touched_bar_at_the_stop_price(tmp_path):
    bot = load_bot(write_bot(tmp_path, FIRST_BAR_LONG))      # 損切り −0.5%(99.5)
    cs = candles([(100, 100, 100, 100), (100, 100, 100, 100), (99.9, 100, 99.2, 99.3), (90, 90, 90, 90)])
    t = run(bot, cs, fee_rate=0).trades[0]
    assert t.reason == "損切り" and t.exit_price == pytest.approx(99.5)
    assert t.exit_t == cs[2].t + STEP                          # 次の足(始値90)を待たない


def test_stop_loss_for_short_fills_inside_the_bar(tmp_path):
    bot = load_bot(write_bot(tmp_path, "def entry_signal(h):\n    return 'short' if len(h) == 1 else None"))
    cs = candles([(100, 100, 100, 100), (100, 100, 100, 100), (100.1, 100.8, 100, 100.6), (110, 110, 110, 110)])
    t = run(bot, cs, fee_rate=0).trades[0]
    assert t.reason == "損切り" and t.exit_price == pytest.approx(100.5) and t.exit_t == cs[2].t + STEP


def test_slippage_is_taken_from_both_fills(tmp_path):
    bot = load_bot(write_bot(tmp_path, FIRST_BAR_LONG))
    cs = candles([(100, 100, 100, 100), (100, 100, 100, 100), (100, 101.5, 100, 101), (102, 102, 102, 102)])
    t = run(bot, cs, fee_rate=0, slippage_rate=0.0005).trades[0]   # 往復0.05% = 片道0.025%
    assert t.entry_price == pytest.approx(100 * 1.00025) and t.exit_price == pytest.approx(102 * 0.99975)
    assert t.notional <= 200
    short = load_bot(write_bot(tmp_path, "def entry_signal(h):\n    return 'short' if len(h) == 1 else None"))
    s = run(short, candles([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 98.5, 99), (98, 98, 98, 98)]),
            fee_rate=0, slippage_rate=0.0005).trades[0]
    assert s.entry_price == pytest.approx(100 * 0.99975) and s.exit_price == pytest.approx(98 * 1.00025)
