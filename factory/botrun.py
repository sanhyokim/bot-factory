"""BOTを、そのBOT自身の土台で動かす(SPEC 5.4 の2・9章: BOTは1本ずつ自己完結)。

bots/<記号>/base.py があれば、そのファイル(コピーした時点の土台)で動かす。土台の版はそのファイルの BASE_VERSION。
base.py がないのは段階3bより前に作ったBOT(土台の版0)で、工場の engine.py で動かす。
どちらの版でも、AIが書いた strategy.py は botbase.load_strategy の静的検査を通してから読み込む。
結果は版によらず engine.SimTrade / SimResult の形にそろえて返す(paper評価・反証・自動テストはこの形だけを読む)。
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from . import engine
from .botbase import BotRejected, load_bot, load_strategy
from .hyperliquid.reader import INTERVAL_MS
from .models import PRICE

BASE_FILE = "base.py"


@dataclass(frozen=True)
class Spec:
    """版によらない売買ルールの数字。版0のBOTは値動き型で、最長保有がない(max_hold_hours が None)。"""
    name: str
    type: str
    symbol: str
    timeframe: str
    stop_loss_pct: float
    max_trades_per_day: int
    take_profit_pct: float | None = None
    max_hold_hours: float | None = None
    max_hold_bars: int | None = None
    funding_x: float | None = None
    funding_y: float | None = None

    @property
    def step_ms(self) -> int:
        return INTERVAL_MS[self.timeframe]

    def describe(self) -> str:
        """1行の説明(早送りの表示など)。"""
        if self.type == PRICE:
            head = f"利益確定+{self.take_profit_pct:g}% 損切り−{self.stop_loss_pct:g}%"
        else:
            head = (f"入る ±{self.funding_x:g}%/時 出る ±{self.funding_y:g}%/時の内側 "
                    f"損切り−{self.stop_loss_pct:g}%")
        hold = f" 最長保有{self.max_hold_hours:g}時間" if self.max_hold_hours else ""
        return f"{self.type}型 {self.symbol} {self.timeframe} {head}{hold}"


@dataclass
class LoadedBot:
    path: Path
    spec: Spec
    base_version: int
    entry_signal: object
    exit_signal: object | None
    _base: ModuleType | None = None      # 版1以降: BOT自身の base.py
    _legacy: object | None = None        # 版0: botbase.Bot

    def simulate(self, candles, funding, *, amount_usd: float, fee_rate: float, size_decimals: int = 5,
                 min_order_usd: float = 10.0, trade_from: int | None = None,
                 slippage_rate: float = 0.0) -> engine.SimResult:
        kw = {"amount_usd": amount_usd, "fee_rate": fee_rate, "size_decimals": size_decimals,
              "min_order_usd": min_order_usd, "trade_from": trade_from, "slippage_rate": slippage_rate}
        if self._legacy is not None:
            return engine.simulate(self._legacy, candles, funding, interval_ms=self.spec.step_ms, **kw)
        base_spec = self._base.load_spec(self.path)
        r = self._base.simulate(base_spec, self.entry_signal, self.exit_signal, candles, funding, **kw)
        trades = [engine.SimTrade(t.side, t.entry_t, t.entry_price, t.qty, t.exit_t, t.exit_price, t.fee_usd,
                                  t.slippage_usd, t.funding_usd, t.net_usd, t.reason) for t in r.trades]
        return engine.SimResult(trades=trades, signal_times=list(r.signal_times), start_t=r.start_t, end_t=r.end_t,
                                amount_usd=r.amount_usd, max_open_positions=r.max_open_positions)


def base_version(path: str | Path) -> int:
    """BOTの土台の版。base.py がなければ 0(段階3bより前のBOT)。読み込まずに文字で読む。"""
    file = Path(path) / BASE_FILE
    if not file.exists():
        return 0
    m = re.search(r"^BASE_VERSION\s*=\s*(\d+)", file.read_text(encoding="utf-8"), re.M)
    return int(m.group(1)) if m else 0


def _load_base(path: Path) -> ModuleType:
    """BOTのフォルダの base.py を読み込む。名前は中身から付ける(中身が同じなら同じもの。違えば別々に動く)。"""
    file = path / BASE_FILE
    digest = hashlib.sha1(file.read_bytes()).hexdigest()[:12]
    name = f"factory_bot_base_{digest}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[name]
        raise
    return module


def load(path: str | Path) -> LoadedBot:
    """BOTのフォルダを読み込む。土台の決まりに反していれば BotRejected。"""
    path = Path(path)
    if not (path / BASE_FILE).exists():
        bot = load_bot(path)
        s = bot.spec
        spec = Spec(name=s.name, type=PRICE, symbol=s.symbol, timeframe=s.timeframe, stop_loss_pct=s.stop_loss_pct,
                    max_trades_per_day=s.max_trades_per_day, take_profit_pct=s.take_profit_pct,
                    max_hold_bars=s.max_hold_bars)
        return LoadedBot(path, spec, 0, bot.entry_signal, bot.exit_signal, _legacy=bot)
    base = _load_base(path)
    try:
        b = base.load_spec(path)
    except base.SpecError as e:
        raise BotRejected(str(e)) from e
    entry, exit_ = load_strategy(path)
    spec = Spec(name=b.name, type=b.type, symbol=b.symbol, timeframe=b.timeframe, stop_loss_pct=b.stop_loss_pct,
                max_trades_per_day=b.max_trades_per_day, take_profit_pct=b.take_profit_pct,
                max_hold_hours=b.max_hold_hours, max_hold_bars=b.max_hold_bars, funding_x=b.funding_x,
                funding_y=b.funding_y)
    return LoadedBot(path, spec, int(base.BASE_VERSION), entry, exit_, _base=base)
