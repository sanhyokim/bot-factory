"""Compound の写しの清算を深く読む(設計書 2026-11-04 の道具6「機会の模擬」の2つめ)。

清算の地図(venue-scan)で、Compound の写しの古い仕組み(Lodestar・dForce など)は清算が多いのに、
清算している主体が1〜2しかいないと分かった(11/5)。作者の言う「競争のない古い場所」の候補なので、
地図に載った清算1件ずつについて、次を帳簿 chain_compound_liquidations に残す。

- いくら: 受け取った担保の価値(仕組みの取り分を引く)− 返した借金の価値 − ガス代。値段は仕組み自身の値段表を
  そのブロックで読む。値段表の単位がドルでない仕組み(ETH 建てなど)もあるので、Aave の値段表と同じトークンで
  比べて単位を決める
- いつ: 借り手が清算できるようになった(不足額 shortfall が0より大きくなった)ブロックを、古いブロックの状態から
  さかのぼって探し、そこから清算までのブロック数・秒数を出す。Compound の写しは、誰かが市場に触れるまで利息が
  帳簿に足されないので、借り手の市場の利息を足してから(accrueInterest を、送らずに試しに呼んでから)読む
- なぜ: 設定の変更(担保の掛け目などが変わった。告知されることが多く、時刻が前もって分かる)/ 値段 / 利息 /
  続き(前の清算の後も清算できるまま)/ 長く清算できるまま

Compound 本家の写し(comptroller())と、dForce の写し(controller())の2つの呼び方に対応する。
読み取りだけ(古いブロックの状態を読める接続先が要る)。鍵もお金も使わず、取引も送らない。
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
from . import actors, venues
from .abi import (
    address,
    decode_addresses,
    decode_aggregate3,
    encode_aggregate3,
    encode_call,
    to_int,
    words,
)
from .chain import Chain
from .rpc import CallReverted

CAUSES = ("設定の変更", "値段", "利息", "続き", "長く清算できるまま")
FRESH = ("設定の変更", "値段", "利息")          # 新しく清算できるようになった分
LIQUIDATE_BORROW = next(f for f in venues.FAMILIES if f.name == venues.COMPTROLLER).topic0


@dataclass(frozen=True)
class Dialect:
    """Compound の写しの呼び方。shortfall_word は不足額が答えの何語目か。assets は借り手の市場の一覧を返す呼び方。"""
    name: str
    controller: str
    oracle: str
    liquidity: str
    shortfall_word: int
    accrue: str
    assets: tuple[str, ...]


DIALECTS = (
    Dialect("Compound", "comptroller()", "oracle()", "getAccountLiquidity(address)", 2, "accrueInterest()",
            ("getAssetsIn(address)",)),
    Dialect("dForce", "controller()", "priceOracle()", "calcAccountEquity(address)", 1, "updateInterest()",
            ("getEnteredMarkets(address)", "getBorrowedAssets(address)")),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_compound_liquidations (
    chain          TEXT NOT NULL,
    tx_hash        TEXT NOT NULL,
    log_index      INTEGER NOT NULL,
    controller     TEXT NOT NULL,      -- まとめる仕組み(Comptroller / dForce の Controller)
    dialect        TEXT NOT NULL,      -- Compound / dForce
    market         TEXT NOT NULL,      -- 借金を返した市場
    collateral_market TEXT NOT NULL,   -- 担保を受け取った市場
    borrower       TEXT NOT NULL,
    liquidator     TEXT NOT NULL,
    block          INTEGER NOT NULL,
    ts             INTEGER NOT NULL,
    repay_usd      REAL,
    seize_usd      REAL,               -- 仕組みの取り分を引いた、清算した側が受け取った担保の価値
    gas_usd        REAL,
    net_usd        REAL,
    cross_block    INTEGER,            -- 清算できるようになったブロック(長く清算できるままなら NULL)
    delay_blocks   INTEGER,
    delay_seconds  INTEGER,
    cause          TEXT NOT NULL,
    searched_back  INTEGER NOT NULL,
    reads          INTEGER NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (chain, tx_hash, log_index)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    venues.ensure_schema(conn)
    conn.executescript(SCHEMA)


class Reader:
    def __init__(self, conn: sqlite3.Connection, chain: Chain, *, scanner: actors.Scanner,
                 multicall: str | None = None, max_back: int = 2_419_200, purpose: str = "research",
                 out: Callable[[str], None] = print):
        ensure_schema(conn)
        self.conn, self.chain, self.scanner, self.purpose, self.out = conn, chain, scanner, purpose, out
        self.multicall = address(multicall) if multicall else None
        self.max_back = max_back
        self._controllers: dict[str, tuple[str, Dialect] | None] = {}
        self._oracles: dict[str, str | None] = {}
        self._underlying: dict[str, tuple[str | None, int]] = {}
        self._units: dict[str, tuple[str, float]] = {}
        self._reads = 0

    @classmethod
    def from_config(cls, conn, chain: Chain, **kw) -> Reader:
        cfg = config.load_config()["onchain"]
        max_back = cfg.get("timing", {}).get("max_back_blocks", 2_419_200)
        multicall = cfg["chains"][chain.name]["protocols"].get("multicall3")
        return cls(conn, chain, scanner=actors.Scanner.from_config(conn, chain, out=kw.get("out", print)),
                   multicall=multicall, max_back=max_back, **kw)

    # --- 読み取り ---
    def _call(self, to: str, data: str, block: int | str = "latest") -> str | None:
        self._reads += 1
        try:
            return self.chain.call(to, data, block=block, purpose=self.purpose)
        except CallReverted:
            return None

    def _words(self, to: str, sig: str, *args, block: int | str = "latest") -> list[int] | None:
        got = self._call(to, encode_call(sig, *args), block)
        return words(got) if got and got != "0x" else None

    def controller(self, market: str) -> tuple[str, Dialect] | None:
        """市場をまとめる仕組みと呼び方。どちらの呼び方でも読めなければ None。"""
        if market not in self._controllers:
            found = None
            for d in DIALECTS:
                w = self._words(market, d.controller)
                if w and w[0]:
                    found = (address(f"0x{w[0]:064x}"), d)
                    break
            self._controllers[market] = found
            if found:      # 地図の帳簿も、まとめる仕組みでまとめ直す(前の版の地図は dForce を市場ごとに分けていた)
                self.conn.execute("UPDATE chain_venues SET venue = ? WHERE chain = ? AND emitter = ?",
                                  (found[0], self.chain.name, market))
                self.conn.commit()
        return self._controllers[market]

    def oracle(self, ctrl: str, d: Dialect) -> str | None:
        if ctrl not in self._oracles:
            w = self._words(ctrl, d.oracle)
            self._oracles[ctrl] = address(f"0x{w[0]:064x}") if w and w[0] else None
        return self._oracles[ctrl]

    def underlying(self, market: str) -> tuple[str | None, int]:
        """市場の元のトークンと桁数(ETH のように元のトークンが無い市場は (None, 18))。"""
        if market not in self._underlying:
            w = self._words(market, "underlying()")
            token = address(f"0x{w[0]:064x}") if w and w[0] else None
            dec = 18
            if token:
                got = self._words(token, "decimals()")
                dec = got[0] if got else 18
            self._underlying[market] = (token, dec)
        return self._underlying[market]

    def raw_price(self, ctrl: str, d: Dialect, market: str, block: int) -> float | None:
        """仕組みの値段表の値段(元のトークン1つあたり、値段表の単位)。"""
        oracle = self.oracle(ctrl, d)
        if oracle is None:
            return None
        w = self._words(oracle, "getUnderlyingPrice(address)", market, block=block)
        if not w or not w[0]:
            return None
        _, dec = self.underlying(market)
        return w[0] / 10 ** (36 - dec)

    def unit(self, ctrl: str, d: Dialect, markets: tuple[str, ...], block: int) -> tuple[str, float] | None:
        """値段表の単位: ("ドル", 1) / ("ETH", 0)(ETH の値段を掛ける)/ ("倍", 掛ける数)。
        Aave の値段表にあるトークンの市場で比べて決める。比べられる市場が無ければ None(次の清算でまた試す)。"""
        if ctrl in self._units:
            return self._units[ctrl]
        for m in markets:
            token, _ = self.underlying(m)
            raw = self.raw_price(ctrl, d, m, block)
            ref, basis = self.scanner.price(token or self.scanner.weth, block)
            if not raw or not ref or basis != "block":
                continue
            f = ref / raw
            eth, _ = self.scanner.price(self.scanner.weth, block)
            if 0.9 < f < 1.1:
                found = ("ドル", 1.0)
            elif eth and 0.9 < f / eth < 1.1:
                found = ("ETH", 0.0)
            else:
                found = ("倍", f)
            self._units[ctrl] = found
            if found[0] != "ドル":
                self.out(f"  {ctrl}: 値段表の単位がドルではない({found[0]}"
                         + (f" {f:.6g}" if found[0] == "倍" else "") + ")。Aave の値段と比べて合わせる")
            return found
        return None

    def usd_price(self, ctrl: str, d: Dialect, market: str, block: int) -> float | None:
        """元のトークン1つあたりのドル(単位がまだ決まっていなければ None)。"""
        found = self._units.get(ctrl)
        raw = self.raw_price(ctrl, d, market, block) if found else None
        if raw is None:
            return None
        kind, f = found
        if kind == "ETH":
            eth, _ = self.scanner.price(self.scanner.weth, block)
            return raw * eth if eth else None
        return raw * f

    def assets_of(self, ctrl: str, d: Dialect, user: str, block: int) -> list[str]:
        """借り手が担保にしている市場と借りている市場。"""
        seen: list[str] = []
        for sig in d.assets:
            got = self._call(ctrl, encode_call(sig, user), block)
            if got and got != "0x":
                try:
                    seen += [m for m in decode_addresses(got) if m not in seen]
                except ValueError:
                    continue
        return seen

    def shortfall(self, ctrl: str, d: Dialect, user: str, block: int, markets: list[str] = ()) -> int | None:
        """そのブロックでの不足額(0より大きければ清算できる)。markets を渡すと、その市場の利息を足してから読む
        (Multicall3 の中で accrueInterest を試しに呼ぶ。取引は送らない)。"""
        liq = encode_call(d.liquidity, user)
        if markets and self.multicall:
            got = self._call(self.multicall, encode_aggregate3([(m, encode_call(d.accrue)) for m in markets]
                                                               + [(ctrl, liq)]), block)
            if got and got != "0x":
                ok, data = decode_aggregate3(got)[-1]
                w = words(data) if ok and data != "0x" else []
                if len(w) > d.shortfall_word:
                    return w[d.shortfall_word]
        got = self._call(ctrl, liq, block)
        w = words(got) if got and got != "0x" else []
        return w[d.shortfall_word] if len(w) > d.shortfall_word else None

    def settings(self, ctrl: str, market: str, block: int) -> list[int] | None:
        """市場の設定(担保の掛け目など。markets(address) の答えそのまま)。"""
        return self._words(ctrl, "markets(address)", market, block=block)

    def _timestamp(self, block: int) -> int:
        self._reads += 1
        return to_int((self.chain.block(block, purpose=self.purpose) or {}).get("timestamp", "0x0"))

    # --- 1件 ---
    def measure(self, ev: sqlite3.Row) -> dict | None:
        """清算1件の値段・遅れ・理由。読めない仕組み(呼び方が違う)なら None。"""
        self._reads = 0
        market, b = ev["emitter"], ev["block"]
        found = self.controller(market)
        if found is None:
            return None
        ctrl, d = found
        rc = self.chain.receipt(ev["tx_hash"], purpose=self.purpose) or {}
        self._reads += 1
        log = next((lg for lg in rc.get("logs", []) if to_int(lg.get("logIndex", "0x0")) == ev["log_index"]), None)
        if log is None:
            return None
        w = words(log["data"])
        borrower, repay, coll, seize = address(f"0x{w[1]:064x}"), w[2], address(f"0x{w[3]:064x}"), w[4]
        liquidator = ev["liquidator"] or address(f"0x{w[0]:064x}")

        # いくら
        _, d_dec = self.underlying(market)
        _, c_dec = self.underlying(coll)
        self.unit(ctrl, d, (market, coll), b)
        d_px, c_px = self.usd_price(ctrl, d, market, b), self.usd_price(ctrl, d, coll, b)
        rate = self._words(coll, "exchangeRateStored()", block=b)
        share_w = self._words(coll, "protocolSeizeShareMantissa()", block=b)
        share = share_w[0] / 1e18 if share_w else 0.0
        repay_usd = repay / 10 ** d_dec * d_px if d_px is not None else None
        seize_usd = (seize * rate[0] / 1e18 / 10 ** c_dec * c_px * (1 - share)
                     if rate and c_px is not None else None)
        eth_px, _ = self.scanner.price(self.scanner.weth, b)
        per_tx = sum(1 for lg in rc.get("logs", []) if (lg.get("topics") or [None])[0] == LIQUIDATE_BORROW)
        gas_wei = to_int(rc.get("gasUsed", "0x0")) * to_int(rc.get("effectiveGasPrice") or "0x0")
        gas_usd = gas_wei / 1e18 * eth_px / max(1, per_tx) if eth_px is not None else None
        net = (seize_usd - repay_usd - (gas_usd or 0.0)) if seize_usd is not None and repay_usd is not None else None

        # いつ・なぜ
        timing = self._timing(ctrl, d, ev, borrower, market, coll)
        return {"controller": ctrl, "dialect": d.name, "market": market, "collateral_market": coll,
                "borrower": borrower, "liquidator": liquidator, "repay_usd": repay_usd, "seize_usd": seize_usd,
                "gas_usd": gas_usd, "net_usd": net, **timing, "reads": self._reads}

    def _previous(self, ctrl: str, ev: sqlite3.Row, user: str) -> int | None:
        """同じ仕組みで、この借り手が前に清算されたブロック。"""
        b = ev["block"]
        return self.conn.execute(
            """SELECT MAX(e.block) FROM chain_venue_events e
               JOIN chain_venues v ON v.chain = e.chain AND v.emitter = e.emitter
               WHERE e.chain = ? AND e.family = ? AND e.borrower = ? AND v.venue = ?
                 AND (e.block < ? OR (e.block = ? AND e.log_index < ?))""",
            (self.chain.name, venues.COMPTROLLER, user, ctrl, b, b, ev["log_index"])).fetchone()[0]

    def _timing(self, ctrl: str, d: Dialect, ev: sqlite3.Row, user: str, market: str, coll: str) -> dict:
        b = ev["block"]
        prev = self._previous(ctrl, ev, user)
        if prev == b:
            return {"cross_block": b, "delay_blocks": 0, "delay_seconds": 0, "cause": "続き", "searched_back": 0}
        mkts = self.assets_of(ctrl, d, user, b - 1)
        mkts += [m for m in (market, coll) if m not in mkts]

        def is_open(x: int) -> bool:
            return (self.shortfall(ctrl, d, user, x, mkts) or 0) > 0

        if not is_open(b - 1):
            # 1つ前のブロックでは清算できなかった: 清算と同じブロックの中で清算できるようになった
            return {"cross_block": b, "delay_blocks": 0, "delay_seconds": 0, "searched_back": 1,
                    "cause": self._cause(ctrl, d, market, coll, b - 1, b)}
        floor = max(0, b - self.max_back) if prev is None else prev
        hi, step = b - 1, 1
        while True:
            x = max(floor, hi - step)
            if not is_open(x):
                lo = x
                break
            hi = x
            if x == floor:
                if prev is not None:
                    return {"cross_block": prev, "delay_blocks": b - prev, "cause": "続き", "searched_back": b - prev,
                            "delay_seconds": ev["ts"] - self._timestamp(prev)}
                return {"cross_block": None, "delay_blocks": None, "delay_seconds": None,
                        "cause": "長く清算できるまま", "searched_back": b - floor}
            step *= 2
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if is_open(mid):
                hi = mid
            else:
                lo = mid
        return {"cross_block": hi, "delay_blocks": b - hi, "delay_seconds": ev["ts"] - self._timestamp(hi),
                "cause": self._cause(ctrl, d, market, coll, lo, hi), "searched_back": b - lo}

    def _cause(self, ctrl: str, d: Dialect, market: str, coll: str, lo: int, hi: int) -> str:
        """lo(清算できない)と hi(清算できる)の間で何が変わったか。返した借金の市場と、受け取った担保の市場だけを
        見る(ほかの市場の設定や値段で割れた分は「利息」に入る。粗い見分け)。"""
        pair = [market] if market == coll else [market, coll]
        if any(self.settings(ctrl, m, lo) != self.settings(ctrl, m, hi) for m in pair):
            return "設定の変更"
        if any(self.raw_price(ctrl, d, m, lo) != self.raw_price(ctrl, d, m, hi) for m in pair):
            return "値段"
        return "利息"

    def run(self, since_ts: int, *, progress_every: int = 10) -> dict:
        """まだ読んでいない Compound型の清算を古い順に読む。数のまとめを返す。"""
        rows = self.conn.execute(
            """SELECT e.* FROM chain_venue_events e
               LEFT JOIN chain_compound_liquidations c
                 ON c.chain = e.chain AND c.tx_hash = e.tx_hash AND c.log_index = e.log_index
               WHERE e.chain = ? AND e.family = ? AND e.ts >= ? AND c.tx_hash IS NULL
               ORDER BY e.block, e.log_index""", (self.chain.name, venues.COMPTROLLER, since_ts)).fetchall()
        done = skipped = reads = 0
        for i, ev in enumerate(rows, 1):
            try:
                m = self.measure(ev)
            except (ValueError, IndexError) as e:     # 答えの形が思っていたのと違う仕組み。この1件は飛ばす
                self.out(f"  {ev['tx_hash']}: 読めなかった({e})")
                m = None
            reads += self._reads
            if m is None:
                skipped += 1
                continue
            self.conn.execute(
                """INSERT OR REPLACE INTO chain_compound_liquidations(chain, tx_hash, log_index, controller, dialect,
                       market, collateral_market, borrower, liquidator, block, ts, repay_usd, seize_usd, gas_usd,
                       net_usd, cross_block, delay_blocks, delay_seconds, cause, searched_back, reads, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.chain.name, ev["tx_hash"], ev["log_index"], m["controller"], m["dialect"], m["market"],
                 m["collateral_market"], m["borrower"], m["liquidator"], ev["block"], ev["ts"], m["repay_usd"],
                 m["seize_usd"], m["gas_usd"], m["net_usd"], m["cross_block"], m["delay_blocks"],
                 m["delay_seconds"], m["cause"], m["searched_back"], m["reads"],
                 now_jst().isoformat(timespec="seconds")))
            self.conn.commit()
            done += 1
            if progress_every and i % progress_every == 0:
                self.out(f"  {i}/{len(rows)}件を読んだ(読み取り {reads:,}回)")
        return {"todo": len(rows), "done": done, "skipped": skipped, "reads": reads}


# --- まとめ ---
BUCKETS = ("同じブロック", "1秒以内", "10秒以内", "1分以内", "1時間以内", "1日以内", "1日より後")


def _bucket(r) -> str:
    if r["delay_blocks"] == 0:
        return "同じブロック"
    s = r["delay_seconds"] or 0
    for limit, name in zip((1, 10, 60, 3600, 86400), BUCKETS[1:-1]):
        if s <= limit:
            return name
    return BUCKETS[-1]


def _usd(rows) -> float:
    return sum(r["net_usd"] or 0.0 for r in rows)


def summarize(conn: sqlite3.Connection, chain: str, since_ts: int) -> list[dict]:
    """まとめる仕組みごと。純利益の多い順。"""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM chain_compound_liquidations WHERE chain = ? AND ts >= ? ORDER BY block, log_index",
        (chain, since_ts)).fetchall()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["controller"], []).append(r)
    out = []
    for ctrl, rs in by.items():
        symbols = [s for (s,) in conn.execute(
            """SELECT symbol FROM chain_venues WHERE chain = ? AND venue = ? AND symbol != ''
               ORDER BY symbol LIMIT 4""", (chain, ctrl))]
        fresh = [r for r in rs if r["cause"] in FRESH]
        who: dict[str, list] = {}
        for r in rs:
            who.setdefault(r["liquidator"], []).append(r)
        delays = [r["delay_seconds"] for r in fresh if r["delay_seconds"] is not None]
        slow = [r for r in fresh if (r["delay_seconds"] or 0) > 60]
        out.append({
            "controller": ctrl, "dialect": rs[0]["dialect"], "symbols": symbols, "count": len(rs),
            "net_usd": _usd(rs), "unpriced": sum(1 for r in rs if r["net_usd"] is None),
            "actors": sorted(((w, len(x), _usd(x)) for w, x in who.items()), key=lambda a: a[2], reverse=True),
            "causes": Counter(r["cause"] for r in rs),
            "cause_usd": {c: _usd([r for r in rs if r["cause"] == c]) for c in CAUSES},
            "speed": Counter(_bucket(r) for r in fresh),
            "median_delay": statistics.median(delays) if delays else None,
            "slow_usd": _usd(slow),
            "slow": sorted(slow, key=lambda r: r["net_usd"] or 0.0, reverse=True),
        })
    out.sort(key=lambda v: v["net_usd"], reverse=True)
    return out


def describe(summary: list[dict], *, chain: str, days: float, top: int = 5) -> list[str]:
    total = sum(v["count"] for v in summary)
    out = [f"Compound の写しの清算({chain}・直近{days:g}日、{total}件、仕組み {len(summary)}か所)"]
    if not summary:
        return out + ["  読めた清算はまだ無い(先に venue-scan で清算の地図を作る)"]
    for v in summary:
        names = "・".join(v["symbols"]) or "名前なし"
        out.append(f"  {v['controller']}({names}・{v['dialect']}の呼び方): {v['count']}件  純利益 ${v['net_usd']:,.2f}"
                   + (f"(値段が読めない {v['unpriced']}件)" if v["unpriced"] else ""))
        out.append("    清算した主体: " + "、".join(f"{w} {n}件 ${usd:,.2f}" for w, n, usd in v["actors"][:3])
                   + (f"、ほか{len(v['actors']) - 3}主体" if len(v["actors"]) > 3 else ""))
        out.append("    理由: " + "、".join(f"{c} {v['causes'][c]}件(${v['cause_usd'][c]:,.2f})"
                                          for c in CAUSES if v["causes"].get(c)))
        if v["speed"]:
            med = v["median_delay"]
            out.append("    清算できるようになってから取られるまで: "
                       + "、".join(f"{n} {v['speed'][n]}件" for n in BUCKETS if v["speed"].get(n))
                       + (f"(中央値 {med:,.0f}秒)" if med is not None else ""))
        if v["slow"]:
            out.append(f"    1分より遅く取られた {len(v['slow'])}件・純利益 ${v['slow_usd']:,.2f}(額の大きい順):")
        for r in v["slow"][:top]:
            when = datetime.fromtimestamp(r["ts"], JST).strftime("%m/%d %H:%M")
            out.append(f"      {when}  ブロック {r['block']:,}  {r['liquidator']}  遅れ {r['delay_seconds']:,}秒  "
                       f"{r['cause']}  借金 ${r['repay_usd'] or 0:,.2f}  純利益 ${r['net_usd'] or 0:,.2f}")
    return out
