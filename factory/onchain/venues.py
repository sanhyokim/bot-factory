"""清算の地図(設計書 2026-11-04 の道具4「主体の分析」の広げ)。

Aave・Arbitrum の清算は、額のある機会がすべて同じブロックで取られる速さ勝負だった(11/5 の liq-timing)。
作者の言う「競争のない古い場所」を探すため、チェーンの上で清算の記録を出している仕組みを、住所を決めずに
すべて読む。知っている清算の記録の形(下の FAMILIES)のどれかを出した仕組みを、1つずつ帳簿に残す。

- どこで: 記録を出した仕組み(市場)と、それをまとめる仕組み(Compound の写しなら Comptroller、dForce の写しなら Controller)
- 誰が: 清算した主体(記録に書いてあればそれ、無ければ取引の宛先)・取引を出した財布・ガス価格
- 結果のまとめ: 仕組みごとの件数・市場の数・清算した主体の数・1位の取り分(件数で数える)

額(ドル)は、記録の形ごとに読み方が違うので、ここではまだ出さない。競争の薄い仕組みが見つかったら、
その仕組みだけ深く読む。読み取りだけで、鍵もお金も使わない。
"""
from __future__ import annotations

import hashlib
import sqlite3
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..models import JST
from . import actors
from .abi import address, decode_string, encode_call, to_int, topic, words
from .chain import Chain
from .rpc import CallReverted


@dataclass(frozen=True)
class Family:
    """清算の記録の形。liquidator・borrower は、記録のどこに書いてあるか(("topic", 番号) / ("data", 語の番号))。
    liquidator が None なら、取引の宛先(清算を呼んだ仕組み)を主体とする。"""
    name: str
    signature: str
    liquidator: tuple[str, int] | None
    borrower: tuple[str, int] | None

    @property
    def topic0(self) -> str:
        return topic(self.signature)


FAMILIES = (
    Family("Aave型", "LiquidationCall(address,address,address,uint256,uint256,address,bool)", ("data", 2), ("topic", 3)),
    Family("Compound型", "LiquidateBorrow(address,address,uint256,address,uint256)", ("data", 0), ("data", 1)),
    Family("Compound v3 の吸収", "AbsorbDebt(address,address,uint256,uint256)", ("topic", 1), ("topic", 2)),
    Family("Compound v3 の担保の買い取り", "BuyCollateral(address,address,uint256,uint256)", ("topic", 1), None),
    Family("Morpho", "Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)",
           ("topic", 2), ("topic", 3)),
    Family("Euler v2", "Liquidate(address,address,address,uint256,uint256)", ("topic", 1), ("topic", 2)),
    Family("Silo v1 ほか", "Liquidate(address,address,uint256,uint256)", None, ("topic", 2)),
)
BY_TOPIC = {f.topic0: f for f in FAMILIES}
# 読み終えた範囲の名前。記録の形を足したら名前が変わり、最初から読み直す
SCAN_KEY = "venues_" + hashlib.sha256("".join(sorted(BY_TOPIC)).encode()).hexdigest()[:8]
COMPTROLLER = "Compound型"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_venue_events (
    chain          TEXT NOT NULL,
    tx_hash        TEXT NOT NULL,
    log_index      INTEGER NOT NULL,
    family         TEXT NOT NULL,      -- 記録の形(Aave型 / Compound型 / Morpho など)
    emitter        TEXT NOT NULL,      -- 記録を出した仕組み(市場)
    block          INTEGER NOT NULL,
    ts             INTEGER NOT NULL,
    liquidator     TEXT,
    borrower       TEXT,
    sender         TEXT,               -- 取引を出した財布
    tx_to          TEXT,
    gas_price_gwei REAL,
    PRIMARY KEY (chain, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS chain_venue_events_ts ON chain_venue_events(chain, ts);
CREATE TABLE IF NOT EXISTS chain_venues (
    chain   TEXT NOT NULL,
    emitter TEXT NOT NULL,
    venue   TEXT NOT NULL,             -- まとめる仕組み(Compound型は Comptroller、ほかは emitter と同じ)
    symbol  TEXT NOT NULL,             -- 市場の名前(読めなければ空)
    PRIMARY KEY (chain, emitter)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    actors.ensure_schema(conn)
    conn.executescript(SCHEMA)


def _field(log: dict, spec: tuple[str, int] | None) -> str | None:
    """記録の決まった場所から住所を読む(無ければ None)。"""
    if spec is None:
        return None
    kind, i = spec
    try:
        if kind == "topic":
            t = log["topics"]
            return address(t[i]) if len(t) > i else None
        w = words(log["data"])
        return address(f"0x{w[i] % 2**160:064x}") if len(w) > i else None
    except (ValueError, KeyError, TypeError):
        return None


class Mapper:
    def __init__(self, conn: sqlite3.Connection, chain: Chain, *, purpose: str = "research",
                 out: Callable[[str], None] = print):
        ensure_schema(conn)
        self.conn, self.chain, self.purpose, self.out = conn, chain, purpose, out

    def _call(self, to: str, sig: str) -> str | None:
        try:
            return self.chain.call(to, encode_call(sig), purpose=self.purpose)
        except CallReverted:
            return None

    def venue(self, family: Family, emitter: str) -> tuple[str, str]:
        """記録を出した仕組みの、まとめる仕組みと名前(帳簿に残し、2回目からは読まない)。"""
        row = self.conn.execute("SELECT venue, symbol FROM chain_venues WHERE chain = ? AND emitter = ?",
                                (self.chain.name, emitter)).fetchone()
        if row:
            return row[0], row[1]
        venue = emitter
        if family.name == COMPTROLLER:
            for sig in ("comptroller()", "controller()"):      # Compound 本家の写し / dForce の写し
                got = self._call(emitter, sig)
                if got and len(got) >= 66 and int(got[2:66], 16):
                    venue = address(got[:66])
                    break
        symbol = ""
        got = self._call(emitter, "symbol()")
        if got and got != "0x":
            try:
                text = decode_string(got)
                symbol = text if text.isprintable() and len(text) <= 32 else ""
            except ValueError:
                symbol = ""
        self.conn.execute("INSERT OR REPLACE INTO chain_venues(chain, emitter, venue, symbol) VALUES (?,?,?,?)",
                          (self.chain.name, emitter, venue, symbol))
        self.conn.commit()
        return venue, symbol

    def scan(self, from_block: int, to_block: int) -> int:
        """範囲の清算の記録を、住所を決めずに読む。新しく帳簿に足した件数を返す。"""
        added = 0
        start = from_block
        windows = max(1, -(-(to_block - from_block + 1) // actors.WINDOW_BLOCKS))
        done = 0
        while start <= to_block:
            end = min(to_block, start + actors.WINDOW_BLOCKS - 1)
            logs = self.chain.logs(from_block=start, to_block=end, topics=[list(BY_TOPIC)], purpose=self.purpose)
            added += self._record(logs)
            actors.mark_progress(self.conn, self.chain.name, SCAN_KEY, from_block, end)
            done += 1
            if done % 10 == 0 and done < windows:
                self.out(f"  {done}/{windows} 区切りを読んだ(ブロック {end:,} まで、新しい記録 {added}件)")
            start = end + 1
        actors.finish_progress(self.conn, self.chain.name, SCAN_KEY, from_block)
        return added

    def _record(self, logs: list[dict]) -> int:
        new = 0
        txs: dict[str, dict] = {}
        times: dict[int, int] = {}
        for lg in logs:
            family = BY_TOPIC.get((lg.get("topics") or [None])[0])
            if family is None:
                continue
            tx_hash, log_index, block = lg["transactionHash"], to_int(lg["logIndex"]), to_int(lg["blockNumber"])
            if self.conn.execute("SELECT 1 FROM chain_venue_events WHERE chain = ? AND tx_hash = ? AND log_index = ?",
                                 (self.chain.name, tx_hash, log_index)).fetchone():
                continue
            if tx_hash not in txs:
                txs[tx_hash] = self.chain.transaction(tx_hash, purpose=self.purpose) or {}
            if block not in times:
                times[block] = to_int((self.chain.block(block, purpose=self.purpose) or {}).get("timestamp", "0x0"))
            tx = txs[tx_hash]
            emitter = address(lg["address"])
            self.venue(family, emitter)
            to = address(tx["to"]) if tx.get("to") else None
            self.conn.execute(
                """INSERT OR IGNORE INTO chain_venue_events(chain, tx_hash, log_index, family, emitter, block, ts,
                       liquidator, borrower, sender, tx_to, gas_price_gwei) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.chain.name, tx_hash, log_index, family.name, emitter, block, times[block],
                 _field(lg, family.liquidator) or to, _field(lg, family.borrower),
                 address(tx["from"]) if tx.get("from") else None, to,
                 to_int(tx.get("gasPrice") or "0x0") / 1e9))
            self.conn.commit()
            new += 1
        return new


# --- まとめ ---
def summarize(conn: sqlite3.Connection, chain: str, since_ts: int) -> list[dict]:
    """仕組み(まとめる仕組み)ごとのまとめ。件数の多い順。"""
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT e.*, v.venue, v.symbol FROM chain_venue_events e
           LEFT JOIN chain_venues v ON v.chain = e.chain AND v.emitter = e.emitter
           WHERE e.chain = ? AND e.ts >= ? ORDER BY e.ts""", (chain, since_ts)).fetchall()
    by: dict[tuple[str, str], list] = {}
    for r in rows:
        by.setdefault((r["family"], r["venue"] or r["emitter"]), []).append(r)
    out = []
    for (family, venue), rs in by.items():
        who = Counter(r["liquidator"] for r in rs if r["liquidator"])
        top, top_n = who.most_common(1)[0] if who else (None, 0)
        symbols = Counter(r["symbol"] for r in rs if r["symbol"])
        out.append({
            "family": family, "venue": venue, "count": len(rs), "markets": len({r["emitter"] for r in rs}),
            "symbols": [s for s, _ in symbols.most_common(3)], "liquidators": len(who), "top": top,
            "top_share": top_n / len(rs), "senders": len({r["sender"] for r in rs if r["sender"]}),
            "borrowers": len({r["borrower"] for r in rs if r["borrower"]}),
            "median_gwei": statistics.median(r["gas_price_gwei"] or 0.0 for r in rs),
            "first_ts": rs[0]["ts"], "last_ts": rs[-1]["ts"],
        })
    out.sort(key=lambda v: v["count"], reverse=True)
    return out


def describe(summary: list[dict], *, chain: str, days: float, top: int = 20) -> list[str]:
    total = sum(v["count"] for v in summary)
    out = [f"清算の地図({chain}・直近{days:g}日、清算の記録 {total}件、仕組み {len(summary)}か所)"]
    if not summary:
        return out + ["  清算の記録は見つからなかった"]
    fam = Counter()
    places = Counter()
    for v in summary:
        fam[v["family"]] += v["count"]
        places[v["family"]] += 1
    out.append("  形ごと: " + "、".join(f"{f} {n}件({places[f]}か所)" for f, n in fam.most_common()))
    out.append("  順位 形 仕組み(市場の名前) 件数 市場 清算した主体 1位の取り分(件数) 財布 借り手 ガス価格の中央値 最後")
    for i, v in enumerate(summary[:top], 1):
        names = "・".join(v["symbols"]) or "名前なし"
        last = datetime.fromtimestamp(v["last_ts"], JST).strftime("%m/%d %H:%M")
        lead = f"{v['top_share']:.0%}({v['top']})" if v["top"] else "-"
        out.append(f"  {i:>3}. {v['family']}  {v['venue']}({names})  {v['count']}件  {v['markets']}市場  "
                   f"{v['liquidators']}主体  1位 {lead}  {v['senders']}財布  {v['borrowers']}人  "
                   f"{v['median_gwei']:.3g}gwei  {last}")
    if len(summary) > top:
        out.append(f"  ほか {len(summary) - top}か所(件数 {sum(v['count'] for v in summary[top:])}件)")
    return out
