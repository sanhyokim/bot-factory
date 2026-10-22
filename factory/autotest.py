"""BOTの自動テスト(SPEC 5.4 の3): 実装工程で、paper評価の前に必ず通す。

(a) 過去30日の価格で動かして例外が出ない
(b) 入る条件が30日で1回以上成立する
(c) レバレッジ1倍・同時保有1・上限額・損切り・最長保有が守られている
(d) 生成部分にファイルの読み書き・外部通信・鍵の参照が含まれない は、読み込む時の静的検査(botbase.static_check)
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

from .botrun import LoadedBot
from .engine import LEVERAGE, MAX_POSITIONS, SimResult
from .hyperliquid.reader import Candle, Funding


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def stop_breaches(bot: LoadedBot, sim: SimResult, candles: list[Candle]) -> int:
    """損切りの値段に触れたのに、その足で出なかった取引の数。"""
    sl = bot.spec.stop_loss_pct / 100
    times = [c.t for c in candles]
    bad = 0
    for t in sim.trades:
        lo, hi = bisect.bisect_left(times, t.entry_t), bisect.bisect_left(times, t.exit_t)
        held = candles[lo:hi]
        if t.side == "long":
            touched = [c.l <= t.entry_price * (1 - sl) for c in held]
        else:
            touched = [c.h >= t.entry_price * (1 + sl) for c in held]
        if t.reason == "損切り":
            touched = touched[:-1]      # 触れた足で出たのは正しい
        if any(touched):
            bad += 1
    return bad


def run(bot: LoadedBot, candles: list[Candle], funding: list[Funding], *, amount_usd: float, fee_rate: float,
        size_decimals: int, min_order_usd: float, slippage_rate: float = 0.0
        ) -> tuple[list[CheckResult], SimResult | None]:
    checks: list[CheckResult] = []
    try:
        sim = bot.simulate(candles, funding, amount_usd=amount_usd, fee_rate=fee_rate, size_decimals=size_decimals,
                           min_order_usd=min_order_usd, slippage_rate=slippage_rate)
    except Exception as e:  # BOTのどんな異常も(a)の不合格として修理に回す
        checks.append(CheckResult("(a) 例外なく動く", False, f"{type(e).__name__}: {e}"))
        return checks, None
    checks.append(CheckResult("(a) 例外なく動く", True, f"{len(candles)}本の足で完走"))
    n = len(sim.signal_times)
    checks.append(CheckResult("(b) 入る条件が成立する", n >= 1, f"成立 {n} 回"))

    over = [t for t in sim.trades if t.notional > amount_usd * LEVERAGE * (1 + 1e-9)]
    overlap = any(b.entry_t < a.exit_t for a, b in zip(sim.trades, sim.trades[1:]))
    breaches = stop_breaches(bot, sim, candles)
    max_bars = bot.spec.max_hold_bars
    step = bot.spec.step_ms
    longest = max(((t.exit_t - t.entry_t) // step for t in sim.trades), default=0)
    too_long = max_bars is not None and longest > max_bars
    ok = not over and not overlap and sim.max_open_positions <= MAX_POSITIONS and not breaches and not too_long
    detail = (f"最大の取引額 {max((t.notional for t in sim.trades), default=0):.2f}ドル"
              f"(上限 {amount_usd:.0f}ドル)・同時保有 最大{sim.max_open_positions}")
    if overlap:
        detail += "・保有が重なった"
    detail += f"・損切りを越えて持った取引 {breaches}件"
    if max_bars is not None:
        detail += f"・最長の保有 {longest}本(上限 {max_bars}本)"
    else:
        detail += "・最長保有なし(土台の版0)"
    checks.append(CheckResult("(c) 1倍・同時1・上限額・損切り・最長保有", ok, detail))
    return checks, sim
