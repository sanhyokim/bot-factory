"""稼いでいる主体の分析(設計書 2026-11-04 の道具4)。第0期の1つめは清算(Aave v3)。

清算の記録を読み、1件ごとに次を帳簿 chain_liquidations に残す。読み取りだけで、鍵もお金も使わない。
- 誰が: 清算を呼んだ仕組み(liquidator)と、取引を出した財布(sender)
- いくら: 受け取った担保の価値 − 返した借金の価値 − ガス代。値段は Aave の値段表を、清算したブロックで読む
  (古いブロックの状態を読めない接続先しか無い時は今の値段で読み、そう記録する)
- どう取ったか(型): 直接か自前の仕組み経由か、フラッシュローン、交換の回数、取引の中の何番目か

粗利は値段表で計算した理論の値。交換の滑りやフラッシュローンの手数料は引いていない。
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from .. import config
from .abi import address, decode_string, encode_call, to_int, topic, words
from .chain import Chain
from .rpc import AllBusy, CallReverted, RpcError

PROTOCOL = "aave_v3"
LIQUIDATION_CALL = topic("LiquidationCall(address,address,address,uint256,uint256,address,bool)")

# 取引の受領書に出るログから、どう取ったかを見分ける
FLASH_EVENTS = {
    topic("FlashLoan(address,address,address,uint256,uint8,uint256,uint16)"): "Aave",
    topic("FlashLoan(address,address,uint256,uint256)"): "Balancer",
    topic("Flash(address,address,uint256,uint256,uint256,uint256)"): "UniswapV3",
}
SWAP_EVENTS = {
    topic("Swap(address,address,int256,int256,uint160,uint128,int24)"): "UniswapV3",
    topic("Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)"): "PancakeV3",
    topic("Swap(address,uint256,uint256,uint256,uint256,address)"): "UniswapV2",
    topic("Swap(bytes32,address,address,uint256,uint256)"): "Balancer",
    topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"): "UniswapV4",
    topic("TokenExchange(address,int128,uint256,int128,uint256)"): "Curve",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_liquidations (
    chain         TEXT NOT NULL,
    tx_hash       TEXT NOT NULL,
    log_index     INTEGER NOT NULL,
    protocol      TEXT NOT NULL,
    block         INTEGER NOT NULL,
    ts            INTEGER NOT NULL,          -- ブロックの時刻(秒, UTC)
    tx_index      INTEGER,                   -- ブロックの中で何番目の取引か
    liquidator    TEXT NOT NULL,             -- 清算を呼んだ仕組み(または財布)
    sender        TEXT,                      -- 取引を出した財布
    tx_to         TEXT,
    borrower      TEXT NOT NULL,
    collateral    TEXT NOT NULL,
    debt          TEXT NOT NULL,
    collateral_amount TEXT NOT NULL,         -- 最小単位の整数(桁が大きいので文字で残す)
    debt_amount   TEXT NOT NULL,
    collateral_usd REAL,
    debt_usd      REAL,
    gas_usd       REAL,                      -- 取引のガス代を、その取引の清算の件数で割った分
    gross_usd     REAL,
    net_usd       REAL,
    gas_price_gwei REAL,
    price_basis   TEXT,                      -- block(そのブロックの値段)/ latest(今の値段)
    via           TEXT,                      -- 直接 / 自前の仕組み
    flash         TEXT NOT NULL DEFAULT '',  -- 使ったフラッシュローン(例: Aave,Balancer)
    swaps         INTEGER NOT NULL DEFAULT 0,
    pattern       TEXT NOT NULL DEFAULT '',
    timeboosted   INTEGER,                   -- 優先レーン(Timeboost)で入った取引か: 1 / 0 / -1(受領書に印が無い)
    PRIMARY KEY (chain, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS chain_liquidations_actor ON chain_liquidations(chain, liquidator, ts);
CREATE TABLE IF NOT EXISTS chain_tokens (
    chain    TEXT NOT NULL,
    address  TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    decimals INTEGER NOT NULL,
    PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS chain_scans (
    chain      TEXT NOT NULL,
    protocol   TEXT NOT NULL,
    first_block INTEGER NOT NULL,            -- ここから
    last_block INTEGER NOT NULL,             -- ここまで読み終えた(すき間なし)
    PRIMARY KEY (chain, protocol)
);
"""

WINDOW_BLOCKS = 50_000      # この幅ごとに読み終えた所を残す(途中で止まっても、次はそこから)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(chain_liquidations)")}
    if "timeboosted" not in have:          # 2026-11-05 に足した列
        conn.execute("ALTER TABLE chain_liquidations ADD COLUMN timeboosted INTEGER")
        conn.commit()


def timeboosted(receipt: dict) -> int:
    """Arbitrum の優先レーン(1分ごとの入札で勝った人が0.2秒早く入れられる有料の枠)で入った取引か。
    受領書の timeboosted の印を読む。印が無ければ -1(接続先か時期によっては返さない)。"""
    v = (receipt or {}).get("timeboosted")
    if v is None:
        return -1
    return 1 if v in (True, "0x1", "true", 1) else 0


@dataclass
class Liquidation:
    tx_hash: str
    log_index: int
    block: int
    collateral: str
    debt: str
    borrower: str
    debt_amount: int
    collateral_amount: int
    liquidator: str


def parse_liquidation(log: dict) -> Liquidation:
    w = words(log["data"])
    t = log["topics"]
    return Liquidation(tx_hash=log["transactionHash"], log_index=to_int(log["logIndex"]),
                       block=to_int(log["blockNumber"]), collateral=address(t[1]), debt=address(t[2]),
                       borrower=address(t[3]), debt_amount=w[0], collateral_amount=w[1],
                       liquidator=address(f"{w[2]:064x}"))


def tx_shape(tx: dict, receipt: dict, pool: str) -> dict:
    """取引の型: 直接か自前の仕組み経由か・フラッシュローン・交換の回数。"""
    flash, swaps = [], Counter()
    for lg in receipt.get("logs", []):
        t0 = (lg.get("topics") or [None])[0]
        if t0 in FLASH_EVENTS:
            flash.append(FLASH_EVENTS[t0])
        elif t0 in SWAP_EVENTS:
            swaps[SWAP_EVENTS[t0]] += 1
    via = "直接" if (tx.get("to") or "").lower() == pool.lower() else "自前の仕組み"
    parts = [via]
    if flash:
        parts.append("フラッシュローン(" + "・".join(sorted(set(flash))) + ")")
    n_swaps = sum(swaps.values())
    parts.append(f"交換{n_swaps}回(" + "・".join(sorted(swaps)) + ")" if n_swaps else "交換なし")
    return {"via": via, "flash": ",".join(sorted(set(flash))), "swaps": n_swaps, "pattern": "・".join(parts)}


class Scanner:
    def __init__(self, conn: sqlite3.Connection, chain: Chain, *, pool: str, weth: str,
                 purpose: str = "research", out: Callable[[str], None] = print):
        ensure_schema(conn)
        self.conn, self.chain, self.purpose, self.out = conn, chain, purpose, out
        self.pool, self.weth = address(pool), address(weth)
        self._oracle: tuple[str, int] | None = None
        self.archive_ok = True        # 古いブロックの状態を読めるか(読めなければ今の値段で読む)
        self._tokens: dict[str, tuple[str, int]] = {}

    @classmethod
    def from_config(cls, conn, chain: Chain, **kw) -> Scanner:
        p = config.load_config()["onchain"]["chains"][chain.name]["protocols"]
        return cls(conn, chain, pool=p["aave_v3_pool"], weth=p["weth"], **kw)

    # --- 値段 ---
    def _call(self, to: str, data: str, block: int | str = "latest") -> str:
        return self.chain.call(to, data, block=block, purpose=self.purpose)

    def oracle(self) -> tuple[str, int]:
        """Aave の値段表の住所と、値段の単位(USD建てなら 1e8)。"""
        if self._oracle is None:
            provider = address(self._call(self.pool, encode_call("ADDRESSES_PROVIDER()")))
            oracle = address(self._call(provider, encode_call("getPriceOracle()")))
            unit = words(self._call(oracle, encode_call("BASE_CURRENCY_UNIT()")))[0]
            self._oracle = (oracle, unit)
        return self._oracle

    def price(self, asset: str, block: int) -> tuple[float | None, str]:
        """1トークンあたりのドル。そのブロックで読めなければ今の値段。"""
        oracle, unit = self.oracle()
        data = encode_call("getAssetPrice(address)", asset)
        if self.archive_ok:
            try:
                return words(self._call(oracle, data, block))[0] / unit, "block"
            except CallReverted:
                return None, "block"
            except AllBusy:
                raise
            except RpcError as e:
                self.archive_ok = False
                self.out(f"  古いブロックの値段を読める接続先が無い。今の値段で計算する({str(e)[:80]})")
        try:
            return words(self._call(oracle, data))[0] / unit, "latest"
        except CallReverted:
            return None, "latest"

    def token(self, asset: str) -> tuple[str, int]:
        if asset in self._tokens:
            return self._tokens[asset]
        row = self.conn.execute("SELECT symbol, decimals FROM chain_tokens WHERE chain = ? AND address = ?",
                                (self.chain.name, asset)).fetchone()
        if row:
            meta = (row[0], int(row[1]))
        else:
            decimals = words(self._call(asset, encode_call("decimals()")))[0]
            try:
                symbol = decode_string(self._call(asset, encode_call("symbol()")))
            except (CallReverted, ValueError):
                symbol = asset[:10]
            meta = (symbol, int(decimals))
            self.conn.execute("INSERT OR REPLACE INTO chain_tokens(chain, address, symbol, decimals) VALUES (?,?,?,?)",
                              (self.chain.name, asset, *meta))
            self.conn.commit()
        self._tokens[asset] = meta
        return meta

    # --- 読む ---
    def scan(self, from_block: int, to_block: int) -> int:
        """範囲の清算を読み、新しく帳簿に足した件数を返す。WINDOW_BLOCKS ごとに読み終えた所を残す。"""
        added = 0
        start = from_block
        windows = max(1, -(-(to_block - from_block + 1) // WINDOW_BLOCKS))
        done = 0
        while start <= to_block:
            end = min(to_block, start + WINDOW_BLOCKS - 1)
            logs = self.chain.logs(from_block=start, to_block=end, address=self.pool,
                                   topics=[LIQUIDATION_CALL], purpose=self.purpose)
            added += self._record([parse_liquidation(lg) for lg in logs])
            mark_progress(self.conn, self.chain.name, PROTOCOL, from_block, end)
            done += 1
            if done % 10 == 0 and done < windows:
                self.out(f"  {done}/{windows} 区切りを読んだ(ブロック {end:,} まで、新しい清算 {added}件)")
            start = end + 1
        finish_progress(self.conn, self.chain.name, PROTOCOL, from_block)
        return added

    def _record(self, events: list[Liquidation]) -> int:
        have = {(r[0], r[1]) for r in self.conn.execute(
            "SELECT tx_hash, log_index FROM chain_liquidations WHERE chain = ? AND block BETWEEN ? AND ?",
            (self.chain.name, min((e.block for e in events), default=0), max((e.block for e in events), default=0)))}
        new = [e for e in events if (e.tx_hash, e.log_index) not in have]
        per_tx = Counter(e.tx_hash for e in events)
        cache: dict[str, tuple[dict, dict]] = {}
        for e in new:
            if e.tx_hash not in cache:
                tx = self.chain.transaction(e.tx_hash, purpose=self.purpose) or {}
                rc = self.chain.receipt(e.tx_hash, purpose=self.purpose) or {}
                cache[e.tx_hash] = (tx, rc)
            tx, rc = cache[e.tx_hash]
            blk = self.chain.block(e.block, purpose=self.purpose) or {}
            shape = tx_shape(tx, rc, self.pool)
            c_sym, c_dec = self.token(e.collateral)
            d_sym, d_dec = self.token(e.debt)
            c_px, basis = self.price(e.collateral, e.block)
            d_px, _ = self.price(e.debt, e.block)
            eth_px, _ = self.price(self.weth, e.block)
            c_usd = e.collateral_amount / 10 ** c_dec * c_px if c_px is not None else None
            d_usd = e.debt_amount / 10 ** d_dec * d_px if d_px is not None else None
            gas_wei = to_int(rc.get("gasUsed", "0x0")) * to_int(rc.get("effectiveGasPrice") or tx.get("gasPrice") or "0x0")
            gas_usd = gas_wei / 1e18 * eth_px / per_tx[e.tx_hash] if eth_px is not None else None
            gross = c_usd - d_usd if c_usd is not None and d_usd is not None else None
            net = gross - gas_usd if gross is not None and gas_usd is not None else None
            gwei = to_int(rc.get("effectiveGasPrice") or tx.get("gasPrice") or "0x0") / 1e9
            self.conn.execute(
                """INSERT OR IGNORE INTO chain_liquidations(chain, tx_hash, log_index, protocol, block, ts, tx_index,
                       liquidator, sender, tx_to, borrower, collateral, debt, collateral_amount, debt_amount,
                       collateral_usd, debt_usd, gas_usd, gross_usd, net_usd, gas_price_gwei, price_basis,
                       via, flash, swaps, pattern, timeboosted)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.chain.name, e.tx_hash, e.log_index, PROTOCOL, e.block, to_int(blk.get("timestamp", "0x0")),
                 to_int(tx["transactionIndex"]) if tx.get("transactionIndex") else None,
                 e.liquidator, address(tx["from"]) if tx.get("from") else None,
                 address(tx["to"]) if tx.get("to") else None, e.borrower, e.collateral, e.debt,
                 str(e.collateral_amount), str(e.debt_amount), c_usd, d_usd, gas_usd, gross, net, gwei, basis,
                 shape["via"], shape["flash"], shape["swaps"], f"{c_sym}担保/{d_sym}借金・" + shape["pattern"],
                 timeboosted(rc)))
            self.conn.commit()
        return len(new)


def backfill_timeboost(conn: sqlite3.Connection, chain: Chain, since_ts: int, purpose: str = "research") -> int:
    """優先レーンの印を足す前に帳簿に入った清算に、受領書から印を書く(受領書はキャッシュから返る)。"""
    rows = conn.execute("""SELECT DISTINCT tx_hash FROM chain_liquidations
                           WHERE chain = ? AND ts >= ? AND timeboosted IS NULL""", (chain.name, since_ts)).fetchall()
    for (tx_hash,) in rows:
        rc = chain.receipt(tx_hash, purpose=purpose) or {}
        conn.execute("UPDATE chain_liquidations SET timeboosted = ? WHERE chain = ? AND tx_hash = ?",
                     (timeboosted(rc), chain.name, tx_hash))
    conn.commit()
    return len(rows)


def plan(conn: sqlite3.Connection, chain: str, start: int, end: int, key: str = PROTOCOL) -> list[tuple[int, int]]:
    """[start, end] のうち、まだ読んでいない範囲。前に読んだ範囲とつながらなければ、前の記録は忘れて読み直す。
    key は何を読んだかの名前(清算は aave_v3、借り手は aave_v3_borrow など)。"""
    ensure_schema(conn)
    row = conn.execute("SELECT first_block, last_block FROM chain_scans WHERE chain = ? AND protocol = ?",
                       (chain, key)).fetchone()
    if row is None or row[1] < start - 1 or row[0] > end + 1:
        conn.execute("DELETE FROM chain_scans WHERE chain = ? AND protocol = ?", (chain, key))
        conn.commit()
        return [(start, end)] if start <= end else []
    first, last = row
    ranges = []
    if start < first:
        ranges.append((start, first - 1))
    if last < end:
        ranges.append((max(start, last + 1), end))
    return ranges


def mark_progress(conn: sqlite3.Connection, chain: str, key: str, from_block: int, end: int) -> None:
    """[from_block, end] まで読み終えた(区切りごとに呼ぶ)。"""
    conn.execute(
        """INSERT INTO chain_scans(chain, protocol, first_block, last_block) VALUES (?,?,?,?)
           ON CONFLICT(chain, protocol) DO UPDATE SET last_block = MAX(last_block, excluded.last_block)""",
        (chain, key, from_block, end))
    conn.commit()


def finish_progress(conn: sqlite3.Connection, chain: str, key: str, from_block: int) -> None:
    """範囲を最後まで読んだ(前にさかのぼって読んだ時は、読んだ範囲の始まりを広げる)。"""
    conn.execute("UPDATE chain_scans SET first_block = MIN(first_block, ?) WHERE chain = ? AND protocol = ?",
                 (from_block, chain, key))
    conn.commit()


def summarize(conn: sqlite3.Connection, chain: str, since_ts: int) -> list[dict]:
    """清算した仕組みごとのまとめ。純利益の多い順。"""
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT * FROM chain_liquidations WHERE chain = ? AND ts >= ? ORDER BY ts""", (chain, since_ts)).fetchall()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["liquidator"], []).append(r)
    out = []
    for who, rs in by.items():
        known = [r for r in rs if r["net_usd"] is not None]
        shapes = Counter(r["pattern"].split("・", 1)[-1] for r in rs)
        out.append({
            "liquidator": who, "count": len(rs), "senders": len({r["sender"] for r in rs if r["sender"]}),
            "gross_usd": sum(r["gross_usd"] for r in known), "gas_usd": sum(r["gas_usd"] for r in known),
            "net_usd": sum(r["net_usd"] for r in known), "unpriced": len(rs) - len(known),
            "best_usd": max((r["net_usd"] for r in known), default=0.0),
            "median_gwei": statistics.median(r["gas_price_gwei"] for r in rs),
            "via": Counter(r["via"] for r in rs).most_common(1)[0][0],
            "shape": shapes.most_common(1)[0][0], "first_ts": rs[0]["ts"], "last_ts": rs[-1]["ts"],
            "boosted": sum(1 for r in rs if r["timeboosted"] == 1),
            "boost_known": sum(1 for r in rs if r["timeboosted"] in (0, 1)),
        })
    out.sort(key=lambda a: a["net_usd"], reverse=True)
    return out


def describe(summary: list[dict], *, chain: str, days: float, top: int = 15, basis: str = "block") -> list[str]:
    total = sum(a["count"] for a in summary)
    net = sum(a["net_usd"] for a in summary)
    lines = [f"清算で稼いでいる主体(Aave v3・{chain}・直近{days:g}日)",
             f"  清算 {total}件、清算した主体 {len(summary)}件、純利益の合計 ${net:,.2f}"
             + ("" if basis == "block" else "(値段は今の値段で計算。古いブロックの状態を読める接続先が無かった)")]
    if not summary:
        return lines
    lines.append("  順位 清算した仕組み  回数 財布数 純利益(粗利−ガス代) 1件の最大 ガス価格の中央値 主な型 "
                 "優先レーンで入った件数")
    for i, a in enumerate(summary[:top], 1):
        share = a["net_usd"] / net * 100 if net > 0 else 0
        lines.append(f"  {i:>2}. {a['liquidator']}  {a['count']}回 {a['senders']}財布 "
                     f"${a['net_usd']:,.2f}(${a['gross_usd']:,.2f}−${a['gas_usd']:,.2f}、全体の{share:.0f}%) "
                     f"${a['best_usd']:,.2f} {a['median_gwei']:.3g}gwei {a['shape']} "
                     + (f"優先レーン {a['boosted']}/{a['boost_known']}件" if a["boost_known"] else "優先レーン 不明")
                     + (f" 値段なし{a['unpriced']}件" if a["unpriced"] else ""))
    if len(summary) > top:
        lines.append(f"  ほか{len(summary) - top}件")
    return lines
