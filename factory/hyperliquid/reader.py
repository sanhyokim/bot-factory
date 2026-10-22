"""Hyperliquid の読み取り窓口(SPEC 3.1): 順位表・財布の取引履歴・価格・資金調達料。

すべて公開の読み取り用API。鍵は使わない。注文は trader.py(段階4)だけが行う。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import httpx

from .. import config

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000,
    "12h": 43_200_000, "1d": 86_400_000,
}
# 売買ルールの「時間の単位」を取引所の書き方に直す
TIMEFRAME_ALIASES = {"1分": "1m", "5分": "5m", "15分": "15m", "30分": "30m", "1時間": "1h",
                     "4時間": "4h", "1日": "1d"}

CANDLE_LIMIT = 5000      # 1回で取れるローソク足の最大本数
FUNDING_PAGE = 500       # 1回で取れる資金調達料の最大件数
FILLS_PAGE = 2000        # 1回で取れる約定の最大件数

# 取引所の利用量の重さ(1分あたりの上限がある)。(1回の重さ, 返ってきた何件ごとに1足すか)
WEIGHTS = {"candleSnapshot": (20, 60), "fundingHistory": (20, 20), "userFillsByTime": (20, 20),
           "meta": (20, 0), "metaAndAssetCtxs": (20, 0)}


class ReaderError(RuntimeError):
    """取引所からデータが取れなかった。"""


class TooManyFills(ReaderError):
    """財布の約定が多すぎて、決めたページ数では期間を読み切れなかった。"""


@dataclass(frozen=True)
class Candle:
    t: int        # 足の始まり(ミリ秒, UTC)
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class Funding:
    t: int        # ミリ秒, UTC
    rate: float   # 1時間あたりの率。プラスなら買い側が払う


@dataclass(frozen=True)
class Fill:
    coin: str
    t: int
    px: float
    sz: float
    side: str             # "B"(買い)/ "A"(売り)
    direction: str        # "Open Long" など
    closed_pnl: float
    fee: float
    start_position: float = 0.0   # この約定の直前の保有数量(買いはプラス、売りはマイナス)


def interval_code(timeframe: str) -> str:
    code = TIMEFRAME_ALIASES.get(timeframe.strip(), timeframe.strip())
    if code not in INTERVAL_MS:
        raise ValueError(f"時間の単位が不正: {timeframe}")
    return code


class Reader:
    def __init__(self, client: httpx.Client | None = None, *, retries: int = 3,
                 weight_per_minute: float | None = None, clock=time.monotonic, sleep=time.sleep):
        cfg = config.load_config()["hyperliquid"]
        self.info_url = cfg["info_url"]
        self.leaderboard_url = cfg["leaderboard_url"]
        self.client = client or httpx.Client(timeout=cfg["timeout_seconds"])
        self.retries = retries
        # 1分あたりの重さの上限(取引所の上限1200より少し低くしておく)。0 なら待たない
        self.weight_per_minute = (cfg.get("weight_per_minute", 1000)
                                  if weight_per_minute is None else weight_per_minute)
        self._clock, self._sleep = clock, sleep
        self._spent: deque[tuple[float, float]] = deque()
        self.refusals = 0      # 取引所に断られた回数(429・5xx・つながらない。画面の枠6に出す)

    def _throttle(self, weight: float) -> None:
        """直近1分の重さが上限を超えないように待つ。"""
        if not self.weight_per_minute:
            return
        while True:
            now = self._clock()
            while self._spent and now - self._spent[0][0] >= 60:
                self._spent.popleft()
            used = sum(w for _, w in self._spent)
            if not self._spent or used + weight <= self.weight_per_minute:
                break
            self._sleep(60 - (now - self._spent[0][0]) + 0.05)
        self._spent.append((now, weight))

    def _request(self, method: str, url: str, payload: dict | None = None):
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                r = self.client.request(method, url, json=payload)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError("一時的なエラー", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:
                last = e
                if not isinstance(e, ValueError):
                    self.refusals += 1
                if attempt + 1 < self.retries:
                    time.sleep(2 ** attempt)
        raise ReaderError(f"Hyperliquid から取得できなかった: {last}") from last

    def take_refusals(self) -> int:
        """断られた回数を返して0に戻す(記録したら数え直す)。"""
        n, self.refusals = self.refusals, 0
        return n

    def info(self, payload: dict):
        base, per = WEIGHTS.get(payload.get("type"), (20, 0))
        self._throttle(base)
        data = self._request("POST", self.info_url, payload)
        if per and isinstance(data, list) and self.weight_per_minute:
            self._spent.append((self._clock(), len(data) / per))
        return data

    # --- 価格 ---
    def candles(self, coin: str, timeframe: str, start_ms: int, end_ms: int) -> list[Candle]:
        code = interval_code(timeframe)
        step = INTERVAL_MS[code]
        out: dict[int, Candle] = {}
        cursor = start_ms
        while cursor < end_ms:
            chunk_end = min(end_ms, cursor + step * CANDLE_LIMIT)
            rows = self.info({"type": "candleSnapshot",
                              "req": {"coin": coin, "interval": code,
                                      "startTime": cursor, "endTime": chunk_end}})
            for r in rows or []:
                c = Candle(int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]),
                           float(r["c"]), float(r["v"]))
                if start_ms <= c.t < end_ms:
                    out[c.t] = c
            cursor = chunk_end
        return [out[t] for t in sorted(out)]

    # --- 資金調達料 ---
    def funding_history(self, coin: str, start_ms: int, end_ms: int) -> list[Funding]:
        out: dict[int, Funding] = {}
        cursor = start_ms
        while cursor < end_ms:
            rows = self.info({"type": "fundingHistory", "coin": coin,
                              "startTime": cursor, "endTime": end_ms}) or []
            for r in rows:
                f = Funding(int(r["time"]), float(r["fundingRate"]))
                if f.t < end_ms:
                    out[f.t] = f
            if len(rows) < FUNDING_PAGE:
                break
            cursor = int(rows[-1]["time"]) + 1
        return [out[t] for t in sorted(out)]

    # --- 銘柄の決まり(数量の小数桁) ---
    def size_decimals(self) -> dict[str, int]:
        meta = self.info({"type": "meta"})
        return {a["name"]: int(a["szDecimals"]) for a in meta["universe"]}

    # --- 全銘柄の今の状態(探索の入口2) ---
    def asset_contexts(self) -> list[dict]:
        """無期限先物の全銘柄。各行に name, day_volume_usd(直近24時間の出来高), funding(今の1時間あたりの率), mark_px。"""
        meta, ctxs = self.info({"type": "metaAndAssetCtxs"})
        rows = []
        for a, c in zip(meta["universe"], ctxs):
            if a.get("isDelisted"):
                continue
            rows.append({"name": a["name"], "day_volume_usd": float(c.get("dayNtlVlm") or 0),
                         "funding": float(c.get("funding") or 0), "mark_px": float(c.get("markPx") or 0)})
        return rows

    # --- 順位表 ---
    def leaderboard(self) -> list[dict]:
        """成績上位の財布。各行に address, account_value, month_pnl, month_roi, month_volume。"""
        data = self._request("GET", self.leaderboard_url)
        rows = []
        for r in data.get("leaderboardRows", []):
            perf = dict(r.get("windowPerformances", []))
            month = perf.get("month", {})
            rows.append({
                "address": r["ethAddress"],
                "account_value": float(r.get("accountValue", 0)),
                "month_pnl": float(month.get("pnl", 0)),
                "month_roi": float(month.get("roi", 0)),
                "month_volume": float(month.get("vlm", 0)),
            })
        return rows

    # --- 財布の取引履歴 ---
    def user_fills(self, address: str, start_ms: int, end_ms: int, *,
                   max_pages: int | None = None) -> list[Fill]:
        """財布の約定(古い順)。max_pages を超えても読み切れない時は TooManyFills。"""
        out: list[Fill] = []
        seen: set[tuple] = set()
        cursor = start_ms
        pages = 0
        while cursor < end_ms:
            if max_pages is not None and pages >= max_pages:
                raise TooManyFills(f"{address} の約定が{max_pages * FILLS_PAGE}件を超える")
            pages += 1
            rows = self.info({"type": "userFillsByTime", "user": address,
                              "startTime": cursor, "endTime": end_ms}) or []
            for r in rows:
                key = (r.get("tid"), r["time"], r["px"], r["sz"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(Fill(r["coin"], int(r["time"]), float(r["px"]), float(r["sz"]), r["side"],
                                r.get("dir", ""), float(r.get("closedPnl", 0)), float(r.get("fee", 0)),
                                float(r.get("startPosition", 0) or 0)))
            if len(rows) < FILLS_PAGE:
                break
            last = max(int(r["time"]) for r in rows)
            cursor = last if last > cursor else cursor + 1
        out.sort(key=lambda f: f.t)
        return out


def now_ms() -> int:
    return int(time.time() * 1000)
