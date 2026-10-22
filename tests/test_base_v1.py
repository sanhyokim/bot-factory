"""BOTの土台 版1(bots/_template/base.py)と、BOTごとに土台で動かす読み込み(factory/botrun.py)。SPEC 5.4・9章。

- BOTは1本ずつ自己完結: コピーした base.py で動き、別のBOTの土台を直しても影響しない
- 損切りと最長保有は両型で必須。土台が強制する
- 取引記録に滑りの額を残し、資金調達料は受け取りをプラスにする(SPEC 4.4)
"""
import shutil

import pytest
import yaml

from factory import autotest, botrun, config
from factory.botbase import BotRejected
from factory.engine import SimResult, SimTrade
from factory.hyperliquid.reader import Funding
from tests.helpers import HOUR, STEP, T0, candles, flat

TEMPLATE = config.ROOT / "bots" / "_template"
FIRST_BAR_LONG = "def entry_signal(h):\n    return 'long' if len(h) == 1 else None\n"
FUNDING_CODE = '''X = 0.0001
Y = 0.00002


def entry_signal(history):
    rates = history.funding_rates(1)
    if not rates:
        return None
    if rates[-1] >= X:
        return "short"
    if rates[-1] <= -X:
        return "long"
    return None


def exit_signal(history, position):
    rates = history.funding_rates(1)
    return bool(rates) and abs(rates[-1]) < Y
'''


def v1_bot(tmp_path, strategy, name="bot", **fields):
    d = tmp_path / name
    d.mkdir()
    shutil.copyfile(TEMPLATE / "base.py", d / "base.py")
    data = {"type": "値動き", "symbol": "BTC", "timeframe": "15m", "stop_loss_pct": 0.5, "max_hold_hours": 1,
            "max_trades_per_day": 10, "take_profit_pct": 1.0, **fields}
    data = {k: v for k, v in data.items() if v is not None}
    (d / "bot.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (d / "strategy.py").write_text(strategy, encoding="utf-8")
    return d


def run(bot, cs, funding=(), **kw):
    args = {"amount_usd": 200, "fee_rate": 0.0005, "size_decimals": 2, "min_order_usd": 10, **kw}
    return bot.simulate(cs, list(funding), **args)


def test_max_hold_is_enforced_by_the_base(tmp_path):
    bot = botrun.load(v1_bot(tmp_path, FIRST_BAR_LONG))         # 最長1時間 = 15分足4本
    assert bot.base_version == 1 and bot.spec.max_hold_bars == 4
    t = run(bot, flat(10)).trades[0]
    assert t.reason == "保有上限" and (t.exit_t - t.entry_t) == 4 * STEP


def test_take_profit_and_stop_loss_like_version_zero(tmp_path):
    bot = botrun.load(v1_bot(tmp_path, FIRST_BAR_LONG, max_hold_hours=24))
    up = candles([(100, 100, 100, 100), (100, 100.5, 99.8, 100.2), (100.2, 101.5, 100, 101), (101, 101, 101, 101)])
    t = run(bot, up).trades[0]
    assert (t.entry_price, t.exit_price, t.reason) == (100, 101, "利益確定")
    down = candles([(100, 100, 100, 100), (100, 100, 100, 100), (100, 102, 99, 100)])
    t = run(bot, down).trades[0]
    assert t.reason == "損切り" and t.exit_price == pytest.approx(99.5)


def test_slippage_is_recorded_and_funding_received_is_positive(tmp_path):
    bot = botrun.load(v1_bot(tmp_path, FIRST_BAR_LONG))
    t = run(bot, flat(10), slippage_rate=0.0005).trades[0]
    assert t.slippage_usd == pytest.approx(t.qty * 200 * 0.00025)
    gross = (t.exit_price - t.entry_price) * t.qty
    assert gross == pytest.approx(-t.slippage_usd)             # 値段が動かない時の損は滑りの分だけ
    assert t.net_usd == pytest.approx(gross - t.fee_usd + t.funding_usd)


def test_distortion_bot_receives_funding_and_exits_when_it_returns(tmp_path):
    bot = botrun.load(v1_bot(tmp_path, FUNDING_CODE, type="歪み", timeframe="1h", take_profit_pct=None,
                             funding_x=0.01, funding_y=0.002, max_hold_hours=48, stop_loss_pct=5))
    cs = flat(12, step=HOUR)
    funding = [Funding(T0 + k * HOUR, 0.0002 if k <= 5 else 0.0) for k in range(1, 12)]
    r = run(bot, cs, funding, fee_rate=0)
    t = r.trades[0]
    assert t.side == "short" and t.reason == "出る条件"
    assert (t.entry_t, t.exit_t) == (T0 + HOUR, T0 + 6 * HOUR)
    assert t.funding_usd == pytest.approx(4 * 0.0002 * t.qty * 100)       # 2〜5時間目の4回を受け取った
    assert t.net_usd == pytest.approx(t.funding_usd)


def test_distortion_bot_is_cut_by_max_hold(tmp_path):
    bot = botrun.load(v1_bot(tmp_path, FUNDING_CODE, type="歪み", timeframe="1h", take_profit_pct=None,
                             funding_x=0.01, funding_y=0.002, max_hold_hours=3, stop_loss_pct=5))
    funding = [Funding(T0 + k * HOUR, 0.0002) for k in range(1, 12)]
    t = run(bot, flat(12, step=HOUR), funding).trades[0]
    assert t.reason == "保有上限" and t.exit_t - t.entry_t == 3 * HOUR


@pytest.mark.parametrize("fields, message", [
    ({"max_hold_hours": None}, "max_hold_hours"),
    ({"stop_loss_pct": None}, "stop_loss_pct"),
    ({"leverage": 2}, "レバレッジ"),
    ({"type": "歪み", "funding_x": 0.01, "funding_y": 0.01}, "y"),
    ({"type": "その他"}, "type"),
])
def test_spec_rules_are_enforced(tmp_path, fields, message):
    with pytest.raises(BotRejected, match=message):
        botrun.load(v1_bot(tmp_path, FIRST_BAR_LONG, **fields))


def test_forbidden_strategy_is_rejected_for_version_one_too(tmp_path):
    with pytest.raises(BotRejected, match="使えないモジュール"):
        botrun.load(v1_bot(tmp_path, "import os\ndef entry_signal(h):\n    return None\n"))


def test_each_bot_runs_on_its_own_copy_of_the_base(tmp_path):
    """土台を後で直しても、すでにコピーしたBOTには効かない(SPEC 5.4 の2)。"""
    old = botrun.load(v1_bot(tmp_path, FIRST_BAR_LONG, name="old"))
    newer = v1_bot(tmp_path, FIRST_BAR_LONG, name="new")
    text = (newer / "base.py").read_text(encoding="utf-8")
    (newer / "base.py").write_text(text.replace("BASE_VERSION = 1", "BASE_VERSION = 2")
                                   .replace('"保有上限"', '"保有上限(版2)"'), encoding="utf-8")
    new = botrun.load(newer)
    assert (old.base_version, new.base_version) == (1, 2)
    assert run(old, flat(10)).trades[0].reason == "保有上限"
    assert run(new, flat(10)).trades[0].reason == "保有上限(版2)"
    assert botrun.base_version(newer) == 2 and botrun.base_version(config.ROOT / "bots" / "sample_breakout") == 0


def test_version_zero_bot_still_runs_on_the_engine():
    bot = botrun.load(config.ROOT / "bots" / "sample_breakout")
    assert bot.base_version == 0 and bot.spec.type == "値動き" and bot.spec.max_hold_hours is None
    assert "利益確定+1%" in bot.spec.describe()


class StubBot:
    """自動テスト(c)の検査そのものを確かめるための、決まった取引を返すBOT。"""

    def __init__(self, trades):
        self.spec = botrun.Spec(name="stub", type="値動き", symbol="BTC", timeframe="15m", stop_loss_pct=0.5,
                                max_trades_per_day=10, take_profit_pct=1.0, max_hold_hours=1, max_hold_bars=4)
        self.trades = trades

    def simulate(self, cs, funding, **kw):
        return SimResult(trades=self.trades, signal_times=[T0], start_t=T0, end_t=cs[-1].t + STEP, amount_usd=200,
                         max_open_positions=1)


def trade(entry_bar, exit_bar, reason):
    return SimTrade("long", T0 + entry_bar * STEP, 100.0, 1.0, T0 + exit_bar * STEP, 100.0, 0.1, 0.0, 0.0, -0.1,
                    reason)


def test_autotest_c_checks_stop_loss_and_max_hold():
    cs = flat(10)
    cs[2] = cs[2].__class__(cs[2].t, 100, 100, 99, 100, 1.0)       # 3本目で損切りの値段(99.5)に触れる
    ok = autotest.run(StubBot([trade(2, 3, "損切り")]), cs, [], amount_usd=200, fee_rate=0, size_decimals=2,
                      min_order_usd=10)[0][2]
    assert ok.ok and "損切りを越えて持った取引 0件" in ok.detail
    held = autotest.run(StubBot([trade(1, 4, "出る条件")]), cs, [], amount_usd=200, fee_rate=0, size_decimals=2,
                        min_order_usd=10)[0][2]
    assert not held.ok and "損切りを越えて持った取引 1件" in held.detail
    long_ = autotest.run(StubBot([trade(4, 9, "出る条件")]), cs, [], amount_usd=200, fee_rate=0, size_decimals=2,
                         min_order_usd=10)[0][2]
    assert not long_.ok and "最長の保有 5本(上限 4本)" in long_.detail
