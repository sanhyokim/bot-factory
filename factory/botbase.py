"""BOTの土台(SPEC 5.4・9章)。

BOT 1本 = bots/<記号>/ のフォルダ。中身は2つだけ:
  - bot.yaml     売買ルールの数字(銘柄・時間の単位・利益確定・損切り・1日の最大取引回数)
  - strategy.py  「入る条件」「出る条件」の関数だけ(AIが書くのはここだけ)

注文・記録・停止・資金の上限・レバレッジ1倍・同時保有1は engine.py が受け持ち、
strategy.py からは変えられない。strategy.py は読み込む前に静的検査にかける。
"""
from __future__ import annotations

import ast
import builtins
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from .hyperliquid.reader import Candle, interval_code

ALLOWED_IMPORTS = {"math", "statistics"}
FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr",
    "setattr", "delattr", "input", "breakpoint", "exit", "quit", "help", "memoryview",
    "classmethod", "staticmethod", "property", "super", "object", "type", "dir", "id",
}
SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in ("abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "len",
                 "list", "map", "max", "min", "range", "reversed", "round", "set", "sorted", "sum",
                 "tuple", "zip", "isinstance", "ValueError", "ZeroDivisionError")
}
REQUIRED_FIELDS = ("symbol", "timeframe", "take_profit_pct", "stop_loss_pct", "max_trades_per_day")
SIDES = ("long", "short")


class BotRejected(ValueError):
    """BOTが土台の決まりを守っていない。"""


@dataclass(frozen=True)
class BotSpec:
    name: str
    symbol: str
    timeframe: str          # 取引所の書き方(例: 15m)
    take_profit_pct: float  # 例: 1.0 は +1.0% で利益確定
    stop_loss_pct: float    # 例: 0.6 は −0.6% で損切り
    max_trades_per_day: int
    max_hold_bars: int | None = None


@dataclass(frozen=True)
class Position:
    side: str
    entry_price: float
    entry_t: int
    bars_held: int


class History(Sequence):
    """今の足までのローソク足。先の足は見えない(覗き見の防止)。"""

    def __init__(self, candles: Sequence[Candle], end: int):
        self._candles = candles
        self._end = end  # 見えるのは [0, end)

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


@dataclass
class Bot:
    spec: BotSpec
    entry_signal: object
    exit_signal: object | None
    path: Path


def static_check(source: str) -> None:
    """strategy.py を実行せずに検査する。ファイルの読み書き・外部通信・抜け道を弾く。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise BotRejected(f"文法エラー: {e}") from e
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    raise BotRejected(f"使えないモジュール: {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module not in ALLOWED_IMPORTS:
                raise BotRejected(f"使えないモジュール: {node.module}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise BotRejected(f"使えない名前: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise BotRejected(f"使えない属性: {node.attr}")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.ClassDef, ast.AsyncFunctionDef,
                               ast.Await, ast.Yield, ast.YieldFrom, ast.With, ast.AsyncWith, ast.Try)):
            raise BotRejected(f"使えない書き方: {type(node).__name__}")
    for node in tree.body:
        ok = isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign, ast.AnnAssign)) or (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        if not ok:
            raise BotRejected(f"一番外側に置けるのは import・関数・定数だけ: {type(node).__name__}")
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "entry_signal" not in names:
        raise BotRejected("entry_signal がない")


def load_spec(path: Path) -> BotSpec:
    data = yaml.safe_load((path / "bot.yaml").read_text(encoding="utf-8")) or {}
    missing = [f for f in REQUIRED_FIELDS if data.get(f) in (None, "")]
    if missing:
        raise BotRejected(f"bot.yaml に足りない項目: {', '.join(missing)}")
    if float(data.get("leverage", 1)) != 1:
        raise BotRejected("レバレッジは1倍固定")
    if int(data.get("max_positions", 1)) != 1:
        raise BotRejected("同時に持てる数は1固定")
    spec = BotSpec(
        name=path.name, symbol=str(data["symbol"]).upper(), timeframe=interval_code(str(data["timeframe"])),
        take_profit_pct=float(data["take_profit_pct"]), stop_loss_pct=float(data["stop_loss_pct"]),
        max_trades_per_day=int(data["max_trades_per_day"]),
        max_hold_bars=int(data["max_hold_bars"]) if data.get("max_hold_bars") else None,
    )
    if not (0 < spec.take_profit_pct < 100 and 0 < spec.stop_loss_pct < 100):
        raise BotRejected("利益確定・損切りの%が範囲外")
    if spec.max_trades_per_day < 1:
        raise BotRejected("1日の最大取引回数は1以上")
    return spec


def load_bot(path: str | Path) -> Bot:
    path = Path(path)
    spec = load_spec(path)
    source = (path / "strategy.py").read_text(encoding="utf-8")
    static_check(source)
    def _guarded_import(name, *args, **kwargs):
        if name not in ALLOWED_IMPORTS:
            raise ImportError(name)
        return builtins.__import__(name, *args, **kwargs)

    namespace: dict = {"__builtins__": {**SAFE_BUILTINS, "__import__": _guarded_import},
                       "math": math, "statistics": statistics}
    exec(compile(source, str(path / "strategy.py"), "exec"), namespace)  # 検査済みのコードだけ
    return Bot(spec=spec, entry_signal=namespace["entry_signal"],
               exit_signal=namespace.get("exit_signal"), path=path)
