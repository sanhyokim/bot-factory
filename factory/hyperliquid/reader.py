"""Hyperliquid の読み取り窓口(SPEC 3.1): 順位表・財布の取引履歴・価格・資金調達料。

すべて公開の読み取り用API。鍵は使わない。注文は trader.py(段階4)だけが行う。
"""
from __future__ import annotations

import time
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


class ReaderError(RuntimeError):
    """取引所からデータが取れなかった。"""


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


def interval_code(timeframe: str) -> str:
    code = TIMEFRAME_ALIASES.get(timeframe.strip(), timeframe.strip())
    if code not in INTERVAL_MS:
        raise ValueError(f"時間の単位が不正: {timeframe}")
    return code


class Reader:
    def __init__(self, client: httpx.Client | None = None, *, retries: int = 3):
        cfg = config.load_config()["hyperliquid"]
        self.info_url = cfg["info_url"]
        self.leaderboard_url = cfg["leaderboard_url"]
        self.client = client or httpx.Client(timeout=cfg["timeout_seconds"])
        self.retries = retries

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
                if attempt + 1 < self.retries:
                    time.sleep(2 ** attempt)
        raise ReaderError(f"Hyperliquid から取得できなかった: {last}") from last

    def info(self, payload: dict):
        return self._request("POST", self.info_url, payload)

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
    def user_fills(self, address: str, start_ms: int, end_ms: int) -> list[Fill]:
        out: list[Fill] = []
        seen: set[tuple] = set()
        cursor = start_ms
        while cursor < end_ms:
            rows = self.info({"type": "userFillsByTime", "user": address,
                              "startTime": cursor, "endTime": end_ms}) or []
            for r in rows:
                key = (r.get("tid"), r["time"], r["px"], r["sz"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(Fill(r["coin"], int(r["time"]), float(r["px"]), float(r["sz"]), r["side"],
                                r.get("dir", ""), float(r.get("closedPnl", 0)), float(r.get("fee", 0))))
            if len(rows) < FILLS_PAGE:
                break
            last = max(int(r["time"]) for r in rows)
            cursor = last if last > cursor else cursor + 1
        out.sort(key=lambda f: f.t)
        return out


def now_ms() -> int:
    return int(time.time() * 1000)
