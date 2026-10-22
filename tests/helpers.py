from pathlib import Path

from factory.engine import SimResult, SimTrade
from factory.hyperliquid.reader import Candle

T0 = 1_760_000_000_000 - (1_760_000_000_000 % 900_000)   # 15分足の区切り
STEP = 900_000
HOUR = 3_600_000
DAY = 24 * HOUR


def candles(rows, start=T0, step=STEP):
    """rows: (o, h, l, c) の並び。"""
    return [Candle(start + i * step, o, h, l, c, 1.0) for i, (o, h, l, c) in enumerate(rows)]


def flat(n, price=100.0, start=T0, step=STEP):
    return candles([(price, price, price, price)] * n, start, step)


def write_bot(tmp_path: Path, strategy: str, **yaml_fields) -> Path:
    fields = {"symbol": "BTC", "timeframe": "15m", "take_profit_pct": 1.0, "stop_loss_pct": 0.5,
              "max_trades_per_day": 10, **yaml_fields}
    d = tmp_path / "bot"
    d.mkdir(exist_ok=True)
    (d / "bot.yaml").write_text("\n".join(f"{k}: {v}" for k, v in fields.items()), encoding="utf-8")
    (d / "strategy.py").write_text(strategy, encoding="utf-8")
    return d


def trade(exit_t, net, fee=0.2, slippage=0.0):
    return SimTrade("long", exit_t - HOUR, 100.0, 2.0, exit_t, 100.0, fee, slippage, 0.0, net, "test")


def sim(trades, *, days=30, signal_every_hours=1):
    end = T0 + days * DAY
    signals = list(range(T0 + HOUR, end, signal_every_hours * HOUR)) if signal_every_hours else []
    return SimResult(trades=trades, signal_times=signals, start_t=T0, end_t=end, amount_usd=200)
