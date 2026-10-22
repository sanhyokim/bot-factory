"""探索の入口2: 資金調達料の歪み(SPEC 5.1)。歪み型の案を出す。AIは使わない。計算だけ。

1. 全銘柄の今の状態(直近24時間の出来高)を1回で取り、出来高100万ドル未満の薄い銘柄を外す
2. 残った銘柄ごとに直近24時間の資金調達料(1時間ごと)を取り、絶対値が 0.01% を超えた時間が3時間以上の銘柄に絞る
   (3時間は週1回の棚卸しで6時間にきつくなることがある。tuning.py の歪み型の min_hours)
3. 絞った銘柄ごとに、決まった文で案を1つ作る(型 歪み・出どころ funding・入る条件の種類 資金調達料)
4. 登録前照合(歪み型のメモとだけ。照合キーに型が入っている)を通ったものを登録する
同じ銘柄は3日間は再登録しない(照合で除外された銘柄も、3日間は作り直さない)。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from .. import config, memo, tuning
from ..hyperliquid.reader import ReaderError
from ..market import HOUR_MS, Market
from ..models import DISTORTION, Idea, now_jst

ENTRY_KIND = "資金調達料"
SOURCE = "funding"
TIMEFRAME = "1h"


@dataclass
class Candidate:
    symbol: str
    hours: int                  # 絶対値がしきい値を超えた時間
    peak: float                 # 絶対値が一番大きかった時の率(符号つき。0.0001 = 0.01%)
    day_volume_usd: float
    side: str                   # 受け取る側。率がプラスなら売り、マイナスなら買い


@dataclass
class FundingReport:
    listed: int = 0                                   # 取引所の全銘柄
    liquid: int = 0                                   # 出来高で残った銘柄
    skipped_recent: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)  # 照合で除外した時の否決メモID
    errors: int = 0
    min_hours: float = 0.0


def recent_symbols(conn: sqlite3.Connection, days: int) -> set[str]:
    """直近の日数に、入口2で案にした(または照合で除外した)銘柄。"""
    cutoff = (now_jst() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT symbol FROM ideas WHERE source = ? AND created_at >= ?
           UNION SELECT symbol FROM memos WHERE source = ? AND tag = '重複' AND created_at >= ?""",
        (SOURCE, cutoff, SOURCE, cutoff)).fetchall()
    return {r[0].casefold() for r in rows}


def summary_text(c: Candidate) -> str:
    return f"{c.symbol}の資金調達料が極端(直近24時間の最大 {c.peak * 100:+.4f}%/時)。受け取る側で入り、戻ったら出る"


def hypothesis_text(c: Candidate, threshold_pct: float) -> str:
    payer, receiver = ("買い", "売り") if c.peak > 0 else ("売り", "買い")
    return (f"直近24時間のうち{c.hours}時間、1時間あたりの資金調達料の絶対値が{threshold_pct:g}%を超えた"
            f"(最大 {c.peak * 100:+.4f}%/時。{payer}側が払っている)。{receiver}で持てば資金調達料を受け取れる。"
            "率が戻れば歪みが消えるので出る。危険は持っている間の値動きで、損切りと最長保有で守る。")


def find(conn: sqlite3.Connection, market: Market, *, skip: set[str] | None = None) -> FundingReport:
    """登録はせずに、条件に合う銘柄を探す。"""
    cfg = config.load_config()["explore_funding"]
    min_hours = tuning.current(conn, DISTORTION)["min_hours"]
    threshold = cfg["min_abs_rate_pct"] / 100
    rep = FundingReport(min_hours=min_hours)
    assets = market.reader.asset_contexts()
    rep.listed = len(assets)
    liquid = [a for a in assets if a["day_volume_usd"] >= cfg["min_day_volume_usd"]]
    rep.liquid = len(liquid)
    now = market.now()
    for a in sorted(liquid, key=lambda a: -abs(a["funding"])):
        if skip and a["name"].casefold() in skip:
            rep.skipped_recent.append(a["name"])
            continue
        try:
            rates = market.funding(a["name"], now - cfg["hours"] * HOUR_MS, now)
        except ReaderError:
            rep.errors += 1
            continue
        over = [f for f in rates if abs(f.rate) > threshold]
        if len(over) < min_hours:
            continue
        peak = max(over, key=lambda f: abs(f.rate)).rate
        side = "short" if sum(f.rate for f in over) > 0 else "long"
        rep.candidates.append(Candidate(a["name"], len(over), peak, a["day_volume_usd"], side))
    rep.candidates.sort(key=lambda c: (-c.hours, -abs(c.peak)))
    return rep


def run(conn: sqlite3.Connection, market: Market) -> FundingReport:
    cfg = config.load_config()["explore_funding"]
    rep = find(conn, market, skip=recent_symbols(conn, cfg["reregister_days"]))
    params = json.dumps({"x": cfg["min_abs_rate_pct"]}, ensure_ascii=False)
    for c in rep.candidates:
        idea_id, memo_id = memo.register_idea(conn, Idea(
            summary=summary_text(c), hypothesis=hypothesis_text(c, cfg["min_abs_rate_pct"]), source=SOURCE,
            type=DISTORTION, symbol=c.symbol, timeframe=TIMEFRAME, entry_kind=ENTRY_KIND, side=c.side,
            entry_params=params))
        if idea_id:
            rep.registered.append(idea_id)
        else:
            rep.excluded.append(memo_id)
    return rep


def describe(rep: FundingReport, *, limit: int = 10) -> list[str]:
    """開発用の確認(python -m factory funding-check)の表示。"""
    lines = [f"全銘柄 {rep.listed}・出来高で残った {rep.liquid}・3日以内に案にした {len(rep.skipped_recent)}・"
             f"条件に合った {len(rep.candidates)}(基準 {rep.min_hours:g}時間以上)"
             + (f"・取得失敗 {rep.errors}" if rep.errors else "")]
    for c in rep.candidates[:limit]:
        lines.append(f"  {c.symbol}: 超えた時間 {c.hours}・最大 {c.peak * 100:+.4f}%/時・"
                     f"受け取る側 {'売り' if c.side == 'short' else '買い'}・出来高 {c.day_volume_usd / 1e6:.1f}百万ドル")
    return lines
