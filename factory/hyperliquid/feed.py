"""価格の受け取り口(SPEC 3.1): 銘柄ごとに1本。

工場が銘柄ごとに1回だけ価格(時間の単位ごと)と資金調達料を取り、全BOT(paper評価・本番)がそれを読む。
BOTごとに取引所へ取りに行かせない。取りに行くのは market.Market(取った分は帳簿に貯め、足りない分だけ取引所へ)。

使い方: 先に want() で全BOTの「どの銘柄の、どの足を、いつから」を伝え、それから get(銘柄) の Feed から読む。
want() で伝えた一番古い時刻から1回で取るので、同じ銘柄のBOTが何本あっても取引所へ行くのは1回。
`python -m factory feed` は、これを決まった間隔で回して帳簿を新しくしておくプロセス(見張り役が見る)。
"""
from __future__ import annotations

import bisect
import time
from collections.abc import Callable

from .reader import Candle, Funding, ReaderError, interval_code

HOUR_MS = 3_600_000


class Feed:
    """1銘柄の受け取り口。読むだけで、BOTからは取引所が見えない。"""

    def __init__(self, market, symbol: str):
        self.market = market
        self.symbol = symbol
        self._want_candles: dict[str, int] = {}     # 時間の単位 → 一番古い時刻
        self._want_funding: int | None = None
        self._candles: dict[str, tuple[int, list[Candle]]] = {}
        self._funding: tuple[int, list[Funding]] | None = None
        self._error: ReaderError | None = None
        self.fetches = 0                              # 取引所(帳簿)へ取りに行った回数

    def want(self, timeframe: str, start_ms: int) -> None:
        code = interval_code(timeframe)
        self._want_candles[code] = min(start_ms, self._want_candles.get(code, start_ms))
        self._want_funding = start_ms if self._want_funding is None else min(self._want_funding, start_ms)

    def _check(self) -> None:
        if self._error is not None:     # 同じ実行の中で、取れなかった銘柄を何度も取りに行かない
            raise self._error

    def candles(self, timeframe: str, start_ms: int, end_ms: int | None = None) -> list[Candle]:
        self._check()
        code = interval_code(timeframe)
        have = self._candles.get(code)
        if have is None or start_ms < have[0]:
            start = min(start_ms, self._want_candles.get(code, start_ms))
            try:
                rows = self.market.candles(self.symbol, code, start)
            except ReaderError as e:
                self._error = e
                raise
            self.fetches += 1
            have = self._candles[code] = (start, rows)
        rows = have[1]
        times = [c.t for c in rows]
        lo = bisect.bisect_left(times, start_ms)
        hi = len(rows) if end_ms is None else bisect.bisect_left(times, end_ms)
        return rows[lo:hi]

    def funding(self, start_ms: int, end_ms: int | None = None) -> list[Funding]:
        self._check()
        if self._funding is None or start_ms < self._funding[0]:
            start = start_ms if self._want_funding is None else min(start_ms, self._want_funding)
            try:
                rows = self.market.funding(self.symbol, start)
            except ReaderError as e:
                self._error = e
                raise
            self.fetches += 1
            self._funding = (start, rows)
        rows = self._funding[1]
        times = [f.t for f in rows]
        lo = bisect.bisect_left(times, start_ms)
        hi = len(rows) if end_ms is None else bisect.bisect_left(times, end_ms)
        return rows[lo:hi]

    def load(self) -> None:
        """want() で伝えた分を全部取っておく(feed のプロセス用)。"""
        for code, start in self._want_candles.items():
            self.candles(code, start)
        if self._want_funding is not None:
            self.funding(self._want_funding)


class Feeds:
    """銘柄ごとに1本の受け取り口の一覧。同じ銘柄には必ず同じ Feed を返す。"""

    def __init__(self, market):
        self.market = market
        self._feeds: dict[str, Feed] = {}

    def get(self, symbol: str) -> Feed:
        feed = self._feeds.get(symbol)
        if feed is None:
            feed = self._feeds[symbol] = Feed(self.market, symbol)
        return feed

    def want(self, symbol: str, timeframe: str, start_ms: int) -> None:
        self.get(symbol).want(timeframe, start_ms)

    def symbols(self) -> list[str]:
        return sorted(self._feeds)


def active_bots(conn) -> list:
    """価格が要るBOT(paper評価中・反証中・本番運用中)。"""
    return conn.execute(
        """SELECT b.id, b.paper_start_ms, r.symbol, r.timeframe FROM bots b JOIN rules r ON r.id = b.rule_id
           WHERE b.status IN ('paper評価中', '反証中', '本番運用中') ORDER BY b.id""").fetchall()


def refresh_once(conn, market, *, warmup_bars: int, log: Callable[[str], None] = print) -> Feeds:
    """動いているBOTの銘柄ごとに、価格と資金調達料を1回ずつ取って帳簿を新しくする。"""
    from .reader import INTERVAL_MS

    feeds = Feeds(market)
    now = market.now()
    for b in active_bots(conn):
        step = INTERVAL_MS[interval_code(b["timeframe"])]
        start = b["paper_start_ms"] or now
        feeds.want(b["symbol"], b["timeframe"], min(start, now) - warmup_bars * step)
    for symbol in feeds.symbols():
        try:
            feeds.get(symbol).load()
        except ReaderError as e:
            log(f"{symbol} を取れなかった: {e}")
    return feeds


def run_forever(*, interval_seconds: float, log: Callable[[str], None] = print, sleep=time.sleep,
                rounds: int | None = None) -> None:
    """feed のプロセス本体。決まった間隔で refresh_once を回す。"""
    from .. import config, db
    from ..market import Market
    from .reader import Reader

    reader = Reader()
    n = 0
    while rounds is None or n < rounds:
        conn = db.connect()
        try:
            feeds = refresh_once(conn, Market(conn, reader),
                                 warmup_bars=config.load_config()["paper"]["warmup_bars"], log=log)
            log(f"価格を更新した: {len(feeds.symbols())}銘柄")
            db.record_refusals(conn, "feed", reader.take_refusals())
        finally:
            conn.close()
        n += 1
        if rounds is None or n < rounds:
            sleep(interval_seconds)
