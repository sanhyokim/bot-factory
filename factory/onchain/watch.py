"""機会の見張り(設計書 2026-11-04 の道具5)。第0期の1つめは Aave v3 の清算。

1. 借り手を集める: 借りた記録(Borrow)を読み、借りた財布(onBehalfOf)を帳簿 chain_borrowers に足す
2. 健康度を読む: 借り手の getUserAccountData を Multicall3 でまとめて読み、chain_positions に残す。
   健康度が1を下回り、借金が min_debt_usd 以上なら「機会」として chain_opportunities に残す
3. 誰が取ったか: 機会のあとに起きた清算(actors.py が帳簿に残したもの)と照らし、取った主体・何ブロック後か・
   その清算の純利益を書く。清算されずに健康度が1以上に戻ったら「消えた」

利益の目安は「返せる借金 × 清算の上乗せ5%」の粗い値(担保ごとの上乗せは見ていない。ガス代は引かない)。
読み取りだけで、鍵もお金も使わない。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .. import config
from ..models import now_jst
from . import actors
from .abi import (
    address,
    decode_aggregate3,
    encode_aggregate3,
    encode_call,
    topic,
    words,
)
from .actors import finish_progress, mark_progress
from .chain import Chain

PROTOCOL = "aave_v3"
BORROW = topic("Borrow(address,address,address,uint256,uint8,uint256,uint16)")
BORROW_KEY = "aave_v3_borrow"
BONUS = 0.05                 # 清算の上乗せの目安(Aave の主な担保は4.5〜10%)
MAX_HEALTH = 10**9           # 借金0の時、Aave は健康度に最大の整数を返す。読みやすい大きな数にそろえる

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_borrowers (
    chain       TEXT NOT NULL,
    address     TEXT NOT NULL,
    first_block INTEGER NOT NULL,       -- 最初に借りたのを見たブロック
    last_block  INTEGER NOT NULL,       -- 最後に借りたのを見たブロック
    PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS chain_positions (
    chain          TEXT NOT NULL,
    address        TEXT NOT NULL,
    block          INTEGER NOT NULL,    -- 読んだ時の最新のブロック
    collateral_usd REAL NOT NULL,
    debt_usd       REAL NOT NULL,
    liquidation_threshold REAL NOT NULL,  -- 0.83 なら担保の83%まで借りられる線で清算
    health         REAL NOT NULL,       -- 健康度。1を下回ると清算できる
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS chain_opportunities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chain          TEXT NOT NULL,
    protocol       TEXT NOT NULL,
    borrower       TEXT NOT NULL,
    block          INTEGER NOT NULL,    -- 最初に見つけたブロック
    last_block     INTEGER NOT NULL,    -- 最後に健康度が1未満だったのを見たブロック
    found_at       TEXT NOT NULL,       -- 日本時間
    health         REAL NOT NULL,       -- 見つけた時の健康度
    min_health     REAL NOT NULL,
    collateral_usd REAL NOT NULL,
    debt_usd       REAL NOT NULL,
    est_profit_usd REAL NOT NULL,       -- 目安(返せる借金 × 5%)
    status         TEXT NOT NULL,       -- 未確定 / 取られた / 消えた
    captured_by    TEXT,                -- 清算を呼んだ仕組み
    captured_block INTEGER,
    delay_blocks   INTEGER,             -- 見つけてから何ブロック後に取られたか
    captured_net_usd REAL,
    captured_tx    TEXT
);
CREATE INDEX IF NOT EXISTS chain_opportunities_open ON chain_opportunities(chain, status, borrower);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    actors.ensure_schema(conn)      # 読み終えた範囲(chain_scans)と清算の記録を使う
    conn.executescript(SCHEMA)


def estimate_profit(health: float, debt_usd: float, collateral_usd: float) -> float:
    """返せる借金(健康度0.95以上は半分、未満は全部)× 上乗せ。担保が足りなければ担保まで。"""
    repay = debt_usd * (0.5 if health >= 0.95 else 1.0)
    repay = min(repay, collateral_usd / (1 + BONUS))
    return max(0.0, repay * BONUS)


class Watcher:
    def __init__(self, conn: sqlite3.Connection, chain: Chain, *, pool: str, multicall: str, batch: int = 100,
                 min_debt_usd: float = 10.0, purpose: str = "research", out: Callable[[str], None] = print):
        ensure_schema(conn)
        self.conn, self.chain, self.purpose, self.out = conn, chain, purpose, out
        self.pool, self.multicall = address(pool), address(multicall)
        self.batch, self.min_debt_usd = batch, min_debt_usd

    @classmethod
    def from_config(cls, conn, chain: Chain, **kw) -> Watcher:
        c = config.load_config()["onchain"]["chains"][chain.name]
        w = config.load_config()["onchain"].get("watch", {})
        return cls(conn, chain, pool=c["protocols"]["aave_v3_pool"], multicall=c["protocols"]["multicall3"],
                   batch=w.get("multicall_batch", 100), min_debt_usd=w.get("min_debt_usd", 10.0), **kw)

    # --- 1. 借り手を集める ---
    def discover(self, from_block: int, to_block: int, *, window: int = 200_000) -> int:
        """範囲の借りた記録を読み、新しい借り手の数を返す。"""
        before = self._count_borrowers()
        start = from_block
        while start <= to_block:
            end = min(to_block, start + window - 1)
            logs = self.chain.logs(from_block=start, to_block=end, address=self.pool, topics=[BORROW],
                                   purpose=self.purpose)
            for lg in logs:
                who = address(lg["topics"][2])
                block = int(lg["blockNumber"], 16)
                self.conn.execute(
                    """INSERT INTO chain_borrowers(chain, address, first_block, last_block) VALUES (?,?,?,?)
                       ON CONFLICT(chain, address) DO UPDATE SET
                         first_block = MIN(first_block, excluded.first_block),
                         last_block = MAX(last_block, excluded.last_block)""",
                    (self.chain.name, who, block, block))
            mark_progress(self.conn, self.chain.name, BORROW_KEY, from_block, end)
            start = end + 1
        finish_progress(self.conn, self.chain.name, BORROW_KEY, from_block)
        return self._count_borrowers() - before

    def _count_borrowers(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM chain_borrowers WHERE chain = ?",
                                 (self.chain.name,)).fetchone()[0]

    # --- 2. 健康度を読む ---
    def candidates(self) -> list[str]:
        """読む借り手: まだ読んでいない、借金が残っている、最後に読んだ後にまた借りた。"""
        return [r[0] for r in self.conn.execute(
            """SELECT b.address FROM chain_borrowers b
               LEFT JOIN chain_positions p ON p.chain = b.chain AND p.address = b.address
               WHERE b.chain = ? AND (p.address IS NULL OR p.debt_usd > 0 OR b.last_block > p.block)
               ORDER BY b.address""", (self.chain.name,))]

    def read_accounts(self, users: list[str], block: int | str = "latest") -> dict[str, tuple]:
        """借り手ごとの (担保ドル, 借金ドル, 清算の線, 健康度)。Multicall3 で batch 人ずつまとめて読む。"""
        out: dict[str, tuple] = {}
        for i in range(0, len(users), self.batch):
            chunk = users[i:i + self.batch]
            data = encode_aggregate3([(self.pool, encode_call("getUserAccountData(address)", u)) for u in chunk])
            answers = decode_aggregate3(self.chain.call(self.multicall, data, block=block, purpose=self.purpose))
            for u, (ok, ret) in zip(chunk, answers):
                if not ok or len(ret) < 2 + 64 * 6:
                    continue
                coll, debt, _avail, lt, _ltv, hf = words(ret)[:6]
                out[u] = (coll / 1e8, debt / 1e8, lt / 1e4, min(hf / 1e18, MAX_HEALTH))
        return out

    def sweep(self) -> dict:
        """借り手の健康度を読み、機会を記録する。数のまとめを返す。"""
        head = self.chain.head(self.purpose)
        users = self.candidates()
        accounts = self.read_accounts(users)
        now = now_jst().isoformat(timespec="seconds")
        found, still = 0, 0
        under = {u for u, (_, debt, _, hf) in accounts.items() if hf < 1 and debt >= self.min_debt_usd}
        for u, (coll, debt, lt, hf) in accounts.items():
            self.conn.execute(
                """INSERT OR REPLACE INTO chain_positions(chain, address, block, collateral_usd, debt_usd,
                       liquidation_threshold, health, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                (self.chain.name, u, head, coll, debt, lt, hf, now))
            if u not in under:
                continue
            row = self.conn.execute("""SELECT id FROM chain_opportunities WHERE chain = ? AND borrower = ?
                                       AND status = '未確定'""", (self.chain.name, u)).fetchone()
            if row:
                self.conn.execute("""UPDATE chain_opportunities SET last_block = ?, min_health = MIN(min_health, ?)
                                     WHERE id = ?""", (head, hf, row[0]))
                still += 1
            else:
                self.conn.execute(
                    """INSERT INTO chain_opportunities(chain, protocol, borrower, block, last_block, found_at, health,
                           min_health, collateral_usd, debt_usd, est_profit_usd, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?, '未確定')""",
                    (self.chain.name, PROTOCOL, u, head, head, now, hf, hf, coll, debt,
                     estimate_profit(hf, debt, coll)))
                found += 1
        self.conn.commit()
        # 健康度が戻った(または借金が小さくなった)未確定の機会は、清算の記録と照らしてから「消えた」にする
        open_rows = self.conn.execute("""SELECT id, borrower FROM chain_opportunities
                                         WHERE chain = ? AND status = '未確定'""", (self.chain.name,)).fetchall()
        recovered = [r for r in open_rows if r[1] in accounts and r[1] not in under]
        return {"head": head, "borrowers": self._count_borrowers(), "read": len(accounts),
                "with_debt": sum(1 for a in accounts.values() if a[1] > 0),
                "under": len(under), "found": found, "still": still, "recovered": [r[0] for r in recovered],
                "near": sum(1 for a in accounts.values() if 1 <= a[3] < 1.02 and a[1] >= self.min_debt_usd)}

    # --- 3. 誰が取ったか ---
    def resolve(self, recovered_ids: list[int] = ()) -> dict:
        """未確定の機会を、あとで起きた清算と照らす。取られていなくて健康度が戻ったものは「消えた」。"""
        captured, vanished = 0, 0
        for opp in self.conn.execute("""SELECT * FROM chain_opportunities WHERE chain = ? AND status = '未確定'""",
                                     (self.chain.name,)).fetchall():
            liq = self.conn.execute(
                """SELECT liquidator, block, net_usd, tx_hash FROM chain_liquidations
                   WHERE chain = ? AND borrower = ? AND block > ? ORDER BY block, log_index LIMIT 1""",
                (self.chain.name, opp["borrower"], opp["block"])).fetchone()
            if liq:
                self.conn.execute(
                    """UPDATE chain_opportunities SET status = '取られた', captured_by = ?, captured_block = ?,
                           delay_blocks = ?, captured_net_usd = ?, captured_tx = ? WHERE id = ?""",
                    (liq[0], liq[1], liq[1] - opp["block"], liq[2], liq[3], opp["id"]))
                captured += 1
            elif opp["id"] in recovered_ids:
                self.conn.execute("UPDATE chain_opportunities SET status = '消えた' WHERE id = ?", (opp["id"],))
                vanished += 1
        self.conn.commit()
        return {"captured": captured, "vanished": vanished}


def report(conn: sqlite3.Connection, chain: str, *, limit: int = 10) -> list[str]:
    """機会の記録のまとめ(取られた・消えた・未確定、取った主体、何ブロック後か)。"""
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM chain_opportunities WHERE chain = ? ORDER BY id", (chain,)).fetchall()
    if not rows:
        return ["  機会(健康度1未満)の記録はまだ無い"]
    by = {s: [r for r in rows if r["status"] == s] for s in ("取られた", "消えた", "未確定")}
    lines = [(f"  機会の記録 {len(rows)}件: 取られた {len(by['取られた'])}件、消えた {len(by['消えた'])}件、"
              f"未確定 {len(by['未確定'])}件")]
    if by["取られた"]:
        delays = sorted(r["delay_blocks"] for r in by["取られた"])
        lines.append(f"  取られるまでのブロック数: 中央値 {delays[len(delays) // 2]}、最短 {delays[0]}、最長 {delays[-1]}")
    for r in rows[-limit:][::-1]:
        tail = (f"→ {r['captured_by']} が {r['delay_blocks']}ブロック後に取った(純利益 "
                + (f"${r['captured_net_usd']:,.2f}" if r["captured_net_usd"] is not None else "不明") + ")"
                if r["status"] == "取られた" else f"→ {r['status']}")
        lines.append(f"    {r['found_at'][5:16].replace('T', ' ')} {r['borrower']} 健康度 {r['health']:.4f} 借金 ${r['debt_usd']:,.0f} "
                     f"目安 ${r['est_profit_usd']:,.2f} {tail}")
    return lines
