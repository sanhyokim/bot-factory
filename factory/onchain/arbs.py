"""裁定で稼いでいる主体を探す(設計書 2026-11-04 の道具4「主体の分析」の残り)。

清算は、Arbitrum では額のある機会が全部同じブロックで取られる速さ勝負で、競争のない古い仕組みにはお金が
無かった(11/5)。作者の稼ぎのもう1つの柱は「アトミック裁定」(1回の取引の中で、いくつかの交換の場を回って
元のトークンに戻り、増えた分を取る)。住所を決めずに交換の記録を読み、1回の取引の中で元に戻る交換を探す。

- 何を: 交換の記録(Uniswap V2 型・V3 型・PancakeSwap V3・Balancer・Velodrome 型)。量が多いので、読んだ記録は
  キャッシュに残さない
- 裁定の見分け方: 取引の中の交換をすべて足して、どのトークンも減っておらず、どれかが増えていれば裁定。
  ふつうの人の交換(A を出して B を受け取る)は A が減るので入らない。清算のついでの交換も入らない
- 誰が: 交換の場を呼んだ仕組み(交換の記録の sender)。財布・ガス代・優先レーンは、上位の主体の取引だけ
  受領書を読んで確かめる(全部読むと回線が足りない)
- いくら: 増えたトークンを、Aave の値段表で1時間ごとの値段にしてドルにする(粗利。ガス代は上の確かめた分だけ)
- 速さ: その裁定の交換の場で、直前に誰かが交換したブロックからの遅れ。同じブロックなら「値段を動かした交換の
  すぐ後ろに並べた」速さ勝負の形

失敗して取り消された裁定(記録が残らない)のガス代は見えない。読み取りだけで、鍵もお金も使わない。
"""
from __future__ import annotations

import hashlib
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .. import config
from ..models import JST
from . import actors
from .abi import (
    address,
    decode_aggregate3,
    encode_aggregate3,
    encode_call,
    to_int,
    topic,
    words,
)
from .chain import Chain
from .rpc import AllBusy, CallReverted

KINDS = {
    topic("Swap(address,uint256,uint256,uint256,uint256,address)"): "V2",
    topic("Swap(address,address,int256,int256,uint160,uint128,int24)"): "V3",
    topic("Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)"): "PancakeV3",
    topic("Swap(bytes32,address,address,uint256,uint256)"): "Balancer",
    topic("Swap(address,address,uint256,uint256,uint256,uint256)"): "Solidly",   # Velodrome・Aerodrome など
}
# 読み終えた範囲の名前。交換の記録の形を足したら名前が変わり、最初から読み直す
SCAN_KEY = "arbs_" + hashlib.sha256("".join(sorted(KINDS)).encode()).hexdigest()[:8]
V2_LIKE = ("V2", "Solidly")             # 答えが (amount0In, amount1In, amount0Out, amount1Out) の形
PRICE_BUCKET = 14_400          # 値段は約1時間(0.25秒 × 14,400)ごとに読む
TOLERANCE = 10_000             # 途中のトークンが、通った量の 1/10,000 より減っていれば裁定とみなさない

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_pools (
    chain   TEXT NOT NULL,
    pool    TEXT NOT NULL,
    token0  TEXT NOT NULL,             -- 読めなければ空
    token1  TEXT NOT NULL,
    PRIMARY KEY (chain, pool)
);
CREATE TABLE IF NOT EXISTS chain_arbs (
    chain          TEXT NOT NULL,
    tx_hash        TEXT NOT NULL,
    block          INTEGER NOT NULL,
    ts             INTEGER NOT NULL,   -- 区切りの始めと終わりのブロックの時刻から割り出した目安
    actor          TEXT NOT NULL,      -- 交換の場を呼んだ仕組み(交換の記録の sender)
    swaps          INTEGER NOT NULL,
    kinds          TEXT NOT NULL,      -- 交換の場の種類(例: V3,V3,V2)
    pools          TEXT NOT NULL,
    path           TEXT NOT NULL,      -- 通ったトークンの名前(例: WETH>USDC>WETH)
    profit_token   TEXT NOT NULL,
    profit_amount  TEXT NOT NULL,      -- 最小単位の整数(文字で残す)
    gross_usd      REAL,               -- 値段が読めなければ NULL
    trigger_block  INTEGER,            -- 同じ交換の場で直前に誰かが交換したブロック(読んだ範囲に無ければ NULL)
    trigger_seconds INTEGER,
    sampled        INTEGER NOT NULL DEFAULT 0,   -- 受領書を読んだか
    sender         TEXT,               -- 取引を出した財布(受領書を読んだ分だけ)
    tx_to          TEXT,
    tx_index       INTEGER,
    gas_usd        REAL,
    timeboosted    INTEGER,
    PRIMARY KEY (chain, tx_hash)
);
CREATE INDEX IF NOT EXISTS chain_arbs_actor ON chain_arbs(chain, actor, ts);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    actors.ensure_schema(conn)
    conn.executescript(SCHEMA)


def _signed(x: int) -> int:
    return x - 2**256 if x >= 2**255 else x


@dataclass
class Swap:
    """交換1回。deltas は交換した側から見た増減(V2・V3 は (token0, token1) の順、Balancer はトークンつき)。"""
    tx: str
    block: int
    log_index: int
    kind: str
    pool: str
    sender: str | None
    deltas: tuple[int, int] = (0, 0)
    tokens: tuple[str, str] | None = None      # Balancer は記録に書いてある


def parse_swap(log: dict) -> Swap | None:
    """交換の記録を読む。形の違う記録(同じ名前で引数の並びが違う仕組みなど)は None。"""
    kind = KINDS.get((log.get("topics") or [None])[0])
    if kind is None:
        return None
    try:
        return _parse(log, kind)
    except (IndexError, ValueError, KeyError):
        return None


def _parse(log: dict, kind: str) -> Swap | None:
    t, w = log["topics"], words(log["data"])
    if len(t) < (4 if kind == "Balancer" else 3) or len(w) < (4 if kind in V2_LIKE else 2):
        return None
    base = {"tx": log["transactionHash"], "block": to_int(log["blockNumber"]), "log_index": to_int(log["logIndex"]),
            "kind": kind}
    if kind == "Balancer":
        return Swap(**base, pool=t[1].lower(), sender=None, deltas=(-w[0], w[1]),
                    tokens=(address(t[2]), address(t[3])))
    pool, sender = address(log["address"]), address(t[1])
    if kind in V2_LIKE:
        return Swap(**base, pool=pool, sender=sender, deltas=(w[2] - w[0], w[3] - w[1]))
    return Swap(**base, pool=pool, sender=sender, deltas=(-_signed(w[0]), -_signed(w[1])))


@dataclass
class Arb:
    tx: str
    block: int
    actor: str
    swaps: list[Swap]
    net: dict[str, int]
    path: list[str] = field(default_factory=list)


def find_arb(swaps: list[Swap], pools: dict[str, tuple[str, str]]) -> Arb | None:
    """1つの取引の交換から、裁定(どのトークンも減らず、どれかが増える)なら Arb を返す。"""
    if len(swaps) < 2:
        return None
    net: dict[str, int] = defaultdict(int)
    through: dict[str, int] = defaultdict(int)
    path: list[str] = []
    for s in swaps:
        tokens = s.tokens or pools.get(s.pool)
        if not tokens or not tokens[0]:
            return None
        for tok, d in zip(tokens, s.deltas):
            net[tok] += d
            through[tok] += abs(d)
        given = tokens[0] if s.deltas[0] < 0 else tokens[1]
        if not path or path[-1] != given:
            path.append(given)
    last = swaps[-1]
    got = (last.tokens or pools[last.pool])
    path.append(got[1] if last.deltas[1] > 0 else got[0])
    if any(n < -(through[t] // TOLERANCE) for t, n in net.items()) or not any(n > 0 for n in net.values()):
        return None
    actor = next((s.sender for s in swaps if s.sender), "")
    return Arb(tx=swaps[0].tx, block=swaps[0].block, actor=actor, swaps=swaps,
               net={t: n for t, n in net.items() if n}, path=path)


class ArbScanner:
    def __init__(self, conn: sqlite3.Connection, chain: Chain, *, scanner: actors.Scanner,
                 multicall: str | None = None, log_blocks: int = 500, purpose: str = "research",
                 out: Callable[[str], None] = print):
        ensure_schema(conn)
        self.conn, self.chain, self.scanner, self.purpose, self.out = conn, chain, scanner, purpose, out
        self.multicall = address(multicall) if multicall else None
        self.log_blocks = log_blocks
        self._pools: dict[str, tuple[str, str]] = {}
        self._prices: dict[tuple[str, int], float | None] = {}
        self.last_swap: dict[str, tuple[int, int]] = {}      # 交換の場 → (ブロック, 時刻)
        self.swaps_seen = 0
        self.txs_seen = 0

    @classmethod
    def from_config(cls, conn, chain: Chain, **kw) -> ArbScanner:
        cfg = config.load_config()["onchain"]
        a = cfg.get("arbs", {})
        return cls(conn, chain, scanner=actors.Scanner.from_config(conn, chain, out=kw.get("out", print)),
                   multicall=cfg["chains"][chain.name]["protocols"].get("multicall3"),
                   log_blocks=cfg["chains"][chain.name].get("arb_log_blocks", a.get("log_blocks", 500)), **kw)

    # --- 交換の場のトークン ---
    def pool_tokens(self, pools: list[str]) -> dict[str, tuple[str, str]]:
        """V2・V3 型の交換の場の (token0, token1)。帳簿に残し、2回目からは読まない。読めなければ ("", "")。"""
        need = [p for p in dict.fromkeys(pools) if p not in self._pools]
        if need:
            q = ",".join("?" * len(need))
            for pool, t0, t1 in self.conn.execute(
                    f"SELECT pool, token0, token1 FROM chain_pools WHERE chain = ? AND pool IN ({q})",
                    (self.chain.name, *need)):
                self._pools[pool] = (t0, t1)
        need = [p for p in need if p not in self._pools]
        for i in range(0, len(need), 100):
            chunk = need[i:i + 100]
            found = self._read_tokens(chunk)
            for p in chunk:
                self._pools[p] = found.get(p, ("", ""))
            self.conn.executemany("INSERT OR REPLACE INTO chain_pools(chain, pool, token0, token1) VALUES (?,?,?,?)",
                                  [(self.chain.name, p, *self._pools[p]) for p in chunk])
            self.conn.commit()
        return {p: self._pools[p] for p in pools}

    def _read_tokens(self, pools: list[str]) -> dict[str, tuple[str, str]]:
        calls = [(p, encode_call(sig)) for p in pools for sig in ("token0()", "token1()")]
        answers: list[tuple[bool, str]] | None = None
        if self.multicall:
            try:
                answers = decode_aggregate3(self.chain.call(self.multicall, encode_aggregate3(calls),
                                                            purpose=self.purpose))
            except (CallReverted, ValueError):
                answers = None
        if answers is None:
            answers = []
            for to, data in calls:
                try:
                    answers.append((True, self.chain.call(to, data, purpose=self.purpose)))
                except CallReverted:
                    answers.append((False, "0x"))
        out = {}
        for k, p in enumerate(pools):
            (ok0, d0), (ok1, d1) = answers[2 * k], answers[2 * k + 1]
            try:
                if ok0 and ok1 and len(d0) >= 66 and len(d1) >= 66:
                    out[p] = (address(d0[:66]), address(d1[:66]))
            except ValueError:
                continue
        return out

    # --- 値段 ---
    def price(self, token: str, block: int) -> float | None:
        """1トークンあたりのドル(約1時間ごとの値段。Aave の値段表に無ければ None)。"""
        b = block - block % PRICE_BUCKET
        key = (token, b)
        if key not in self._prices:
            try:
                self._prices[key], _ = self.scanner.price(token, b)
            except AllBusy:
                raise
            except CallReverted:
                self._prices[key] = None
        return self._prices[key]

    def token(self, token: str) -> tuple[str, int] | None:
        try:
            return self.scanner.token(token)
        except (CallReverted, ValueError, IndexError):
            return None

    def usd(self, token: str, amount: int, block: int) -> float | None:
        meta, px = self.token(token), self.price(token, block)
        return amount / 10 ** meta[1] * px if meta and px is not None else None

    # --- 読む ---
    def _timestamp(self, block: int) -> int:
        return to_int((self.chain.block(block, purpose=self.purpose) or {}).get("timestamp", "0x0"))

    def scan(self, from_block: int, to_block: int, *, progress_every: int = 20) -> int:
        """範囲の交換の記録を読み、裁定を帳簿に足す。新しく足した件数を返す。"""
        added = 0
        start = from_block
        chunks = max(1, -(-(to_block - from_block + 1) // self.log_blocks))
        done = 0
        while start <= to_block:
            end = min(to_block, start + self.log_blocks - 1)
            logs = self.chain.logs(from_block=start, to_block=end, topics=[list(KINDS)], purpose=self.purpose,
                                   cache=False)
            ts0, ts1 = self._timestamp(start), self._timestamp(end)
            added += self._record(logs, start, end, ts0, ts1)
            actors.mark_progress(self.conn, self.chain.name, SCAN_KEY, from_block, end)
            done += 1
            if progress_every and done % progress_every == 0 and done < chunks:
                self.out(f"  {done}/{chunks} 区切りを読んだ(ブロック {end:,} まで、交換 {self.swaps_seen:,}回、"
                         f"裁定 {added:,}件)")
            start = end + 1
        actors.finish_progress(self.conn, self.chain.name, SCAN_KEY, from_block)
        return added

    def _record(self, logs: list[dict], start: int, end: int, ts0: int, ts1: int) -> int:
        def ts(b: int) -> int:
            return ts0 if end == start else ts0 + round((b - start) * (ts1 - ts0) / (end - start))

        swaps = [s for s in (parse_swap(lg) for lg in logs) if s]
        swaps.sort(key=lambda s: (s.block, s.log_index))
        self.swaps_seen += len(swaps)
        by_tx: dict[str, list[Swap]] = {}
        for s in swaps:
            by_tx.setdefault(s.tx, []).append(s)
        self.txs_seen += len(by_tx)
        multi = [p for ss in by_tx.values() if len(ss) > 1 for p in (s.pool for s in ss if not s.tokens)]
        pools = self.pool_tokens(multi) if multi else {}
        new = 0
        for tx, ss in by_tx.items():         # 取引の並び(ブロック・記録の順)
            arb = find_arb(ss, pools)
            if arb is not None and not self.conn.execute(
                    "SELECT 1 FROM chain_arbs WHERE chain = ? AND tx_hash = ?", (self.chain.name, tx)).fetchone():
                self._save(arb, ts(arb.block))
                new += 1
            for s in ss:
                self.last_swap[s.pool] = (s.block, ts(s.block))
        self.conn.commit()
        return new

    def _save(self, arb: Arb, at: int) -> None:
        trig = [self.last_swap[s.pool] for s in arb.swaps if s.pool in self.last_swap]
        trigger = max(trig) if trig else None
        values = {t: self.usd(t, n, arb.block) for t, n in arb.net.items()}
        gains = {t: n for t, n in arb.net.items() if n > 0}
        profit_token = max(gains, key=lambda t: (values[t] or 0.0, gains[t]))
        gross = None if values[profit_token] is None else sum(v for v in values.values() if v is not None)
        names = [(self.token(t) or (t[:10], 0))[0] for t in arb.path]
        self.conn.execute(
            """INSERT OR IGNORE INTO chain_arbs(chain, tx_hash, block, ts, actor, swaps, kinds, pools, path,
                   profit_token, profit_amount, gross_usd, trigger_block, trigger_seconds)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.chain.name, arb.tx, arb.block, at, arb.actor, len(arb.swaps),
             ",".join(s.kind for s in arb.swaps), ",".join(s.pool for s in arb.swaps), ">".join(names),
             profit_token, str(gains[profit_token]), gross,
             trigger[0] if trigger else None, at - trigger[1] if trigger else None))

    # --- 上位の主体の受領書 ---
    def sample(self, since_ts: int, *, actors_n: int = 15, per_actor: int = 20) -> int:
        """粗利の多い主体ごとに、粗利の大きい裁定の受領書を読んで、財布・ガス代・優先レーンを確かめる。"""
        top = [a for (a,) in self.conn.execute(
            """SELECT actor FROM chain_arbs WHERE chain = ? AND ts >= ? GROUP BY actor
               ORDER BY SUM(COALESCE(gross_usd, 0)) DESC LIMIT ?""", (self.chain.name, since_ts, actors_n))]
        read = 0
        for a in top:
            rows = self.conn.execute(
                """SELECT tx_hash, block FROM chain_arbs WHERE chain = ? AND actor = ? AND ts >= ? AND sampled = 0
                   ORDER BY COALESCE(gross_usd, 0) DESC LIMIT ?""",
                (self.chain.name, a, since_ts, max(0, per_actor - self._sampled(a, since_ts)))).fetchall()
            for tx, block in rows:
                rc = self.chain.receipt(tx, purpose=self.purpose) or {}
                eth = self.price(self.scanner.weth, block)
                gas_wei = to_int(rc.get("gasUsed", "0x0")) * to_int(rc.get("effectiveGasPrice") or "0x0")
                self.conn.execute(
                    """UPDATE chain_arbs SET sampled = 1, sender = ?, tx_to = ?, tx_index = ?, gas_usd = ?,
                           timeboosted = ? WHERE chain = ? AND tx_hash = ?""",
                    (address(rc["from"]) if rc.get("from") else None, address(rc["to"]) if rc.get("to") else None,
                     to_int(rc["transactionIndex"]) if rc.get("transactionIndex") else None,
                     gas_wei / 1e18 * eth if eth is not None else None, actors.timeboosted(rc), self.chain.name, tx))
                read += 1
            self.conn.commit()
        return read

    def _sampled(self, actor: str, since_ts: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM chain_arbs WHERE chain = ? AND actor = ? AND ts >= ? "
                                 "AND sampled = 1", (self.chain.name, actor, since_ts)).fetchone()[0]


# --- まとめ ---
SIZES = ((0.1, "0.1ドル未満"), (1, "1ドル未満"), (10, "10ドル未満"), (100, "100ドル未満"), (float("inf"), "100ドル以上"))


def _size(usd: float) -> str:
    return next(name for limit, name in SIZES if usd < limit)


def summarize(conn: sqlite3.Connection, chain: str, since_ts: int) -> dict:
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM chain_arbs WHERE chain = ? AND ts >= ? ORDER BY block",
                        (chain, since_ts)).fetchall()
    names = {a: s for a, s in conn.execute("SELECT address, symbol FROM chain_tokens WHERE chain = ?", (chain,))}
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["actor"], []).append(r)
    out = []
    for actor, rs in by.items():
        priced = [r for r in rs if r["gross_usd"] is not None]
        gross = sum(r["gross_usd"] for r in priced)
        sampled = [r for r in rs if r["sampled"]]
        gas = [r["gas_usd"] for r in sampled if r["gas_usd"] is not None]
        avg_gas = statistics.mean(gas) if gas else None
        known_boost = [r for r in sampled if r["timeboosted"] in (0, 1)]
        trig = [r for r in rs if r["trigger_block"] is not None]
        delays = [r["trigger_seconds"] for r in trig]
        out.append({
            "actor": actor, "count": len(rs), "gross_usd": gross, "unpriced": len(rs) - len(priced),
            "tokens": [names.get(t, t[:10]) for t, _ in Counter(r["profit_token"] for r in rs).most_common(2)],
            "kinds": Counter(k for r in rs for k in r["kinds"].split(",")),
            "median_swaps": statistics.median(r["swaps"] for r in rs),
            "same_block": sum(1 for r in trig if r["trigger_block"] == r["block"]), "with_trigger": len(trig),
            "median_delay": statistics.median(delays) if delays else None,
            "sampled": len(sampled), "avg_gas": avg_gas,
            "net_est": gross - avg_gas * len(rs) if avg_gas is not None else None,
            "senders": len({r["sender"] for r in sampled if r["sender"]}),
            "boosted": sum(1 for r in known_boost if r["timeboosted"] == 1), "boost_known": len(known_boost),
            "median_index": statistics.median(r["tx_index"] for r in sampled if r["tx_index"] is not None)
            if any(r["tx_index"] is not None for r in sampled) else None,
        })
    out.sort(key=lambda v: v["gross_usd"], reverse=True)
    total = sum(v["gross_usd"] for v in out)
    sizes: dict[str, list[float]] = {name: [] for _, name in SIZES}
    for r in rows:
        if r["gross_usd"] is not None:
            sizes[_size(r["gross_usd"])].append(r["gross_usd"])
    big = sorted((r for r in rows if r["gross_usd"] is not None), key=lambda r: r["gross_usd"], reverse=True)
    return {"actors": out, "count": len(rows), "gross_usd": total, "sizes": sizes, "biggest": big[:10],
            "first_ts": rows[0]["ts"] if rows else None, "last_ts": rows[-1]["ts"] if rows else None}


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts, JST).strftime("%m/%d %H:%M")


def describe(s: dict, *, chain: str, hours: float, top: int = 15, swaps: int = 0, txs: int = 0) -> list[str]:
    out = [(f"裁定(1回の取引の中で元のトークンに戻る交換)({chain}・直近{hours:g}時間、{s['count']:,}件、"
            f"主体 {len(s['actors'])}、粗利 ${s['gross_usd']:,.2f})")]
    if swaps:
        out.append(f"  この回に読んだ交換 {swaps:,}回・取引 {txs:,}件")
    if not s["actors"]:
        return out + ["  裁定は見つからなかった"]
    lead = s["actors"][0]
    out.append(f"  1位の取り分(粗利): {lead['gross_usd'] / s['gross_usd']:.0%}" if s["gross_usd"] > 0 else
               "  粗利が読めた裁定は無い")
    out.append("  1件の粗利の大きさ: " + "、".join(f"{name} {len(v):,}件(${sum(v):,.2f})"
                                          for name, v in s["sizes"].items() if v))
    out.append("  順位 主体 件数 粗利 増えたトークン 交換の場 交換の数(中央値) 直前の交換と同じブロック "
               "遅れの中央値 | 受領書を読んだ分: 財布 ガス代(1件) 純利益の見込み 優先レーン 取引の順番(中央値)")
    for i, v in enumerate(s["actors"][:top], 1):
        kinds = "・".join(f"{k}×{n}" for k, n in v["kinds"].most_common(3))
        same = f"{v['same_block']}/{v['with_trigger']}件" if v["with_trigger"] else "-"
        med = f"{v['median_delay']:,.0f}秒" if v["median_delay"] is not None else "-"
        line = (f"  {i:>3}. {v['actor'] or '不明'}  {v['count']:,}件  ${v['gross_usd']:,.2f}"
                + (f"(値段が読めない {v['unpriced']}件)" if v["unpriced"] else "")
                + f"  {'・'.join(v['tokens'])}  {kinds}  {v['median_swaps']:g}回  {same}  {med}")
        if v["sampled"]:
            gas = f"${v['avg_gas']:,.4f}" if v["avg_gas"] is not None else "-"
            net = f"${v['net_est']:,.2f}" if v["net_est"] is not None else "-"
            idx = f"{v['median_index']:g}番目" if v["median_index"] is not None else "-"
            line += (f"  | {v['sampled']}件: {v['senders']}財布  {gas}  {net}  "
                     f"{v['boosted']}/{v['boost_known']}件  {idx}")
        out.append(line)
    if len(s["actors"]) > top:
        rest = s["actors"][top:]
        out.append(f"  ほか {len(rest)}主体(件数 {sum(v['count'] for v in rest):,}件、"
                   f"粗利 ${sum(v['gross_usd'] for v in rest):,.2f})")
    out.append("  粗利の大きい裁定:")
    for r in s["biggest"]:
        delay = (f"直前の交換から {r['block'] - r['trigger_block']:,}ブロック・{r['trigger_seconds']:,}秒"
                 if r["trigger_block"] is not None else "直前の交換は読んだ範囲に無い")
        out.append(f"    {_when(r['ts'])}  ブロック {r['block']:,}  {r['actor'] or '不明'}  ${r['gross_usd']:,.2f}  "
                   f"{r['path']}({r['swaps']}回)  {delay}")
    return out
