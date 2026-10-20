"""価格と資金調達料の保管(段階3)。

取引所は直近5000本のローソク足しか返さない(5分足なら約17日分)。毎日の実行で取った足を帳簿に貯め、
paper評価を始めた時点からの価格をたどれるようにする。保管するのは閉じた足だけ。
"""
from __future__ import annotations

import sqlite3

from .hyperliquid.reader import INTERVAL_MS, Candle, Funding, Reader, interval_code, now_ms

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


class Market:
    def __init__(self, conn: sqlite3.Connection, reader: Reader, *, clock=now_ms):
        self.conn = conn
        self.reader = reader
        self._clock = clock
        self._decimals: dict[str, int] | None = None
        self._tried: dict[tuple, int] = {}   # この実行で取りに行った一番古い時刻(取れない過去を何度も取りに行かない)

    def now(self) -> int:
        return self._clock()

    def size_decimals(self) -> dict[str, int]:
        """無期限先物の銘柄と数量の小数桁。1回の実行で1度だけ取る。"""
        if self._decimals is None:
            self._decimals = self.reader.size_decimals()
        return self._decimals

    def resolve(self, symbol: str) -> str | None:
        """銘柄名を取引所の書き方に直す(大文字小文字の違いだけ。kPEPE のように小文字を含む銘柄がある)。"""
        names = self.size_decimals()
        if symbol in names:
            return symbol
        return {k.upper(): k for k in names}.get(symbol.strip().upper())

    # --- ローソク足 ---
    def candles(self, symbol: str, timeframe: str, start_ms: int, end_ms: int | None = None) -> list[Candle]:
        code = interval_code(timeframe)
        step = INTERVAL_MS[code]
        now = self.now()
        limit = min(end_ms or now, now - now % step)   # これより前に始まった足は閉じている
        key = ("candles", symbol, code)
        lo, hi = self.conn.execute(
            "SELECT MIN(t), MAX(t) FROM candles WHERE symbol = ? AND interval = ? AND t >= ? AND t < ?",
            (symbol, code, start_ms, limit)).fetchone()
        ranges = []
        if lo is None:
            if self._tried.get(key, limit) > start_ms:
                ranges.append((start_ms, limit))
        else:
            if lo > start_ms + step and self._tried.get(key, lo) > start_ms:
                ranges.append((start_ms, lo))
            if hi + step < limit:
                ranges.append((hi + step, limit))
        for a, b in ranges:
            got = [c for c in self.reader.candles(symbol, code, a, b) if c.t + step <= now]
            self.conn.executemany(
                "INSERT OR REPLACE INTO candles(symbol, interval, t, o, h, l, c, v) VALUES (?,?,?,?,?,?,?,?)",
                [(symbol, code, c.t, c.o, c.h, c.l, c.c, c.v) for c in got])
            self.conn.commit()
        self._tried[key] = min(self._tried.get(key, start_ms), start_ms)
        rows = self.conn.execute(
            "SELECT t, o, h, l, c, v FROM candles WHERE symbol = ? AND interval = ? AND t >= ? AND t < ? ORDER BY t",
            (symbol, code, start_ms, limit)).fetchall()
        return [Candle(*r) for r in rows]

    # --- 資金調達料(1時間ごと) ---
    def funding(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[Funding]:
        now = self.now()
        limit = min(end_ms or now, now)
        key = ("funding", symbol)
        lo, hi = self.conn.execute(
            "SELECT MIN(t), MAX(t) FROM funding WHERE symbol = ? AND t >= ? AND t < ?",
            (symbol, start_ms, limit)).fetchone()
        ranges = []
        if lo is None:
            if self._tried.get(key, limit) > start_ms:
                ranges.append((start_ms, limit))
        else:
            if lo > start_ms + HOUR_MS and self._tried.get(key, lo) > start_ms:
                ranges.append((start_ms, lo))
            if hi + HOUR_MS < limit:
                ranges.append((hi + 1, limit))
        for a, b in ranges:
            got = self.reader.funding_history(symbol, a, b)
            self.conn.executemany("INSERT OR REPLACE INTO funding(symbol, t, rate) VALUES (?,?,?)",
                                  [(symbol, f.t, f.rate) for f in got])
            self.conn.commit()
        self._tried[key] = min(self._tried.get(key, start_ms), start_ms)
        rows = self.conn.execute(
            "SELECT t, rate FROM funding WHERE symbol = ? AND t >= ? AND t < ? ORDER BY t",
            (symbol, start_ms, limit)).fetchall()
        return [Funding(*r) for r in rows]
