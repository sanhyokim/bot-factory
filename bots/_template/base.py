"""BOTの土台(SPEC 5.4・9章)。実装工程がこのフォルダを丸ごと bots/<BOT記号>/ にコピーする。

BOTは1本ずつ自己完結: 工場は、BOT自身のフォルダにコピーされたこのファイルで BOT を動かす。
コピーした後に bots/_template/base.py を直しても、すでにあるBOTには効かない(土台の改善はそれ以降のBOTにだけ効く)。
直した時は BASE_VERSION を1つ上げる。このファイルは工場が書いたもので、AIは触らない(AIが書くのは strategy.py だけ)。

決まり(strategy.py からは変えられない):
- レバレッジ1倍: 数量 = 上限額 ÷ 価格。同時に持てるのは1つ
- 1回の上限額: 呼び出し側が渡した額(paper評価は200ドル)
- 1日の最大取引回数(日本時間の日付で数える)を超えたら、その日は入らない
- 入る・出るの判断は足が閉じた時。注文は次の足の始値で通ったとみなす(覗き見をしない)
- 利益確定(値動き型だけ): 値段が触れた足が閉じた所で決め、次の足の始値で出る
- 損切り(両型で必須): 値段が触れたその足の中で、損切りの値段で出る(足の始値がすでに越えていれば始値)。
  同じ足で利益確定にも触れていたら損切りを先とする
- 最長保有(両型で必須): その時間を持ったら、次の足の始値で必ず出る
- 滑り(往復の率)は入りと出に半分ずつ、不利な向きに約定の値段へ含める
- 手数料は入りと出の両方で、資金調達料は持っていた時間分を必ず計算する(受け取りはプラス、支払いはマイナス)
"""
from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

BASE_VERSION = 1
LEVERAGE = 1          # 固定。変えない
MAX_POSITIONS = 1     # 固定。変えない
TYPES = ("値動き", "歪み")
SIDES = ("long", "short")
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000,
    "12h": 43_200_000, "1d": 86_400_000,
}
HOUR_MS = 3_600_000
JST = timezone(timedelta(hours=9))


class SpecError(ValueError):
    """bot.yaml が土台の決まりを守っていない。"""


@dataclass(frozen=True)
class Spec:
    """売買ルールの数字(SPEC 4.2)。bot.yaml から読む。"""
    name: str
    type: str                       # 値動き / 歪み
    symbol: str
    timeframe: str                  # 判断に使う足(歪み型は 1h)
    stop_loss_pct: float            # 例: 0.8 は −0.8% で損切り
    max_hold_hours: float           # 最長保有の時間
    max_trades_per_day: int
    take_profit_pct: float | None = None    # 値動き型だけ。例: 1.5 は +1.5% で利益確定
    funding_x: float | None = None          # 歪み型だけ: 入る資金調達料(%/時)
    funding_y: float | None = None          # 歪み型だけ: 出る資金調達料(%/時)

    @property
    def step_ms(self) -> int:
        return INTERVAL_MS[self.timeframe]

    @property
    def max_hold_bars(self) -> int:
        return max(1, math.ceil(self.max_hold_hours * HOUR_MS / self.step_ms - 1e-9))


def _number(data: dict, key: str) -> float:
    try:
        value = float(data[key])
    except (KeyError, TypeError, ValueError) as e:
        raise SpecError(f"bot.yaml の {key} が数字でない") from e
    if not math.isfinite(value):
        raise SpecError(f"bot.yaml の {key} が数字でない")
    return value


def load_spec(folder: str | Path) -> Spec:
    folder = Path(folder)
    data = yaml.safe_load((folder / "bot.yaml").read_text(encoding="utf-8")) or {}
    if float(data.get("leverage", 1)) != LEVERAGE:
        raise SpecError("レバレッジは1倍固定")
    if int(data.get("max_positions", 1)) != MAX_POSITIONS:
        raise SpecError("同時に持てる数は1固定")
    type_ = str(data.get("type", "")).strip()
    if type_ not in TYPES:
        raise SpecError(f"bot.yaml の type は {' / '.join(TYPES)} のどちらか")
    timeframe = str(data.get("timeframe", "")).strip()
    if timeframe not in INTERVAL_MS:
        raise SpecError(f"bot.yaml の timeframe が不正: {timeframe}")
    symbol = str(data.get("symbol", "")).strip()
    if not symbol:
        raise SpecError("bot.yaml に symbol がない")
    sl, hold = _number(data, "stop_loss_pct"), _number(data, "max_hold_hours")
    if not 0 < sl < 100:
        raise SpecError("損切りの%が範囲外")
    if hold <= 0:
        raise SpecError("最長保有の時間は0より大きい数字")
    trades = int(_number(data, "max_trades_per_day"))
    if trades < 1:
        raise SpecError("1日の最大取引回数は1以上")
    tp = x = y = None
    if type_ == "値動き":
        tp = _number(data, "take_profit_pct")
        if not 0 < tp < 100:
            raise SpecError("利益確定の%が範囲外")
    else:
        x, y = _number(data, "funding_x"), _number(data, "funding_y")
        if not 0 <= y < x:
            raise SpecError("歪み型は 0 ≦ 出る資金調達料(y) < 入る資金調達料(x)")
    return Spec(name=folder.name, type=type_, symbol=symbol, timeframe=timeframe, stop_loss_pct=sl,
                max_hold_hours=hold, max_trades_per_day=trades, take_profit_pct=tp, funding_x=x, funding_y=y)


@dataclass(frozen=True)
class Position:
    side: str
    entry_price: float
    entry_t: int
    bars_held: int


class History(Sequence):
    """今の足までのローソク足。先の足は見えない(覗き見の防止)。"""

    def __init__(self, candles: Sequence, end: int, funding: Sequence = (), funding_times: Sequence[int] = (),
                 step: int = 0):
        self._candles = candles
        self._end = end  # 見えるのは [0, end)
        self._funding = funding
        self._funding_times = funding_times
        self._step = step

    def __len__(self) -> int:
        return self._end

    def __getitem__(self, i):
        if isinstance(i, slice):
            return [self._candles[j] for j in range(*i.indices(self._end))]
        if i < 0:
            i += self._end
        if not 0 <= i < self._end:
            raise IndexError(i)
        return self._candles[i]

    def closes(self, n: int) -> list[float]:
        return [c.c for c in self[-n:]]

    def highs(self, n: int) -> list[float]:
        return [c.h for c in self[-n:]]

    def lows(self, n: int) -> list[float]:
        return [c.l for c in self[-n:]]

    def volumes(self, n: int) -> list[float]:
        return [c.v for c in self[-n:]]

    def funding_rates(self, n: int) -> list[float]:
        """今の足が閉じた時点までに決まった、直近n回の1時間ごとの資金調達料(0.0001 = 0.01%)。"""
        if not self._end or not self._funding:
            return []
        k = bisect.bisect_right(self._funding_times, self._candles[self._end - 1].t + self._step)
        return [f.rate for f in self._funding[max(0, k - n):k]]


@dataclass
class Trade:
    side: str
    entry_t: int
    entry_price: float      # 滑りを含めた約定の値段
    qty: float
    exit_t: int
    exit_price: float       # 滑りを含めた約定の値段
    fee_usd: float
    slippage_usd: float     # 滑りで不利になった額(約定の値段に含めてある。純利益からは引き済み)
    funding_usd: float      # 受け取りはプラス、支払いはマイナス(SPEC 4.4)
    net_usd: float          # 手数料・滑り・資金調達料を引いた後
    reason: str             # 利益確定 / 損切り / 出る条件 / 保有上限

    @property
    def notional(self) -> float:
        return self.qty * self.entry_price


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
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


def _funding_received(side: str, qty: float, entry_t: int, exit_t: int, funding, funding_times, candles,
                      candle_times) -> float:
    """持っていた間に来た資金調達(受け取りはプラス)。値段はその時刻の足の終値を使う。
    率がプラスの時は買いが払い売りが受け取る。"""
    sign = -1 if side == "long" else 1
    got = 0.0
    for k in range(bisect.bisect_right(funding_times, entry_t), bisect.bisect_left(funding_times, exit_t)):
        f = funding[k]
        j = max(0, bisect.bisect_right(candle_times, f.t) - 1)
        got += sign * qty * candles[j].c * f.rate
    return got


def simulate(spec: Spec, entry_signal, exit_signal, candles: Sequence, funding: Sequence, *, amount_usd: float,
             fee_rate: float, size_decimals: int = 5, min_order_usd: float = 10.0,
             trade_from: int | None = None, slippage_rate: float = 0.0) -> Result:
    """trade_from(ミリ秒)を渡すと、その時刻より前の足は判断の材料として見せるだけで、売買はしない。"""
    if not candles:
        raise ValueError("ローソク足がない")
    step = spec.step_ms
    tp = spec.take_profit_pct / 100 if spec.take_profit_pct is not None else None
    sl = spec.stop_loss_pct / 100
    candle_times = [c.t for c in candles]
    funding_times = [f.t for f in funding]
    start = candles[0].t if trade_from is None else max(trade_from, candles[0].t)
    result = Result(start_t=start, end_t=candles[-1].t + step, amount_usd=amount_usd)

    pos: dict | None = None          # side, entry_price, raw_entry, entry_t, qty, entry_index
    pending_entry: str | None = None
    pending_exit: str | None = None
    trades_per_day: dict[str, int] = {}
    half_slip = slippage_rate / 2

    def close(t: int, raw: float, reason: str) -> None:
        nonlocal pos
        p = pos
        sign = 1 if p["side"] == "long" else -1
        price = raw * (1 - sign * half_slip)          # 出る時の滑り(買いは安く、売りは高く出る)
        gross = sign * (price - p["entry_price"]) * p["qty"]
        fee = fee_rate * p["qty"] * (p["entry_price"] + price)
        slip = p["qty"] * (p["raw_entry"] + raw) * half_slip
        fund = _funding_received(p["side"], p["qty"], p["entry_t"], t, funding, funding_times, candles,
                                 candle_times)
        result.trades.append(Trade(p["side"], p["entry_t"], p["entry_price"], p["qty"], t, price, fee, slip, fund,
                                   gross - fee + fund, reason))
        pos = None

    for i, bar in enumerate(candles):
        # 1) 前の足で決めた注文を、この足の始値で通す
        if pos is not None and pending_exit:
            close(bar.t, bar.o, pending_exit)
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
                stop = e * (1 - sl)
                if bar.l <= stop:
                    close(bar.t + step, min(bar.o, stop) if pos["entry_index"] < i else stop, "損切り")
                elif tp is not None:
                    take_hit = bar.h >= e * (1 + tp)
            else:
                stop = e * (1 + sl)
                if bar.h >= stop:
                    close(bar.t + step, max(bar.o, stop) if pos["entry_index"] < i else stop, "損切り")
                elif tp is not None:
                    take_hit = bar.l <= e * (1 - tp)

        # 3) 足が閉じた所で、次の足の注文を決める(売買を始める時刻より前は判断しない)
        if bar.t + step < start:
            continue
        history = History(candles, i + 1, funding, funding_times, step)
        signal = entry_signal(history)
        if signal is not None and signal not in SIDES:
            raise ValueError(f"entry_signal の戻り値が不正: {signal!r}")
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
            elif held >= spec.max_hold_bars:
                pending_exit = "保有上限"
            elif exit_signal is not None:
                view = Position(pos["side"], pos["entry_price"], pos["entry_t"], held)
                if exit_signal(history, view):
                    pending_exit = "出る条件"
    # 期間の終わりに持っている分は、決着していないので数えない
    return result
