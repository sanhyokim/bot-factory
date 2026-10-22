"""BOTを価格の上で動かす土台(注文・記録・停止の固定部分。SPEC 5.4・5.5・9章)。

段階3bから、新しいBOTは自分のフォルダにコピーされた土台(bots/<記号>/base.py)で動く(SPEC 5.4 の2)。
このファイルは、それより前に作ったBOT(土台の版0: bots/<記号>/ に base.py がない)だけを動かす。
売買の動き(いつ入り・いつ出て・いくらで約定するか)は版0のまま変えない。変えたのは記録の書き方だけ
(資金調達料を受け取りプラスに、滑りの額を別に記録。SPEC 4.4)。

決まり(strategy.py からは変えられない):
- レバレッジ1倍: 数量 = 上限額 ÷ 価格。借りて増やすことはしない
- 同時保有1: 持っている間は新しく入らない
- 1回の上限額: 呼び出し側が渡した額(paper評価は200ドル)
- 1日の最大取引回数(日本時間の日付で数える)を超えたら、その日は入らない
- 入る・出るの判断は足が閉じた時。注文は次の足の始値で通ったとみなす(覗き見をしない)
- 利益確定も出る条件の1つ: 値段が触れた足が閉じた所で決め、次の足の始値で出る
- 損切りだけは、値段が触れたその足の中で損切りの値段で出る(足の始値がすでに越えていれば始値)。
  同じ足で利益確定にも触れていたら損切りを先とする
- 滑り(slippage_rate、往復の率)は入りと出に半分ずつ、不利な向きに約定の値段へ含める
- 手数料は入りと出の両方で、資金調達料は持っていた時間分を必ず引く
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime

from .botbase import SIDES, Bot, BotRejected, History, Position
from .hyperliquid.reader import Candle, Funding
from .models import JST

LEVERAGE = 1          # 固定。変えない
MAX_POSITIONS = 1     # 固定。変えない


@dataclass
class SimTrade:
    side: str
    entry_t: int
    entry_price: float
    qty: float
    exit_t: int
    exit_price: float
    fee_usd: float
    slippage_usd: float     # 滑りで不利になった額(約定の値段に含めてある。純利益からは引き済み)
    funding_usd: float      # 受け取りはプラス、支払いはマイナス(SPEC 4.4)
    net_usd: float          # 手数料・滑り・資金調達料を引いた後
    reason: str             # 利益確定 / 損切り / 出る条件 / 保有上限

    @property
    def notional(self) -> float:
        return self.qty * self.entry_price


@dataclass
class SimResult:
    trades: list[SimTrade] = field(default_factory=list)
    signal_times: list[int] = field(default_factory=list)   # 入る条件が成立した足の終わり(ミリ秒)
    start_t: int = 0
    end_t: int = 0
    amount_usd: float = 0.0
    max_open_positions: int = 0


def jst_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, JST).date().isoformat()


def _floor_qty(qty: float, decimals: int) -> float:
    f = 10 ** decimals
    return math.floor(qty * f + 1e-9) / f


def _funding_received(side: str, qty: float, entry_t: int, exit_t: int, funding: list[Funding],
                      funding_times: list[int], candles: list[Candle], candle_times: list[int]) -> float:
    """持っていた間に来た資金調達(受け取りはプラス)。値段はその時刻の足の終値を使う。
    率がプラスの時は買いが払い売りが受け取る。"""
    sign = -1 if side == "long" else 1
    got = 0.0
    for k in range(bisect.bisect_right(funding_times, entry_t), bisect.bisect_left(funding_times, exit_t)):
        f = funding[k]
        j = max(0, bisect.bisect_right(candle_times, f.t) - 1)
        got += sign * qty * candles[j].c * f.rate
    return got


def simulate(bot: Bot, candles: list[Candle], funding: list[Funding], *, amount_usd: float,
             fee_rate: float, size_decimals: int = 5, min_order_usd: float = 10.0,
             interval_ms: int | None = None, trade_from: int | None = None,
             slippage_rate: float = 0.0) -> SimResult:
    """trade_from(ミリ秒)を渡すと、その時刻より前の足は判断の材料として見せるだけで、売買はしない。"""
    if not candles:
        raise ValueError("ローソク足がない")
    spec = bot.spec
    tp, sl = spec.take_profit_pct / 100, spec.stop_loss_pct / 100
    step = interval_ms or (candles[1].t - candles[0].t if len(candles) > 1 else 60_000)
    candle_times = [c.t for c in candles]
    funding_times = [f.t for f in funding]
    start = candles[0].t if trade_from is None else max(trade_from, candles[0].t)
    result = SimResult(start_t=start, end_t=candles[-1].t + step, amount_usd=amount_usd)

    pos: dict | None = None          # side, entry_price, raw_entry, entry_t, qty, entry_index
    pending_entry: str | None = None
    pending_exit: str | None = None
    trades_per_day: dict[str, int] = {}
    half_slip = slippage_rate / 2

    def close(i: int, t: int, raw: float, reason: str) -> None:
        nonlocal pos
        p = pos
        sign = 1 if p["side"] == "long" else -1
        price = raw * (1 - sign * half_slip)          # 出る時の滑り(買いは安く、売りは高く出る)
        gross = sign * (price - p["entry_price"]) * p["qty"]
        fee = fee_rate * p["qty"] * (p["entry_price"] + price)
        slip = p["qty"] * (p["raw_entry"] + raw) * half_slip
        fund = _funding_received(p["side"], p["qty"], p["entry_t"], t, funding, funding_times, candles,
                                 candle_times)
        result.trades.append(SimTrade(p["side"], p["entry_t"], p["entry_price"], p["qty"], t, price,
                                      fee, slip, fund, gross - fee + fund, reason))
        pos = None

    for i, bar in enumerate(candles):
        # 1) 前の足で決めた注文を、この足の始値で通す
        if pos is not None and pending_exit:
            close(i, bar.t, bar.o, pending_exit)
        pending_exit = None
        if pos is None and pending_entry is not None:
            fill = bar.o * (1 + (1 if pending_entry == "long" else -1) * half_slip)   # 入る時の滑り
            qty = _floor_qty(amount_usd * LEVERAGE / fill, size_decimals)
            if qty > 0 and qty * fill >= min_order_usd:
                pos = {"side": pending_entry, "entry_price": fill, "raw_entry": bar.o, "entry_t": bar.t, "qty": qty,
                       "entry_index": i}
                day = jst_date(bar.t)
                trades_per_day[day] = trades_per_day.get(day, 0) + 1
                result.max_open_positions = max(result.max_open_positions, 1)
        pending_entry = None

        # 2) 損切りは足の中で約定。利益確定は触れたことだけ覚え、次の足の始値で出る
        take_hit = False
        if pos is not None:
            e = pos["entry_price"]
            if pos["side"] == "long":
                stop, take = e * (1 - sl), e * (1 + tp)
                if bar.l <= stop:
                    close(i, bar.t + step, min(bar.o, stop) if pos["entry_index"] < i else stop, "損切り")
                else:
                    take_hit = bar.h >= take
            else:
                stop, take = e * (1 + sl), e * (1 - tp)
                if bar.h >= stop:
                    close(i, bar.t + step, max(bar.o, stop) if pos["entry_index"] < i else stop, "損切り")
                else:
                    take_hit = bar.l <= take

        # 3) 足が閉じた所で、次の足の注文を決める(売買を始める時刻より前は判断しない)
        if bar.t + step < start:
            continue
        history = History(candles, i + 1, funding, funding_times, step)
        signal = bot.entry_signal(history)
        if signal is not None and signal not in SIDES:
            raise BotRejected(f"entry_signal の戻り値が不正: {signal!r}")
        if signal is not None:
            result.signal_times.append(bar.t + step)
        if pos is None:
            next_day = jst_date(bar.t + step)
            if signal is not None and trades_per_day.get(next_day, 0) < spec.max_trades_per_day:
                pending_entry = signal
        else:
            held = i - pos["entry_index"] + 1
            if take_hit:
                pending_exit = "利益確定"
            elif spec.max_hold_bars and held >= spec.max_hold_bars:
                pending_exit = "保有上限"
            elif bot.exit_signal is not None:
                view = Position(pos["side"], pos["entry_price"], pos["entry_t"], held)
                if bot.exit_signal(history, view):
                    pending_exit = "出る条件"
    # 期間の終わりに持っている分は、決着していないので数えない
    return result
