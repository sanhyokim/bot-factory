"""取られるまでの速さ(設計書 2026-11-04 の道具6「機会の模擬」の1つめ)。第0期は Aave v3 の清算。

帳簿にある過去の清算1件ずつについて、借り手の健康度が1を割ったブロックを、古いブロックの状態から探す。
1件ごとに次を帳簿 chain_liquidation_timing に残す。
- 割れたブロックと、そこから清算までのブロック数・秒数
- 割れた理由
  - 値段: 担保か借金の値段が動いて割れた(速さ勝負になりやすい)
  - 利息: 値段は動かず、借金の利息が積もって割れた(割れる時刻が前もって計算できる。作者が狙った型)
  - 続き: 同じ口座の前の清算の後も、1未満のままだった(同じ機会の残り)
  - 長く1未満: さかのぼれる所まで、ずっと1未満だった(小さすぎて誰も取らなかった口座など)
- 割れた時の健康度・担保・借金

清算と同じブロックの中で割れた分は、そのブロックの前後で、清算された担保と借金の値段が動いたかだけを見る
(ほかの持ち物の値段で割れた場合は「利息」に入ってしまう。持ち物が1〜2種類の口座が多いので、粗い見分け)。

探し方: 清算の1つ前のブロックから、1・2・4・8…ブロックずつ前に戻り、健康度が1以上の所を見つける。
その間を半分ずつに分けて、1以上から1未満に変わったブロックを探す(1件あたり、遅れのブロック数の
2倍の対数くらいの回数を読む)。途中で健康度が上下していれば、見つかるのはそのうちの1つの変わり目。
読み取りだけ(古いブロックの状態を読める接続先が要る)。鍵もお金も使わない。
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .. import config
from ..models import JST, now_jst
from . import actors
from .abi import address, encode_call, to_int, words
from .chain import Chain
from .rpc import CallReverted

PROTOCOL = "aave_v3"
CAUSES = ("値段", "利息", "続き", "長く1未満")
PRICE_MOVE = 1e-4            # 担保・借金の合計がこの割合より動いたら「値段で割れた」とみなす(利息は1ブロックで1e-9ほど)
SPEED_BUCKETS = ((0, "同じブロック"), (1, "1秒以内"), (10, "10秒以内"), (60, "1分以内"), (3600, "1時間以内"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_liquidation_timing (
    chain          TEXT NOT NULL,
    tx_hash        TEXT NOT NULL,
    log_index      INTEGER NOT NULL,
    borrower       TEXT NOT NULL,
    liq_block      INTEGER NOT NULL,
    cross_block    INTEGER,            -- 健康度が1を割ったブロック(長く1未満なら NULL)
    delay_blocks   INTEGER,            -- 清算のブロック − cross_block
    delay_seconds  INTEGER,
    cause          TEXT NOT NULL,      -- 値段 / 利息 / 続き / 長く1未満
    health         REAL,               -- 割れた時の健康度
    collateral_usd REAL,               -- 割れた時の担保
    debt_usd       REAL,               -- 割れた時の借金
    searched_back  INTEGER NOT NULL,   -- さかのぼったブロック数
    reads          INTEGER NOT NULL,   -- 使った読み取りの回数(キャッシュから返った分も含む)
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (chain, tx_hash, log_index)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    actors.ensure_schema(conn)
    conn.executescript(SCHEMA)


@dataclass
class Account:
    collateral: float     # ドル(Aave の単位 1e8)
    debt: float
    health: float         # 借金0なら inf


@dataclass
class Timing:
    cross_block: int | None
    delay_blocks: int | None
    delay_seconds: int | None
    cause: str
    health: float | None
    collateral_usd: float | None
    debt_usd: float | None
    searched_back: int
    reads: int


class Timer:
    def __init__(self, conn: sqlite3.Connection, chain: Chain, *, pool: str, max_back: int = 2_419_200,
                 purpose: str = "research", out: Callable[[str], None] = print):
        ensure_schema(conn)
        self.conn, self.chain, self.purpose, self.out = conn, chain, purpose, out
        self.pool = address(pool)
        self.max_back = max_back
        self._oracle: tuple[str, int] | None = None
        self._reads = 0

    @classmethod
    def from_config(cls, conn, chain: Chain, **kw) -> Timer:
        cfg = config.load_config()["onchain"]
        p = cfg["chains"][chain.name]["protocols"]
        max_back = cfg.get("timing", {}).get("max_back_blocks", 2_419_200)
        return cls(conn, chain, pool=p["aave_v3_pool"], max_back=max_back, **kw)

    # --- 読み取り ---
    def account(self, user: str, block: int) -> Account:
        """そのブロックの後の、借り手の担保・借金・健康度。"""
        data = self._call(self.pool, encode_call("getUserAccountData(address)", user), block)
        coll, debt, _avail, _lt, _ltv, hf = words(data)[:6]
        return Account(coll / 1e8, debt / 1e8, hf / 1e18 if debt else float("inf"))

    def _call(self, to: str, data: str, block: int | str = "latest") -> str:
        self._reads += 1
        return self.chain.call(to, data, block=block, purpose=self.purpose)

    def oracle(self) -> tuple[str, int]:
        """Aave の値段表の住所と、値段の単位(USD建てなら 1e8)。"""
        if self._oracle is None:
            provider = address(self._call(self.pool, encode_call("ADDRESSES_PROVIDER()")))
            oracle = address(self._call(provider, encode_call("getPriceOracle()")))
            self._oracle = (oracle, words(self._call(oracle, encode_call("BASE_CURRENCY_UNIT()")))[0])
        return self._oracle

    def _price(self, asset: str, block: int) -> float | None:
        oracle, unit = self.oracle()
        try:
            return words(self._call(oracle, encode_call("getAssetPrice(address)", asset), block))[0] / unit
        except CallReverted:
            return None

    def _timestamp(self, block: int) -> int:
        self._reads += 1
        return to_int(self.chain.block(block, purpose=self.purpose)["timestamp"])

    # --- 1件 ---
    def measure(self, liq: sqlite3.Row) -> Timing:
        """清算1件の「割れたブロック・遅れ・理由」を探す。"""
        self._reads = 0
        user, b = liq["borrower"], liq["block"]
        prev = self.conn.execute(
            """SELECT MAX(block) FROM chain_liquidations WHERE chain = ? AND borrower = ?
               AND (block < ? OR (block = ? AND log_index < ?))""",
            (self.chain.name, user, b, b, liq["log_index"])).fetchone()[0]
        if prev == b:                    # 同じブロックの前の清算(同じ取引で担保を2種類取ったなど)
            return Timing(b, 0, 0, "続き", None, None, None, 0, self._reads)
        floor = max(0, b - self.max_back) if prev is None else prev
        hi, at_hi = b - 1, self.account(user, b - 1)
        if at_hi.health >= 1:
            # 1つ前のブロックではまだ1以上: 清算と同じブロックの中で割れた(値段の更新のすぐ後ろに並んだか、
            # 割れる時刻を前もって計算して、その最初のブロックに入れたか)
            moved = any(self._price(a, b - 1) != self._price(a, b) for a in {liq["collateral"], liq["debt"]})
            return Timing(b, 0, 0, "値段" if moved else "利息", None, at_hi.collateral, at_hi.debt, 1, self._reads)
        # 1・2・4…ブロックずつ戻って、1以上の所を探す
        step, lo, at_lo = 1, None, None
        while lo is None:
            x = max(floor, hi - step)
            acc = self.account(user, x)
            if acc.health >= 1:
                lo, at_lo = x, acc
                break
            hi, at_hi = x, acc
            if x == floor:
                back = b - floor
                if prev is not None:
                    ts = self._timestamp(prev)
                    return Timing(prev, b - prev, liq["ts"] - ts, "続き", acc.health, acc.collateral, acc.debt,
                                  back, self._reads)
                return Timing(None, None, None, "長く1未満", acc.health, acc.collateral, acc.debt, back, self._reads)
            step *= 2
        # lo(1以上)と hi(1未満)の間を半分ずつに分ける
        while hi - lo > 1:
            mid = (lo + hi) // 2
            acc = self.account(user, mid)
            if acc.health >= 1:
                lo, at_lo = mid, acc
            else:
                hi, at_hi = mid, acc
        cause = "値段" if _moved(at_lo, at_hi) else "利息"
        ts = self._timestamp(hi)
        return Timing(hi, b - hi, liq["ts"] - ts, cause, at_hi.health, at_hi.collateral, at_hi.debt, b - lo,
                      self._reads)

    def run(self, since_ts: int, *, progress_every: int = 10) -> dict:
        """まだ測っていない清算を古い順に測る。数のまとめを返す。"""
        rows = self.conn.execute(
            """SELECT l.* FROM chain_liquidations l
               LEFT JOIN chain_liquidation_timing t
                 ON t.chain = l.chain AND t.tx_hash = l.tx_hash AND t.log_index = l.log_index
               WHERE l.chain = ? AND l.protocol = ? AND l.ts >= ? AND t.tx_hash IS NULL
               ORDER BY l.block, l.log_index""", (self.chain.name, PROTOCOL, since_ts)).fetchall()
        done = failed = reads = 0
        for i, liq in enumerate(rows, 1):
            try:
                t = self.measure(liq)
            except CallReverted:          # 健康度が読めない(起きないはず)。この1件は飛ばす
                failed += 1
                continue
            reads += t.reads
            self.conn.execute(
                """INSERT OR REPLACE INTO chain_liquidation_timing(chain, tx_hash, log_index, borrower, liq_block,
                       cross_block, delay_blocks, delay_seconds, cause, health, collateral_usd, debt_usd,
                       searched_back, reads, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.chain.name, liq["tx_hash"], liq["log_index"], liq["borrower"], liq["block"], t.cross_block,
                 t.delay_blocks, t.delay_seconds, t.cause, t.health, t.collateral_usd, t.debt_usd, t.searched_back,
                 t.reads, now_jst().isoformat(timespec="seconds")))
            self.conn.commit()
            done += 1
            if progress_every and i % progress_every == 0:
                self.out(f"  {i}/{len(rows)}件を測った(読み取り {reads:,}回)")
        return {"todo": len(rows), "done": done, "failed": failed, "reads": reads}


def _moved(a: Account, b: Account) -> bool:
    """2つのブロックの間で、担保か借金の合計が利息では説明できないほど動いたか。"""
    def rel(x: float, y: float) -> float:
        return abs(y - x) / max(abs(x), 1e-9)
    return rel(a.collateral, b.collateral) > PRICE_MOVE or rel(a.debt, b.debt) > PRICE_MOVE


# --- まとめ ---
def summarize(conn: sqlite3.Connection, chain: str, since_ts: int) -> dict:
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT t.*, l.liquidator, l.net_usd, l.ts FROM chain_liquidation_timing t
           JOIN chain_liquidations l ON l.chain = t.chain AND l.tx_hash = t.tx_hash AND l.log_index = t.log_index
           WHERE t.chain = ? AND l.ts >= ? ORDER BY l.block, l.log_index""", (chain, since_ts)).fetchall()
    first = [r for r in rows if r["cause"] in ("値段", "利息")]      # 新しく割れた機会(続き・長く1未満を除く)
    speed = Counter(_bucket(r) for r in first)
    by_cause = {c: [r for r in rows if r["cause"] == c] for c in CAUSES}
    actors_: dict[str, list] = {}
    for r in first:
        actors_.setdefault(r["liquidator"], []).append(r)
    return {"rows": rows, "first": first, "speed": speed, "by_cause": by_cause, "by_actor": actors_}


def _bucket(r) -> str:
    """遅れの区切り。ブロックの時刻は秒単位で、1秒に4ブロックほど並ぶので、同じブロックかはブロック数で見る。"""
    if r["delay_blocks"] == 0:
        return "同じブロック"
    for limit, name in SPEED_BUCKETS[1:]:
        if r["delay_seconds"] <= limit:
            return name
    return "1時間より後"


def _median(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _usd(rows) -> float:
    return sum(r["net_usd"] or 0.0 for r in rows)


def describe(s: dict, *, chain: str, days: float, top: int = 10) -> list[str]:
    rows, first = s["rows"], s["first"]
    out = [f"取られるまでの速さ(Aave v3・{chain}・直近{days:g}日の清算 {len(rows)}件)"]
    if not rows:
        return out + ["  測った清算はまだ無い(先に actor-scan で清算の記録を読む)"]
    out.append("  割れた理由: " + "、".join(
        f"{c} {len(s['by_cause'][c])}件(純利益 ${_usd(s['by_cause'][c]):,.2f})" for c in CAUSES))
    if first:
        names = [n for _, n in SPEED_BUCKETS] + ["1時間より後"]
        out.append("  割れてから取られるまで(値段・利息の分): "
                   + "、".join(f"{n} {s['speed'].get(n, 0)}件" for n in names))
        for c in ("値段", "利息"):
            rs = s["by_cause"][c]
            if rs:
                mb, ms = _median([r["delay_blocks"] for r in rs]), _median([r["delay_seconds"] for r in rs])
                out.append(f"    {c}で割れた分の中央値: {mb:g}ブロック・{ms:g}秒")
        out.append("  主体ごと(値段・利息の分。純利益の多い順)")
        ranked = sorted(s["by_actor"].items(), key=lambda kv: _usd(kv[1]), reverse=True)
        for who, rs in ranked[:top]:
            causes = Counter(r["cause"] for r in rs)
            ms = _median([r["delay_seconds"] for r in rs])
            same = sum(1 for r in rs if r["delay_blocks"] == 0)
            out.append(f"    {who}  {len(rs)}件 純利益 ${_usd(rs):,.2f}  遅れの中央値 {ms:g}秒"
                       f"(同じブロック {same}件)  値段 {causes['値段']}件・利息 {causes['利息']}件")
    interest = s["by_cause"]["利息"]
    if interest:
        out.append("  利息で割れた清算(割れる時刻を前もって計算できた機会)")
        for r in interest[:top]:
            when = datetime.fromtimestamp(r["ts"], JST).strftime("%m/%d %H:%M")
            debt = f"${r['debt_usd']:,.2f}" if r["debt_usd"] is not None else "?"
            out.append(f"    {when}  ブロック {r['liq_block']:,}  {r['liquidator']}  遅れ {r['delay_blocks']}ブロック・"
                       f"{r['delay_seconds']}秒  借金 {debt}  純利益 ${r['net_usd'] or 0:,.2f}")
    return out
