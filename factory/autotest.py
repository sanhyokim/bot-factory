"""BOTの自動テスト(SPEC 5.4 の2): 実装工程で、paper評価の前に必ず通す3つ。

(a) 過去30日の価格で動かして例外が出ない
(b) 入る条件が30日で1回以上成立する
(c) レバレッジ1倍・同時保有1・上限額が守られている
"""
from __future__ import annotations

from dataclasses import dataclass

from .botbase import Bot
from .engine import LEVERAGE, MAX_POSITIONS, SimResult, simulate
from .hyperliquid.reader import Candle, Funding


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def run(bot: Bot, candles: list[Candle], funding: list[Funding], *, amount_usd: float, fee_rate: float,
        size_decimals: int, min_order_usd: float, slippage_rate: float = 0.0
        ) -> tuple[list[CheckResult], SimResult | None]:
    checks: list[CheckResult] = []
    try:
        sim = simulate(bot, candles, funding, amount_usd=amount_usd, fee_rate=fee_rate,
                       size_decimals=size_decimals, min_order_usd=min_order_usd, slippage_rate=slippage_rate)
    except Exception as e:  # BOTのどんな異常も(a)の不合格として修理に回す
        checks.append(CheckResult("(a) 例外なく動く", False, f"{type(e).__name__}: {e}"))
        return checks, None
    checks.append(CheckResult("(a) 例外なく動く", True, f"{len(candles)}本の足で完走"))
    n = len(sim.signal_times)
    checks.append(CheckResult("(b) 入る条件が成立する", n >= 1, f"成立 {n} 回"))

    over = [t for t in sim.trades if t.notional > amount_usd * LEVERAGE * (1 + 1e-9)]
    overlap = any(b.entry_t < a.exit_t for a, b in zip(sim.trades, sim.trades[1:]))
    ok = not over and not overlap and sim.max_open_positions <= MAX_POSITIONS
    detail = (f"最大の取引額 {max((t.notional for t in sim.trades), default=0):.2f}ドル"
              f"(上限 {amount_usd:.0f}ドル)・同時保有 最大{sim.max_open_positions}")
    if overlap:
        detail += "・保有が重なった"
    checks.append(CheckResult("(c) 1倍・同時1・上限額", ok, detail))
    return checks, sim
