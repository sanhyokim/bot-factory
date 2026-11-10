"""取り残し(2026-11-07 ボスの選択「取り残しを探す」)。

第0期で読めた所では、お金のある清算と裁定はどれも、同じブロックか次のブロックで取られていた(報告書 11/5 の18〜19章)。
一方、作者の稼ぎの7割強は、誰も見ていない所での一回きりの当たりだった(作者の振り返り 11/1)。稼いでいる主体を
真似る読み方には、そういう機会は映らない。そこで、古い Compound の写しで、今清算できるのに誰も取っていない
借り手を探す。

1. 仕組み: 清算の地図(venue-scan)に載った Compound の写しのまとめる仕組みと、config.yaml の compound_comptrollers
2. 借り手: 市場の借りた記録(Borrow)を、市場の住所を決めて直近 days 日読む。清算の地図に載った借り手も足す
3. 今の状態: 市場の利息を足してから(accrueInterest を、送らずに試しに呼んでから)不足額を読む。0より大きければ
   清算できる。誰も市場に触れていないと利息が帳簿に足されず、帳簿の上では割れていない借り手もいるので
4. 清算できる借り手ごとに: 市場ごとの借金と担保をドルにし、一番大きい借金を返して一番大きい担保を受け取る形で、
   返せる額(借金 × closeFactor と、担保 ÷ 上乗せ の小さい方)と、利益の目安(返せる額 ×(上乗せ ×(1 − 仕組みの
   取り分)− 1))を出す。仕組みに「この清算をしてよいか」(liquidateBorrowAllowed)を聞き、担保の市場に払い出す
   現金があるかも見る(現金が無い市場では、受け取った担保を元のトークンに戻せない)。担保の値段は、仕組みの値段表と
   市場の値段(Aave の値段表)を比べる。仕組みの値段表だけが高いままの担保(崩れた安定通貨など)は、受け取っても
   売れないので、利益の目安は見かけだけになる(11/8、Arbitrum の dForce の USX)
5. 帳簿 chain_stranded に、初めて見た時刻と見た回数を残す。何日も続けて清算できるままなら、誰も見ていない見込みが高い

利益の目安は、交換の滑りとガス代を引かない粗い値(上乗せが読めない仕組みは1.08、closeFactor は0.5 とみなす)。
清算してよいかは今のブロックで聞くだけで、清算は試さない。利益の目安が0.5ドル未満の借り手には聞かない。
読み取りだけで、鍵もお金も使わず、取引も送らない。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from .. import config
from ..models import now_jst
from . import actors, venues
from .abi import (
    address,
    decode_addresses,
    decode_aggregate3,
    decode_string,
    encode_aggregate3,
    encode_call,
    topic,
    words,
)
from .chain import Chain
from .compound import DIALECTS, Dialect, Reader
from .rpc import AllBusy, CallReverted, RpcError

BORROWS = {   # 借りた記録(借り手は data の1語目)
    "Compound": topic("Borrow(address,uint256,uint256,uint256)"),
    "dForce": topic("Borrow(address,uint256,uint256,uint256,uint256)"),
}
MARKET_LISTS = {"Compound": "getAllMarkets()", "dForce": "getAlliTokens()"}
BORROW_KEY = "stranded_borrow"
NOBODY = "0x000000000000000000000000000000000000dEaD"   # 清算してよいかを聞く時の、清算する側(誰でもない住所)
DEFAULT_INCENTIVE = 1.08      # 上乗せが読めない仕組みの目安
DEFAULT_CLOSE = 0.5
FAIR_GAP = 0.05               # 担保の値段が市場とこの割合以内なら「合う」
MIN_ASK_USD = 0.5             # 利益の目安がこれより小さい借り手には、清算してよいかと現金を聞かない

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_stranded_borrowers (
    chain      TEXT NOT NULL,
    controller TEXT NOT NULL,
    borrower   TEXT NOT NULL,
    PRIMARY KEY (chain, controller, borrower)
);
CREATE TABLE IF NOT EXISTS chain_stranded (
    chain             TEXT NOT NULL,
    controller        TEXT NOT NULL,
    borrower          TEXT NOT NULL,
    first_seen        INTEGER NOT NULL,   -- 初めて清算できると見た時刻(秒)
    last_seen         INTEGER NOT NULL,
    runs              INTEGER NOT NULL,   -- 清算できると見た回数
    debt_usd          REAL,
    collateral_usd    REAL,
    repay_market      TEXT,
    repay_symbol      TEXT,
    collateral_market TEXT,
    collateral_symbol TEXT,
    repay_usd         REAL,               -- 1回で返せる額
    profit_usd        REAL,               -- 利益の目安(滑り・ガス代は引かない)
    allowed           INTEGER,            -- 仕組みの答え: 0 なら清算してよい。それ以外は断った番号。NULL は聞けなかった
    cash_ok           INTEGER,            -- 担保の市場に、受け取る担保の分の現金があるか
    market_ratio      REAL,               -- 担保の値段: 仕組みの値段表 ÷ 市場の値段(Aave の値段表)。NULL は比べられない
    repay_ratio       REAL,               -- 返すトークンの値段: 仕組みの値段表 ÷ 市場の値段。NULL は比べられない
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (chain, controller, borrower)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    venues.ensure_schema(conn)
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(chain_stranded)")}
    for col in ("market_ratio", "repay_ratio"):     # 2026-11-08・11-10 に足した列
        if col not in have:
            conn.execute(f"ALTER TABLE chain_stranded ADD COLUMN {col} REAL")
            conn.commit()


@dataclass
class Place:
    """調べる仕組み1つ。"""
    controller: str
    dialect: Dialect
    name: str
    markets: list[str]


class Finder:
    def __init__(self, conn: sqlite3.Connection, chain: Chain, *, reader: Reader, purpose: str = "research",
                 batch: int = 20, out: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        ensure_schema(conn)
        self.conn, self.chain, self.reader, self.purpose = conn, chain, reader, purpose
        self.batch, self.out, self.clock = batch, out, clock
        self.multicall = reader.multicall
        self._memo: dict[tuple, object] = {}

    @classmethod
    def from_config(cls, conn, chain: Chain, **kw) -> Finder:
        return cls(conn, chain, reader=Reader.from_config(conn, chain, out=kw.get("out", print)), **kw)

    # --- どこを調べるか ---
    def places(self, known: dict[str, str] | None = None) -> list[Place]:
        """清算の地図に載った Compound の写しと、config に書いた仕組み。"""
        found: dict[str, Place] = {}
        for name, ctrl in (known or {}).items():
            place = self._place(address(ctrl), name)
            if place:
                found[place.controller] = place
        emitters = [e for (e,) in self.conn.execute(
            "SELECT DISTINCT emitter FROM chain_venue_events WHERE chain = ? AND family = ?",
            (self.chain.name, venues.COMPTROLLER))]
        for e in emitters:
            got = self.reader.controller(e)
            if got is None or got[0] in found:
                continue
            symbols = [s for (s,) in self.conn.execute(
                "SELECT symbol FROM chain_venues WHERE chain = ? AND venue = ? AND symbol != '' LIMIT 3",
                (self.chain.name, got[0]))]
            place = self._place(got[0], "・".join(symbols) or "名前なし")
            if place:
                found[place.controller] = place
        return list(found.values())

    def _place(self, ctrl: str, name: str) -> Place | None:
        for d in DIALECTS:
            got = self.reader._call(ctrl, encode_call(MARKET_LISTS[d.name]))
            try:
                markets = decode_addresses(got) if got and got != "0x" else []
            except ValueError:
                markets = []
            if markets:
                return Place(ctrl, d, name, markets)
        self.out(f"  {name}({ctrl}): 市場の一覧が読めなかった")
        return None

    # --- 借り手 ---
    def discover(self, places: list[Place], from_block: int, to_block: int) -> int:
        """すべての仕組みの市場の借りた記録を、市場の住所を決めて1回の読み方でまとめて読み、借り手を帳簿に足す。
        新しく足した人数を返す。読み終えた範囲は仕組みの組ごとに残す(組が変わったら、範囲の初めから読み直す)。"""
        owner = {m.lower(): p for p in places for m in p.markets}
        key = BORROW_KEY + "@" + hashlib.sha256(
            ",".join(sorted(p.controller.lower() for p in places)).encode()).hexdigest()[:8]
        kinds = sorted({BORROWS[p.dialect.name] for p in places})
        added = 0
        for a, b in actors.plan(self.conn, self.chain.name, from_block, to_block, key=key):
            windows = max(1, -(-(b - a + 1) // actors.WINDOW_BLOCKS))
            every = max(10, windows // 10)
            start, done = a, 0
            while start <= b:
                end = min(b, start + actors.WINDOW_BLOCKS - 1)
                logs = self.chain.logs(from_block=start, to_block=end, address=sorted(owner), topics=[kinds],
                                       purpose=self.purpose)
                for lg in logs:
                    p = owner.get(lg.get("address", "").lower())
                    w = words(lg.get("data", "0x"))
                    if p and w and (lg.get("topics") or [None])[0] == BORROWS[p.dialect.name]:
                        added += self._add(p, address(f"0x{w[0] % 2**160:064x}"))
                self.conn.commit()
                actors.mark_progress(self.conn, self.chain.name, key, a, end)
                done += 1
                if done % every == 0 and done < windows:
                    self.out(f"    {done}/{windows} 区切りを読んだ(ブロック {end:,} まで、新しい借り手 {added:,}人)")
                start = end + 1
            actors.finish_progress(self.conn, self.chain.name, key, a)
        for p in places:          # 清算の地図に載った借り手も足す
            lowered = [m.lower() for m in p.markets]
            for (who,) in self.conn.execute(
                    f"""SELECT DISTINCT borrower FROM chain_venue_events WHERE chain = ? AND family = ?
                        AND borrower IS NOT NULL AND LOWER(emitter) IN ({','.join('?' * len(lowered))})""",
                    (self.chain.name, venues.COMPTROLLER, *lowered)):
                added += self._add(p, who)
        self.conn.commit()
        return added

    def _add(self, place: Place, who: str) -> int:
        cur = self.conn.execute("INSERT OR IGNORE INTO chain_stranded_borrowers(chain, controller, borrower) "
                                "VALUES (?,?,?)", (self.chain.name, place.controller, who))
        return cur.rowcount

    def borrowers(self, place: Place) -> list[str]:
        return [b for (b,) in self.conn.execute(
            "SELECT borrower FROM chain_stranded_borrowers WHERE chain = ? AND controller = ? ORDER BY borrower",
            (self.chain.name, place.controller))]

    # --- 今の状態 ---
    def _aggregate(self, calls: list[tuple[str, str]]) -> list[tuple[bool, str]] | None:
        """Multicall3 でまとめて読む。読めなければ None(呼ぶ側が小さく分けて読み直す)。"""
        if not self.multicall:
            return None
        try:
            got = self.chain.call(self.multicall, encode_aggregate3(calls), purpose=self.purpose)
        except AllBusy:
            raise
        except RpcError:        # 通らない・ガスの上限・答えが大きすぎる、など
            return None
        return decode_aggregate3(got) if got and got != "0x" else None

    def shortfalls(self, place: Place, users: list[str]) -> dict[str, int]:
        """利息を足してから読んだ不足額(0より大きい人だけ)。まとめて読めない時は半分ずつに分けて読み直す。"""
        d = place.dialect
        accrue = [(m, encode_call(d.accrue)) for m in place.markets]
        out: dict[str, int] = {}

        def read(group: list[str]) -> None:
            res = self._aggregate(accrue + [(place.controller, encode_call(d.liquidity, u)) for u in group])
            if res is None or len(res) != len(accrue) + len(group):
                if len(group) > 1:
                    half = len(group) // 2
                    read(group[:half])
                    read(group[half:])
                    return
                s = self.reader.shortfall(place.controller, d, group[0], "latest", place.markets)
                if s:
                    out[group[0]] = s
                return
            for u, (ok, data) in zip(group, res[len(accrue):]):
                w = words(data) if ok and data != "0x" else []
                if len(w) > d.shortfall_word and w[d.shortfall_word] > 0:
                    out[u] = w[d.shortfall_word]

        for i in range(0, len(users), self.batch):
            read(users[i:i + self.batch])
            done = min(len(users), i + self.batch)
            if done < len(users) and (i // self.batch + 1) % 50 == 0:
                self.out(f"    借り手 {done:,}/{len(users):,}人の不足額を読んだ(清算できる {len(out)}人)")
        return out

    def _once(self, key: tuple, read: Callable[[], object]):
        """1回の調べの間は同じ答え(今のブロックの値段・仕組みの設定)を覚えておき、借り手ごとに読み直さない。"""
        if key not in self._memo:
            self._memo[key] = read()
        return self._memo[key]

    def _mantissa(self, to: str, sig: str, default: float) -> float:
        def read() -> float:
            w = self.reader._words(to, sig)
            return w[0] / 1e18 if w and w[0] else default
        return self._once((to, sig), read)

    def _price(self, place: Place, market: str) -> float | None:
        return self._once(("price", market),
                          lambda: self.reader.usd_price(place.controller, place.dialect, market, "latest"))

    def measure(self, place: Place, user: str) -> dict:
        """清算できる借り手1人の、借金・担保・返せる額・利益の目安・清算してよいか・現金。"""
        r, ctrl, d = self.reader, place.controller, place.dialect
        mkts = r.assets_of(ctrl, d, user, "latest") or place.markets
        self._once(("unit", ctrl), lambda: r.unit(ctrl, d, tuple(place.markets), "latest"))
        accrue = [(m, encode_call(d.accrue)) for m in mkts]
        reads = []
        for m in mkts:
            reads += [(m, encode_call("borrowBalanceStored(address)", user)), (m, encode_call("balanceOf(address)", user)),
                      (m, encode_call("exchangeRateStored()"))]
        res = self._aggregate(accrue + reads)
        vals = res[len(accrue):] if res and len(res) == len(accrue) + len(reads) else None
        debt: dict[str, float] = {}
        coll: dict[str, float] = {}
        for i, m in enumerate(mkts):
            if vals:
                got = [words(data)[0] if ok and len(data) > 2 else 0 for ok, data in vals[3 * i:3 * i + 3]]
            else:
                got = [(r._words(m, s, *a) or [0])[0] for s, a in (("borrowBalanceStored(address)", (user,)),
                                                                   ("balanceOf(address)", (user,)),
                                                                   ("exchangeRateStored()", ()))]
            borrowed, held, rate = got
            px = self._price(place, m)
            _, dec = r.underlying(m)
            if px is None:
                continue
            if borrowed:
                debt[m] = borrowed / 10 ** dec * px
            if held and rate:
                coll[m] = held * rate / 1e18 / 10 ** dec * px
        row = {"debt_usd": sum(debt.values()) if debt else None, "collateral_usd": sum(coll.values()) if coll else None,
               "repay_market": None, "collateral_market": None, "repay_usd": None, "profit_usd": None,
               "allowed": None, "cash_ok": None, "market_ratio": None, "repay_ratio": None}
        if not debt or not coll:
            return row
        rm, cm = max(debt, key=debt.get), max(coll, key=coll.get)
        close = self._mantissa(ctrl, "closeFactorMantissa()", DEFAULT_CLOSE)
        incentive = self._mantissa(ctrl, "liquidationIncentiveMantissa()", DEFAULT_INCENTIVE)
        share = self._mantissa(cm, "protocolSeizeShareMantissa()", 0.0)
        repay = min(close * debt[rm], coll[cm] / incentive)
        row.update({"repay_market": rm, "collateral_market": cm, "repay_usd": repay,
                    "profit_usd": repay * (incentive * (1 - share) - 1)})
        # 担保の値段を市場(Aave の値段表)と比べる。仕組みの値段表だけが高いままの担保(崩れた安定通貨など)は、
        # 受け取っても売れないので、利益の目安は見かけだけになる(11/8 の dForce の USX)
        token, _ = r.underlying(cm)
        mkt = self._once(("market", token or "native"), lambda: self._market_price(token))
        c_now = self._price(place, cm)
        row["market_ratio"] = c_now / mkt if mkt and c_now else None
        r_token, _ = r.underlying(rm)       # 返すトークンも比べる(仕組みの値段表だけが安いと、市場で買う方が高くつく)
        r_mkt = self._once(("market", r_token or "native"), lambda: self._market_price(r_token))
        r_now = self._price(place, rm)
        row["repay_ratio"] = r_now / r_mkt if r_mkt and r_now else None
        if row["profit_usd"] < MIN_ASK_USD:       # 小さすぎる清算には、清算してよいかと現金を聞かない(読む回数を減らす)
            return row
        # 清算してよいか(Compound の呼び方だけ。利息を足してから聞く)と、担保の市場の現金
        r_px, c_px = self._price(place, rm), self._price(place, cm)
        _, r_dec = r.underlying(rm)
        _, c_dec = r.underlying(cm)
        if d.name == "Compound" and r_px:
            who = self._once((ctrl, "liquidatorContract()"), lambda: r._words(ctrl, "liquidatorContract()"))
            liquidator = address(f"0x{who[0]:064x}") if who and who[0] else NOBODY
            amount = int(repay / r_px * 10 ** r_dec)
            ask = encode_call("liquidateBorrowAllowed(address,address,address,address,uint256)",
                              rm, cm, liquidator, user, amount)
            res = self._aggregate([(rm, encode_call(d.accrue)), (cm, encode_call(d.accrue)), (ctrl, ask)])
            if res and len(res) == 3 and res[2][0] and len(res[2][1]) > 2:
                row["allowed"] = words(res[2][1])[0]
        cash = r._words(cm, "getCash()")
        if cash is not None and c_px:
            row["cash_ok"] = int(cash[0] / 10 ** c_dec * c_px >= repay * incentive)
        return row

    def _market_price(self, token: str | None) -> float | None:
        px, _ = self.reader.scanner.price(token or self.reader.scanner.weth, "latest")
        return px or None

    # --- 1回 ---
    def run(self, places: list[Place], *, from_block: int, to_block: int) -> dict:
        for p in places:
            self.out(f"  {p.name}({p.controller}・{p.dialect.name} の呼び方・市場 {len(p.markets)}か所)")
        self.out(f"  借りた記録を、市場 {sum(len(p.markets) for p in places)}か所に決めて読む"
                 f"(ブロック {from_block:,}〜{to_block:,})")
        added = self.discover(places, from_block, to_block)
        return {**self.check(places), "added": added}

    def add_borrowers(self, place: Place, users) -> int:
        """ほかの読み方で見つけた借り手を帳簿に足す(裾野の調べは、借りた記録を自分でまとめて読む)。"""
        added = sum(self._add(place, u) for u in users)
        self.conn.commit()
        return added

    def check(self, places: list[Place]) -> dict:
        """帳簿の借り手の今の状態を読み、今清算できる借り手を chain_stranded に残す。"""
        now = int(self.clock())
        self._memo.clear()
        total = {"places": len(places), "borrowers": 0, "open": 0, "gone": 0}
        for p in places:
            users = self.borrowers(p)
            total["borrowers"] += len(users)
            self.out(f"  {p.name}: 借り手 {len(users):,}人の、利息を足した後の不足額を読む")
            short = self.shortfalls(p, users)
            total["open"] += len(short)
            for user in short:
                m = self.measure(p, user)
                sym = {k: self._symbol(m[k]) for k in ("repay_market", "collateral_market")}
                self.conn.execute(
                    """INSERT INTO chain_stranded(chain, controller, borrower, first_seen, last_seen, runs, debt_usd,
                           collateral_usd, repay_market, repay_symbol, collateral_market, collateral_symbol, repay_usd,
                           profit_usd, allowed, cash_ok, market_ratio, repay_ratio, updated_at)
                       VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(chain, controller, borrower) DO UPDATE SET last_seen = excluded.last_seen,
                           runs = runs + 1, debt_usd = excluded.debt_usd, collateral_usd = excluded.collateral_usd,
                           repay_market = excluded.repay_market, repay_symbol = excluded.repay_symbol,
                           collateral_market = excluded.collateral_market,
                           collateral_symbol = excluded.collateral_symbol, repay_usd = excluded.repay_usd,
                           profit_usd = excluded.profit_usd, allowed = excluded.allowed, cash_ok = excluded.cash_ok,
                           market_ratio = excluded.market_ratio, repay_ratio = excluded.repay_ratio,
                           updated_at = excluded.updated_at""",
                    (self.chain.name, p.controller, user, now, now, m["debt_usd"], m["collateral_usd"],
                     m["repay_market"], sym["repay_market"], m["collateral_market"], sym["collateral_market"],
                     m["repay_usd"], m["profit_usd"], m["allowed"], m["cash_ok"], m["market_ratio"],
                     m["repay_ratio"], now_jst().isoformat(timespec="seconds")))
            gone = self.conn.execute(
                f"""DELETE FROM chain_stranded WHERE chain = ? AND controller = ?
                    AND borrower NOT IN ({','.join('?' * len(short)) or "''"})""",
                (self.chain.name, p.controller, *short)).rowcount
            total["gone"] += gone
            self.conn.commit()
        return total

    def _symbol(self, market: str | None) -> str:
        """市場の元のトークンの名前(元のトークンが無い ETH などの市場は、市場の名前)。"""
        if not market:
            return ""
        token, _ = self.reader.underlying(market)
        if token:
            try:
                return self.reader.scanner.token(token)[0]
            except (CallReverted, ValueError, IndexError):
                return ""
        got = self.reader._call(market, encode_call("symbol()"))
        try:
            return decode_string(got) if got and got != "0x" else ""
        except ValueError:
            return ""


# --- まとめ ---
def summarize(conn: sqlite3.Connection, chain: str) -> list[dict]:
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM chain_stranded WHERE chain = ? ORDER BY COALESCE(profit_usd, 0) DESC",
                        (chain,)).fetchall()
    return [dict(r) for r in rows]


def money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "値段が読めない"


def _ratio(v: float | None) -> str:
    return "比べられない" if v is None else f"{v:.2f}倍"


def _allowed(code) -> str:
    return "聞けなかった" if code is None else "できる" if code == 0 else f"断られた(番号 {code})"


def describe(rows: list[dict], totals: dict, *, chain: str, days: float, top: int = 20,
             now: float | None = None) -> list[str]:
    now = now or time.time()
    out = [(f"取り残し(Compound の写し・{chain}): 仕組み {totals['places']}か所、借り手 {totals['borrowers']:,}人"
            f"(借りた記録は直近{days:g}日と、清算の地図から)")]
    if not rows:
        return out + ["  今清算できる借り手はいない"]
    ok = [r for r in rows if r["allowed"] == 0]
    fair = [r for r in rows if r.get("market_ratio") is not None and abs(r["market_ratio"] - 1) <= FAIR_GAP]
    total = sum(r["profit_usd"] or 0.0 for r in rows)
    out.append(f"  今清算できる借り手 {len(rows)}人。利益の目安の合計 ${total:,.2f}"
               f"(仕組みが清算してよいと答えた分 {len(ok)}人・${sum(r['profit_usd'] or 0.0 for r in ok):,.2f}、"
               f"担保の値段が市場と{FAIR_GAP:.0%}以内で合う分 {len(fair)}人・"
               f"${sum(r['profit_usd'] or 0.0 for r in fair):,.2f})"
               + (f"。前回から消えた {totals['gone']}人" if totals.get("gone") else ""))
    out.append("  順位 仕組み 借り手 借金(一番大きい市場) 担保(一番大きい市場) 1回で返せる額 利益の目安 清算してよいか "
               "担保の市場の現金 担保の値段(仕組み÷市場) 続いている")
    for i, r in enumerate(rows[:top], 1):
        days_open = (now - r["first_seen"]) / 86400
        cash = "-" if r["cash_ok"] is None else "足りる" if r["cash_ok"] else "足りない"
        out.append(f"  {i:>3}. {r['controller']}  {r['borrower']}  {money(r['debt_usd'])}({r['repay_symbol'] or '-'})  "
                   f"{money(r['collateral_usd'])}({r['collateral_symbol'] or '-'})  {money(r['repay_usd'])}  "
                   f"{money(r['profit_usd'])}  {_allowed(r['allowed'])}  {cash}  {_ratio(r.get('market_ratio'))}  "
                   f"初めて見てから{days_open:.1f}日({r['runs']}回)")
    if len(rows) > top:
        rest = rows[top:]
        out.append(f"  ほか {len(rest)}人(利益の目安 ${sum(r['profit_usd'] or 0.0 for r in rest):,.2f})")
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["controller"], []).append(r)
    out.append("  仕組みごと(今清算できる借り手・利益の目安):")
    for ctrl, rs in sorted(by.items(), key=lambda kv: -sum(r["profit_usd"] or 0.0 for r in kv[1])):
        out.append(f"    {ctrl}  {len(rs)}人  ${sum(r['profit_usd'] or 0.0 for r in rs):,.2f}")
    return out


def load_known(chain_name: str) -> dict[str, str]:
    return config.load_config()["onchain"]["chains"][chain_name].get("compound_comptrollers") or {}
