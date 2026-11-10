"""裾野のチェーンの調べ(2026-11-10 ボスの選択「A 裾野を試す」。報告書 strategy-2026-11-10.md の4章)。

第0期で読んだ8チェーンは、お金のある機会がどれも同じブロックで取られていた。作者の稼ぎが伸びたのは、チェーンを
18から29に増やした時期と重なる(推定: 専業の主体が来ない小さいチェーン)。そこで、貸し借りの仕組みの写し
(Compound の写し・Aave の写し)を小さいチェーンも含めて広く読み、「遅い主体でも取れたお金」があるかを、
期限つき(11/24 まで)で確かめる。

チェーンごとに:
1. 接続先: 公開の接続先を直接確かめ、チェーン番号が合い、答えの速い順に何本かを中継に入れる
2. 仕組み: 材料(targets.json。DefiLlama の一覧から作った)の住所をチェーン上で確かめ、市場・プールに直す
   (Compound の写しは市場の一覧、Aave の写しは住所録 → プール。読めない住所は「読めない」と残して外す)
3. 記録: 清算の記録と借りた記録を、仕組みの住所を決めて、新しい方から1日分ずつさかのぼって読む
   (古い記録を読ませない接続先が多いので、読めた所までを「読めた日数」として残す)
4. 清算: 返した額と受け取った担保を、その時刻の市場の値段でドルにする。受け取った額 − 返した額 が、清算した
   主体の粗い利益(ガス代は引かない)。これが「遅くても取れたお金」の上限になる(速さは、お金のある所だけ後で読む)
5. 取り残し: 借り手の今の状態を読み、今清算できるのに誰も取っていない借り手を数える(Compound の写しは
   stranded.py と同じ読み方、Aave の写しは健康度)。担保と返すトークンの値段を市場と比べ、市場の値段で見ても
   利益が残り、担保を引き出す現金もある分だけを「取れる」とする

小さいチェーンには Aave の値段表が無いので、市場の値段は DefiLlama の値段表(coins.llama.fi)から読む。
外のデータなので数字だけを読み、確からしさの低い値段と古い値段は使わない。
読み取りだけで、鍵もお金も使わず、取引も送らない。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .. import config
from . import compound, stranded, venues
from .abi import (
    address,
    decode_addresses,
    decode_aggregate3,
    decode_string,
    encode_aggregate3,
    encode_call,
    to_int,
    topic,
    words,
)
from .chain import Chain
from .rpc import AllBusy, Relay, RpcError, probe

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
NATIVE = "0x" + "0" * 40              # 値段表での、チェーンの元の通貨(ETH・BNB など)
LIQ_COMPOUND = venues.FAMILY_BY_NAME[venues.COMPTROLLER].topic0
LIQ_AAVE = venues.FAMILY_BY_NAME["Aave型"].topic0
BORROW_COMPOUND = stranded.BORROWS["Compound"]
BORROW_AAVE = (topic("Borrow(address,address,address,uint256,uint256,uint256,uint16)"),    # Aave v2 の写し
               topic("Borrow(address,address,address,uint256,uint8,uint256,uint16)"))      # Aave v3 の写し
ADDRESS_STEP = 100                    # 記録を1回で読む住所の数
AAVE_BONUS = 1.05                     # 上乗せが読めない Aave の写しの目安
AAVE_CLOSE = 0.5
DEFAULTS = {"days": 30, "minutes_per_chain": 30, "workers": 6, "max_endpoints": 6, "probe_endpoints": 16,
            "prices_url": "https://coins.llama.fi", "min_confidence": 0.9, "max_price_age_hours": 24,
            "max_borrowers": 3000, "exclude_hosts": []}


def settings() -> dict:
    return {**DEFAULTS, **(config.load_config().get("onchain", {}).get("tail") or {})}


def _addr(n: int) -> str:
    return address(f"0x{n % 2**160:064x}")


def _clean(text: str, limit: int = 20) -> str:
    """外から来た名前(トークンの記号など)は、文字と数字と少しの記号だけにして短くする。"""
    return "".join(ch for ch in str(text) if ch.isalnum() or ch in "._-+ ")[:limit]


# --- 市場の値段 ---
@dataclass(frozen=True)
class Quote:
    price: float
    decimals: int | None
    symbol: str


class Prices:
    """市場の値段(DefiLlama の値段表)。今の値段と、ある時刻の値段(bucket 秒ごとにまとめて読む)。"""

    def __init__(self, client: httpx.Client, chain_key: str, *, base: str = DEFAULTS["prices_url"], batch: int = 40,
                 min_confidence: float = 0.9, max_age: float = 86400.0, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.client, self.chain_key, self.base, self.batch = client, chain_key, base.rstrip("/"), batch
        self.min_confidence, self.max_age, self.clock, self.sleep = min_confidence, max_age, clock, sleep
        self._now: dict[str, Quote | None] = {}
        self._past: dict[tuple[int, str], Quote | None] = {}
        self.calls = self.failed = 0

    def _fetch(self, path: str, tokens: list[str], at: float) -> dict[str, Quote]:
        coins = ",".join(f"{self.chain_key}:{t}" for t in tokens)
        data = None
        for attempt in range(2):
            self.calls += 1
            try:
                r = self.client.get(f"{self.base}/{path}/{coins}")
                if r.status_code == 200:
                    data = r.json()
                    break
            except (httpx.HTTPError, ValueError):
                pass
            if attempt == 0:
                self.sleep(2.0)
        if not isinstance(data, dict) or not isinstance(data.get("coins"), dict):
            self.failed += 1
            return {}
        out: dict[str, Quote] = {}
        for key, v in data["coins"].items():
            try:
                token = str(key).split(":", 1)[1].lower()
                price, conf = float(v["price"]), float(v.get("confidence", 1.0))
                seen = float(v.get("timestamp", at))
                dec = v.get("decimals")
            except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                continue
            if not 0 < price < 1e12 or conf < self.min_confidence or abs(at - seen) > self.max_age:
                continue        # 確からしさの低い値段・古い値段は、値段なしとして扱う
            out[token] = Quote(price, int(dec) if isinstance(dec, int) and 0 <= dec <= 36 else None,
                               _clean(v.get("symbol") or ""))
        return out

    def current(self, tokens) -> dict[str, Quote]:
        want = list(dict.fromkeys(t.lower() for t in tokens))
        missing = [t for t in want if t not in self._now]
        for i in range(0, len(missing), self.batch):
            group = missing[i:i + self.batch]
            got = self._fetch("prices/current", group, self.clock())
            for t in group:
                self._now[t] = got.get(t)
        return {t: self._now[t] for t in want if self._now.get(t)}

    def load_past(self, needs: dict[int, set[str]]) -> None:
        """needs: 時刻(秒)→ トークン。時刻ごとに1回で読んで覚える。"""
        for at, tokens in sorted(needs.items()):
            missing = sorted(t.lower() for t in tokens if (at, t.lower()) not in self._past)
            for i in range(0, len(missing), self.batch):
                group = missing[i:i + self.batch]
                got = self._fetch(f"prices/historical/{at}", group, at)
                for t in group:
                    self._past[(at, t)] = got.get(t)

    def past(self, at: int, token: str) -> Quote | None:
        return self._past.get((at, token.lower()))


class MarketScanner:
    """compound.Reader と stranded.Finder が値段と名前を読む相手(Aave の値段表を読む actors.Scanner の代わり)。"""

    def __init__(self, chain: Chain, prices: Prices, purpose: str = "research"):
        self.chain, self.prices, self.purpose = chain, prices, purpose
        self.weth = NATIVE
        self._tokens: dict[str, tuple[str, int]] = {}

    def price(self, asset: str, block="latest") -> tuple[float | None, str]:
        q = self.prices.current([asset]).get(asset.lower())
        return (q.price if q else None), "block"

    def token(self, asset: str) -> tuple[str, int]:
        """(名前, 桁数)。桁数はチェーンから読み、読めなければ値段表の値、それも無ければ18。"""
        key = asset.lower()
        if key not in self._tokens:
            q = self.prices.current([key]).get(key)
            dec, sym = (q.decimals if q else None), (q.symbol if q else "")
            if key != NATIVE:
                try:
                    got = self.chain.call(key, encode_call("decimals()"), purpose=self.purpose)
                    if got and got != "0x" and words(got)[0] <= 36:
                        dec = words(got)[0]
                except AllBusy:
                    raise
                except (RpcError, ValueError):
                    pass
                if not sym:
                    try:
                        sym = _clean(decode_string(self.chain.call(key, encode_call("symbol()"),
                                                                   purpose=self.purpose)))
                    except AllBusy:
                        raise
                    except (RpcError, ValueError):
                        sym = ""
            self._tokens[key] = (sym or key[:10], 18 if dec is None else int(dec))
        return self._tokens[key]


def consensus(fs: list[float], hint: float | None = None, gap: float = 0.1) -> float | None:
    """値段表の単位を、市場ごとの「市場の値段 ÷ 値段表の値」から決める。いちばん多くの市場が合う値を取る
    (1つの市場だけで決めると、崩れたトークンに引きずられる)。hint(ドル建てならこの値)に合う市場の数が
    それと同じ以上なら hint。"""
    if not fs:
        return None

    def support(x: float) -> int:
        return sum(abs(f / x - 1) <= gap for f in fs)
    med = statistics.median(fs)
    best = max(fs, key=lambda f: (support(f), -abs(f / med - 1)))
    if hint and support(hint) >= support(best):
        return hint
    return statistics.median([f for f in fs if abs(f / best - 1) <= gap])


class TailReader(compound.Reader):
    """値段表の単位を、多くの市場が合う値で決める。どの接続先も答えられない呼び出しは「読めない」として先へ進む
    (小さいチェーンには、通らない呼び出しを決まった形で返さない接続先がある)。"""

    def _call(self, to, data, block="latest"):
        try:
            return super()._call(to, data, block)
        except AllBusy:
            raise
        except RpcError:
            return None

    def unit(self, ctrl, d, markets, block):
        if ctrl in self._units:
            return self._units[ctrl]
        fs = []
        for m in markets:
            token, _ = self.underlying(m)
            raw = self.raw_price(ctrl, d, m, block)
            ref, _ = self.scanner.price(token or self.scanner.weth, block)
            if raw and ref:
                fs.append(ref / raw)
        f = consensus(fs, hint=1.0)
        if f is None:
            return None
        self._units[ctrl] = ("ドル", 1.0) if f == 1.0 else ("倍", f)
        return self._units[ctrl]


# --- 接続先 ---
def load_domains(path: str | Path | None) -> list[str] | None:
    """許可リスト(1行1ドメイン。*.example.com は example.com と同じに読む)。None なら絞らない。"""
    if not path:
        return None
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return sorted({ln.strip().removeprefix("*.").lower() for ln in lines if ln.strip() and not ln.startswith("#")})


def _in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def usable(url: str, exclude=(), allow=None) -> bool:
    """鍵の要らない https の接続先か(鍵を入れる形の URL と、外す・許可していないホストは使わない)。"""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or any(c in url for c in "${}<>"):
        return False
    return not _in(host, exclude) and (allow is None or _in(host, allow))


def pick_endpoints(client: httpx.Client, urls: list[str], chain_id: int, *, most: int = 6,
                   probe_most: int = 16) -> tuple[list[str], list[str]]:
    """接続先を直接確かめ、チェーン番号が合うものを答えの速い順に most 本。(使う接続先, 外した理由)を返す。"""
    cands = list(dict.fromkeys(urls))[:probe_most]
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = list(pool.map(lambda u: probe(client, u), cands))
    good = sorted((g for g in got if g["ok"] and g["chain_id"] == chain_id), key=lambda g: g["ms"] or 0)
    bad = [f"{urlparse(g['url']).hostname}: " + (g["error"] or f"チェーン番号が違う({g['chain_id']})")
           for g in got if not (g["ok"] and g["chain_id"] == chain_id)]
    return [g["url"] for g in good[:most]], bad


# --- 取り残しの利益を市場の値段で見る ---
def market_profit(row: dict) -> float | None:
    """市場の値段で見た利益の目安。仕組みの値段表で R ドル返すと、R × 上乗せ の担保を受け取る。市場の値段では、
    返すトークンは R ÷ rd、担保は R × 上乗せ ÷ rc(rd・rc は 仕組みの値段 ÷ 市場の値段)。比べられなければ None。"""
    rc, rd, repay, profit = row.get("market_ratio"), row.get("repay_ratio"), row.get("repay_usd"), row.get("profit_usd")
    if not rc or not rd or not repay or profit is None:
        return None
    return repay * (profit / repay + 1) / rc - repay / rd


def takeable(row: dict) -> bool:
    """市場の値段で見ても利益が残り、担保を引き出す現金があり、仕組みが清算を断っていない。"""
    mp = market_profit(row)
    return mp is not None and mp > 0 and row.get("cash_ok") != 0 and row.get("allowed") in (None, 0)


# --- Aave の写し ---
@dataclass
class AavePlace:
    name: str
    source: str            # 材料に書いてあった住所
    pool: str
    oracle: str | None
    reserves: list[str]
    insolvent: bool = False


class Surveyor:
    """1つのチェーンを調べる。"""

    def __init__(self, conn: sqlite3.Connection, chain: Chain, prices: Prices, *, days: float = 30,
                 minutes: float = 30, out: Callable[[str], None] = print, purpose: str = "research",
                 clock: Callable[[], float] = time.time, wall: Callable[[], float] = time.monotonic,
                 batch: int = 50, max_borrowers: int = 3000):
        self.conn, self.chain, self.prices, self.days, self.minutes = conn, chain, prices, days, minutes
        self.out, self.purpose, self.clock, self.wall, self.batch = out, purpose, clock, wall, batch
        self.max_borrowers = max_borrowers
        self.scanner = MarketScanner(chain, prices, purpose)
        self.multicall: str | None = None
        self.reader: TailReader | None = None
        self.finder: stranded.Finder | None = None
        self._reserve: dict[tuple[str, str], dict | None] = {}
        self.result: dict = {}          # 途中で読めなくなっても、そこまでの結果を残す

    # --- 読み取り ---
    def _raw(self, to: str, sig: str, *args) -> str | None:
        """呼び出しの答え。通らない・どの接続先も答えられない時は None(どの接続先も休んでいる時だけ、上に伝える)。"""
        try:
            got = self.chain.call(to, encode_call(sig, *args), purpose=self.purpose)
        except AllBusy:
            raise
        except RpcError:
            return None
        return got if got and got != "0x" else None

    def _w(self, to: str, sig: str, *args) -> list[int] | None:
        got = self._raw(to, sig, *args)
        try:
            return words(got) if got else None
        except ValueError:
            return None

    def _list(self, to: str, sig: str) -> list[str]:
        got = self._raw(to, sig)
        try:
            return decode_addresses(got) if got else []
        except (ValueError, IndexError):
            return []

    def _aggregate(self, calls: list[tuple[str, str]]) -> list[tuple[bool, str]]:
        """まとめて読む。Multicall3 が無い・通らない時は1つずつ読む。"""
        if self.multicall and calls:
            try:
                got = self.chain.call(self.multicall, encode_aggregate3(calls), purpose=self.purpose)
                res = decode_aggregate3(got) if got and got != "0x" else []
                if len(res) == len(calls):
                    return res
            except AllBusy:
                raise
            except (RpcError, ValueError, IndexError):
                pass
        out = []
        for to, data in calls:
            try:
                got = self.chain.call(to, data, purpose=self.purpose)
                out.append((bool(got) and got != "0x", got or "0x"))
            except AllBusy:
                raise
            except RpcError:
                out.append((False, "0x"))
        return out

    def prepare(self) -> None:
        code = self.chain.code(MULTICALL3, purpose=self.purpose)
        self.multicall = MULTICALL3 if code and code != "0x" else None
        self.reader = TailReader(self.conn, self.chain, scanner=self.scanner, multicall=self.multicall,
                                 purpose=self.purpose, out=self.out)
        self.finder = stranded.Finder(self.conn, self.chain, reader=self.reader, purpose=self.purpose,
                                      out=self.out, clock=self.clock)

    # --- 仕組みを確かめる ---
    def compound_place(self, name: str, ctrl: str) -> stranded.Place | None:
        return self.finder._place(address(ctrl), name)

    def aave_places(self, name: str, source: str, insolvent: bool = False) -> list[AavePlace]:
        """材料の住所は、プールそのもの・住所録の一覧・データの窓口のどれかなので、順に試してプールに直す。"""
        source = address(source)
        pools: list[str] = []
        if self._list(source, "getReservesList()"):
            pools = [source]
        else:
            providers = self._list(source, "getAddressesProvidersList()")
            if not providers:
                w = self._w(source, "ADDRESSES_PROVIDER()")
                providers = [_addr(w[0])] if w and w[0] else []
            for p in providers:
                for sig in ("getPool()", "getLendingPool()"):
                    w = self._w(p, sig)
                    pool = _addr(w[0]) if w and w[0] else None
                    if pool and pool not in pools and self._list(pool, "getReservesList()"):
                        pools.append(pool)
                        break
        places = []
        for i, pool in enumerate(pools):
            oracle = None
            for sig in ("ADDRESSES_PROVIDER()", "getAddressesProvider()"):
                w = self._w(pool, sig)
                if w and w[0]:
                    o = self._w(_addr(w[0]), "getPriceOracle()")
                    oracle = _addr(o[0]) if o and o[0] else None
                    break
            places.append(AavePlace(name if len(pools) == 1 else f"{name}#{i + 1}", source, pool, oracle,
                                    self._list(pool, "getReservesList()"), insolvent))
        return places

    # --- 記録を読む ---
    def sweep(self, addresses: list[str], head: int, block_seconds: float) -> tuple[list[dict], float, str]:
        """清算の記録と借りた記録を、新しい方から1日分ずつ読む。(記録, 読めた日数, 止まった理由)を返す。"""
        kinds = [LIQ_COMPOUND, LIQ_AAVE, BORROW_COMPOUND, *BORROW_AAVE]
        per_day = max(1, int(86400 / block_seconds))
        first = max(0, head - int(self.days * per_day) + 1)
        deadline = self.wall() + self.minutes * 60
        logs: list[dict] = []
        end, covered, why = head, head + 1, ""
        if len(addresses) > ADDRESS_STEP and self.chain.addr_step is None:
            self.chain.addr_step = ADDRESS_STEP     # 住所が多い時は、初めから分けて読む(多すぎる住所を断る接続先がある)
        while end >= first:
            # 1日分ずつ。ただし1回で読めるブロック数が狭い接続先では、時間切れを確かめられるように小さく刻む
            start = max(first, end - min(per_day, 20 * self.chain.log_step) + 1)
            try:
                logs.extend(self.chain.logs(from_block=start, to_block=end, address=sorted(addresses),
                                            topics=[kinds], purpose=self.purpose, cache=False))
            except RpcError as e:
                why = f"記録が読めなくなった({str(e)[:160]})"
                break
            covered, end = start, start - 1
            if self.wall() > deadline and end >= first:
                why = f"時間切れ({self.minutes:g}分)"
                break
        return logs, (head + 1 - covered) * block_seconds / 86400, why

    def _times(self, blocks: list[int]) -> dict[int, int]:
        """ブロックの時刻。多い時は200か所だけ読み、間は直線で埋める。"""
        blocks = sorted(set(blocks))
        if not blocks:
            return {}
        step = max(1, -(-len(blocks) // 200))
        anchors = sorted({*blocks[::step], blocks[-1]})
        ts = {b: to_int((self.chain.block(b, purpose=self.purpose) or {}).get("timestamp", "0x0")) for b in anchors}
        out = {}
        for b in blocks:
            lo = max(a for a in anchors if a <= b)
            hi = min(a for a in anchors if a >= b)
            out[b] = ts[lo] if hi == lo else int(ts[lo] + (ts[hi] - ts[lo]) * (b - lo) / (hi - lo))
        return out

    def parse(self, logs: list[dict], compounds: list[stranded.Place], aaves: list[AavePlace]):
        """記録を、清算(仕組みごと)と借り手(仕組みごと)に分ける。"""
        c_owner = {m.lower(): p for p in compounds for m in p.markets}
        a_owner = {p.pool.lower(): p for p in aaves}
        liqs: list[dict] = []
        borrowers: dict[str, set[str]] = {}
        seen = set()
        for lg in logs:
            emitter = str(lg.get("address", "")).lower()
            t = lg.get("topics") or [None]
            ident = (lg.get("transactionHash"), lg.get("logIndex"))
            if ident in seen:
                continue
            seen.add(ident)
            try:
                w = words(lg.get("data", "0x"))
                base = {"block": to_int(lg["blockNumber"]), "tx": lg.get("transactionHash")}
                if emitter in c_owner:
                    p = c_owner[emitter]
                    if t[0] == BORROW_COMPOUND and w:
                        borrowers.setdefault(p.controller, set()).add(_addr(w[0]))
                    elif t[0] == LIQ_COMPOUND and len(w) >= 5:
                        borrowers.setdefault(p.controller, set()).add(_addr(w[1]))
                        liqs.append({**base, "place": p.controller, "kind": "Compound", "liquidator": _addr(w[0]),
                                     "borrower": _addr(w[1]), "debt_market": emitter, "repay": w[2],
                                     "coll_market": _addr(w[3]), "seize": w[4]})
                elif emitter in a_owner:
                    p = a_owner[emitter]
                    if t[0] in BORROW_AAVE and len(t) >= 3:
                        borrowers.setdefault(p.pool, set()).add(address(t[2]))
                    elif t[0] == LIQ_AAVE and len(t) >= 4 and len(w) >= 3:
                        borrowers.setdefault(p.pool, set()).add(address(t[3]))
                        liqs.append({**base, "place": p.pool, "kind": "Aave", "liquidator": _addr(w[2]),
                                     "borrower": address(t[3]), "debt_token": address(t[2]), "repay": w[0],
                                     "coll_token": address(t[1]), "seize": w[1]})
            except (ValueError, KeyError, TypeError):
                continue
        return liqs, borrowers

    def value(self, liqs: list[dict]) -> None:
        """清算の返した額と受け取った担保を、その時刻(5分刻み。多ければ1時間刻み)の市場の値段でドルにする。"""
        if not liqs:
            return
        times = self._times([lq["block"] for lq in liqs])
        bucket = 300 if len(liqs) <= 300 else 3600
        rates: dict[str, int | None] = {}
        needs: dict[int, set[str]] = {}
        for lq in liqs:
            lq["ts"] = times[lq["block"]]
            if lq["kind"] == "Compound":
                d_token, d_dec = self.reader.underlying(lq.pop("debt_market"))
                cm = lq.pop("coll_market")
                c_token, c_dec = self.reader.underlying(cm)
                if cm not in rates:
                    r = self._w(cm, "exchangeRateStored()")
                    rates[cm] = r[0] if r else None
                lq["debt_token"], lq["coll_token"] = d_token or NATIVE, c_token or NATIVE
                lq["repay_amount"] = lq.pop("repay") / 10 ** d_dec
                lq["seize_amount"] = lq.pop("seize") * rates[cm] / 1e18 / 10 ** c_dec if rates[cm] else None
            else:
                lq["repay_amount"] = lq.pop("repay") / 10 ** self.scanner.token(lq["debt_token"])[1]
                lq["seize_amount"] = lq.pop("seize") / 10 ** self.scanner.token(lq["coll_token"])[1]
            lq["at"] = lq["ts"] // bucket * bucket
            needs.setdefault(lq["at"], set()).update((lq["debt_token"], lq["coll_token"]))
        self.prices.load_past(needs)
        for lq in liqs:
            at = lq.pop("at")
            dq, cq = self.prices.past(at, lq["debt_token"]), self.prices.past(at, lq["coll_token"])
            lq["debt_symbol"] = dq.symbol if dq else ""
            lq["coll_symbol"] = cq.symbol if cq else ""
            lq["repay_usd"] = lq["repay_amount"] * dq.price if dq else None
            lq["seize_usd"] = lq["seize_amount"] * cq.price if cq and lq["seize_amount"] is not None else None

    # --- 取り残し(Aave の写し) ---
    def _reserve_data(self, place: AavePlace, asset: str) -> dict | None:
        """市場のトークンの住所と上乗せ。v2 の写しと v3 の写しで答えの並びが1語ずれるので、8語目の形で見分ける。"""
        key = (place.pool, asset)
        if key not in self._reserve:
            w = self._w(place.pool, "getReserveData(address)", asset)
            found = None
            if w and len(w) >= 11:
                i = 7 if w[7] >= 2**64 else 8          # v2 は8語目が aToken、v3 は8語目が番号(小さい数)
                bonus = (w[0] >> 32 & 0xFFFF) / 1e4
                found = {"a": _addr(w[i]), "stable": _addr(w[i + 1]), "variable": _addr(w[i + 2]),
                         "bonus": bonus if 1 < bonus < 2 else AAVE_BONUS}
            self._reserve[key] = found
        return self._reserve[key]

    def aave_stranded(self, place: AavePlace, users: list[str]) -> list[dict]:
        """健康度が1を割っている借り手。値段表の単位は、値段表が言う単位(BASE_CURRENCY_UNIT)か、多くの市場が合う値。"""
        found: dict[str, tuple[int, int]] = {}
        for i in range(0, len(users), self.batch):
            group = users[i:i + self.batch]
            res = self._aggregate([(place.pool, encode_call("getUserAccountData(address)", u)) for u in group])
            for u, (ok, data) in zip(group, res):
                try:
                    w = words(data) if ok else []
                except ValueError:
                    w = []
                if len(w) >= 6 and w[1] > 0 and w[5] < 10**18:
                    found[u] = (w[0], w[1])
        if not found:
            return []
        quotes = self.prices.current(place.reserves)
        raw = {}
        for a in place.reserves:
            w = self._w(place.oracle, "getAssetPrice(address)", a) if place.oracle else None
            raw[a] = w[0] if w and w[0] else None
        fs = [quotes[a].price / raw[a] for a in place.reserves if a in quotes and raw[a]]
        base = self._w(place.oracle, "BASE_CURRENCY_UNIT()") if place.oracle else None
        unit = consensus(fs, hint=1 / base[0] if base and base[0] else None)
        rows = []
        for user, (coll, debt) in found.items():
            row = {"place": place.pool, "kind": "Aave", "borrower": user,
                   "debt_usd": debt * unit if unit else None, "collateral_usd": coll * unit if unit else None,
                   "repay_symbol": "", "collateral_symbol": "", "repay_usd": None, "profit_usd": None,
                   "allowed": None, "cash_ok": None, "market_ratio": None, "repay_ratio": None}
            if unit:
                rough = min(AAVE_CLOSE * row["debt_usd"], row["collateral_usd"] / AAVE_BONUS)
                row["repay_usd"], row["profit_usd"] = rough, rough * (AAVE_BONUS - 1)
                if row["profit_usd"] >= stranded.MIN_ASK_USD:      # 小さすぎる借り手の中身は読まない
                    self._aave_detail(place, user, unit, raw, quotes, row)
            rows.append(row)
        return rows

    def _aave_detail(self, place: AavePlace, user: str, unit: float, raw: dict, quotes: dict, row: dict) -> None:
        """借り手の一番大きい担保と借金を読み、値段を市場と比べ、担保を引き出す現金があるかを見る。"""
        metas = [(a, self._reserve_data(place, a)) for a in place.reserves]
        metas = [(a, m) for a, m in metas if m and raw.get(a)]
        calls = []
        for _, m in metas:
            calls += [(m[k], encode_call("balanceOf(address)", user)) for k in ("a", "variable", "stable")]
        res = self._aggregate(calls)
        coll: dict[str, float] = {}
        debt: dict[str, float] = {}
        for i, (a, _) in enumerate(metas):
            got = []
            for ok, data in res[3 * i:3 * i + 3]:
                try:
                    got.append(words(data)[0] if ok and len(data) > 2 else 0)
                except ValueError:
                    got.append(0)
            px = raw[a] * unit / 10 ** self.scanner.token(a)[1]
            if got[0]:
                coll[a] = got[0] * px
            if got[1] + got[2]:
                debt[a] = (got[1] + got[2]) * px
        if not coll or not debt:
            return
        cm, rm = max(coll, key=coll.get), max(debt, key=debt.get)
        bonus = dict(metas)[cm]["bonus"]
        repay = min(AAVE_CLOSE * debt[rm], coll[cm] / bonus)
        c_dec = self.scanner.token(cm)[1]
        row.update({"repay_usd": repay, "profit_usd": repay * (bonus - 1),
                    "repay_symbol": self.scanner.token(rm)[0], "collateral_symbol": self.scanner.token(cm)[0],
                    "market_ratio": raw[cm] * unit / quotes[cm].price if cm in quotes else None,
                    "repay_ratio": raw[rm] * unit / quotes[rm].price if rm in quotes else None})
        cash = self._w(cm, "balanceOf(address)", dict(metas)[cm]["a"])
        if cash is not None:
            row["cash_ok"] = int(cash[0] * raw[cm] * unit / 10 ** c_dec >= repay * bonus)

    # --- 1チェーン ---
    def run(self, target: dict) -> dict:
        started = self.wall()
        self.result = result = {"chain": target["chain"], "chain_id": target["id"], "days_asked": self.days, "places": [],
                        "liquidations": [], "stranded": [], "status": "ok", "note": ""}
        self.prepare()
        compounds: list[stranded.Place] = []
        aaves: list[AavePlace] = []
        info: dict[str, dict] = {}

        def note(kind, name, source, insolvent, key=None, markets=0):
            row = {"name": name, "kind": kind, "address": key or address(source), "insolvent": bool(insolvent),
                   "markets": markets, "status": "ok" if key else "読めない"}
            result["places"].append(row)
            if key:
                info[key] = row

        for name, _, ctrl, insolvent in target.get("compound", []):
            p = self.compound_place(name, ctrl)
            if p is None:
                note("Compound", name, ctrl, insolvent)
            elif p.controller not in info:
                compounds.append(p)
                note("Compound", name, ctrl, insolvent, p.controller, len(p.markets))
        for name, _, source, insolvent in target.get("aave", []):
            found = [p for p in self.aave_places(name, source, insolvent) if p.pool not in info]
            if not found:
                note("Aave", name, source, insolvent)
            for p in found:
                aaves.append(p)
                note("Aave", p.name, source, insolvent, p.pool, len(p.reserves))
        addresses = [m for p in compounds for m in p.markets] + [p.pool for p in aaves]
        if not addresses:
            result["status"] = "仕組みが読めない"
            return result
        self.prices.current([NATIVE, *(a for p in aaves for a in p.reserves)])      # 値段をまとめて読んでおく

        head = self.chain.safe_head(self.purpose)
        block_seconds = self.chain.block_time(2.0, sample=min(10_000, max(1, head // 2)), purpose=self.purpose)
        result["block_seconds"] = round(block_seconds, 3)
        self.out(f"  仕組み {len(compounds) + len(aaves)}か所(住所 {len(addresses)}か所)の記録を、"
                 f"直近{self.days:g}日ぶん読む(1ブロック約{block_seconds:.2f}秒)")
        logs, days_read, why = self.sweep(addresses, head, block_seconds)
        result["days_read"], result["note"] = round(days_read, 2), why
        liqs, borrowers = self.parse(logs, compounds, aaves)
        self.value(liqs)
        result["liquidations"] = liqs

        capped = [info[k]["name"] for k, v in borrowers.items() if k in info and len(v) > self.max_borrowers]
        if capped:      # 借り手の多い仕組みは、今の状態を読む人数を絞る(読む回数が増えすぎるので)
            result["note"] = "; ".join(x for x in (
                result["note"], f"借り手が多く、{self.max_borrowers:,}人だけ今の状態を読んだ: {'、'.join(capped)}") if x)
        borrowers = {k: set(sorted(v)[:self.max_borrowers]) for k, v in borrowers.items()}
        for p in compounds:
            self.finder.add_borrowers(p, sorted(borrowers.get(p.controller, ())))
        if compounds:
            self.finder.check(compounds)
            names = {p.controller: p.name for p in compounds}
            for r in stranded.summarize(self.conn, self.chain.name):
                if r["controller"] in names:
                    result["stranded"].append({**{k: r[k] for k in (
                        "borrower", "debt_usd", "collateral_usd", "repay_symbol", "collateral_symbol", "repay_usd",
                        "profit_usd", "allowed", "cash_ok", "market_ratio", "repay_ratio")},
                        "place": r["controller"], "kind": "Compound"})
        for p in aaves:
            result["stranded"] += self.aave_stranded(p, sorted(borrowers.get(p.pool, ())))
        for r in result["stranded"]:
            r["market_profit_usd"] = market_profit(r)
            r["takeable"] = takeable(r)

        for key, row in info.items():
            mine = [lq for lq in liqs if lq["place"] == key]
            who = Counter(lq["liquidator"] for lq in mine)
            priced = [lq for lq in mine if lq["repay_usd"] is not None and lq["seize_usd"] is not None]
            open_rows = [r for r in result["stranded"] if r["place"] == key]
            row.update({
                "liquidations": len(mine), "liquidators": len(who),
                "top_share": max(who.values()) / len(mine) if mine else None,
                "repay_usd": sum(lq["repay_usd"] for lq in mine if lq["repay_usd"] is not None),
                "profit_usd": sum(lq["seize_usd"] - lq["repay_usd"] for lq in priced),
                "unpriced": len(mine) - len(priced), "borrowers": len(borrowers.get(key, ())),
                "open": len(open_rows),
                "open_profit_usd": sum(r["profit_usd"] or 0.0 for r in open_rows),
                "takeable": sum(1 for r in open_rows if r["takeable"]),
                "takeable_usd": sum(r["market_profit_usd"] for r in open_rows if r["takeable"])})
        result["seconds"] = round(self.wall() - started, 1)
        return result


# --- まとめ ---
def survey_chain(target: dict, *, out_dir: Path, cfg: dict, allow=None, days: float | None = None,
                 minutes: float | None = None, client: httpx.Client | None = None, out: Callable[[str], None] = print) -> dict:
    """1チェーンを調べ、結果を out_dir/<チェーン>.json に残す。接続先が無い・読めない時も、理由を残す。"""
    name = target["chain"]
    own = client is None
    client = client or httpx.Client(timeout=15.0)
    result = {"chain": name, "chain_id": target["id"], "places": [], "liquidations": [], "stranded": [], "note": ""}
    conn = None
    try:
        urls = [u for u in target.get("rpcs", []) if usable(u, cfg["exclude_hosts"], allow)]
        endpoints, bad = pick_endpoints(client, urls, target["id"], most=cfg["max_endpoints"],
                                        probe_most=cfg["probe_endpoints"])
        result["endpoints"] = [urlparse(u).hostname for u in endpoints]
        if not endpoints:
            result["status"] = "接続先が無い"
            result["note"] = " / ".join(bad[:4]) or "鍵の要らない接続先が材料に無い"
            return result
        out_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(out_dir / f"{name}.db", timeout=30)
        conn.row_factory = sqlite3.Row
        relay = Relay(name, endpoints, client=client, waits={"research": 45.0})
        chain = Chain(relay, cache_depth=20, max_log_blocks=10_000, min_log_blocks=100)
        prices = Prices(client, name, base=cfg["prices_url"], min_confidence=cfg["min_confidence"],
                        max_age=cfg["max_price_age_hours"] * 3600)
        s = Surveyor(conn, chain, prices, days=days or cfg["days"], minutes=minutes or cfg["minutes_per_chain"],
                     out=out,
                     max_borrowers=cfg["max_borrowers"])
        try:
            s.run(target)
        finally:
            result.update(s.result)
            result["price_calls"], result["price_failures"] = prices.calls, prices.failed
    except RpcError as e:
        result["status"], result["note"] = "読めない", str(e)[:300]
    except Exception as e:        # noqa: BLE001  1つのチェーンの思わぬ失敗で、ほかのチェーンの調べを止めない(理由は結果に残す)
        result["status"], result["note"] = "エラー", f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        if conn is not None:
            conn.close()
        if own:
            client.close()
        result["finished_at"] = int(time.time())
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def load_results(out_dir: Path) -> list[dict]:
    out = []
    for p in sorted(Path(out_dir).glob("*.json")):
        try:
            got = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(got, dict) and "chain" in got and "places" in got:
            out.append(got)
    return out


def _money(v: float | None) -> str:
    return "-" if v is None else f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def describe(results: list[dict], *, kill_usd: float = 100.0, top: int = 40) -> list[str]:
    """表1枚: 仕組みごとの清算(上限)と取り残し。最後に、打ち切りの線と比べる数字。"""
    read = [r for r in results if r.get("status") == "ok"]
    lines = [f"# 裾野のチェーンの調べ(チェーン {len(results)}、うち読めた {len(read)})", ""]
    rows = []
    for r in read:
        days = r.get("days_read") or 0
        for p in r["places"]:
            if p["status"] != "ok" or "profit_usd" not in p:
                continue
            per30 = p["profit_usd"] * 30 / days if days else None
            rows.append((r["chain"], p, days, per30))
    rows.sort(key=lambda x: -((x[3] or 0) + x[1]["takeable_usd"]))
    head = ("| チェーン | 仕組み | 種類 | 読めた日数 | 清算 | 清算した主体 | 1位の割合 | 返した額 | 清算の利益 "
            "| 30日あたり | 今清算できる | うち取れる | 取れる額 |")
    lines += [head, "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for chain, p, days, per30 in rows[:top]:
        share = "-" if p["top_share"] is None else f"{p['top_share']:.0%}"
        lines.append(f"| {chain} | {_clean(p['name'], 28)}{'(返されない借金あり)' if p['insolvent'] else ''} "
                     f"| {p['kind']} | {days:.1f} | {p['liquidations']}件 | {p['liquidators']} | {share} "
                     f"| {_money(p['repay_usd'])} | {_money(p['profit_usd'])} | {_money(per30)} "
                     f"| {p['open']}人 | {p['takeable']}人 | {_money(p['takeable_usd'])} |")
    if len(rows) > top:
        lines.append(f"\nほか {len(rows) - top}か所(清算の利益 "
                     f"{_money(sum(x[1]['profit_usd'] for x in rows[top:]))}、取れる額 "
                     f"{_money(sum(x[1]['takeable_usd'] for x in rows[top:]))})")
    liq30 = sum(x[3] or 0.0 for x in rows)
    stuck = sum(x[1]["takeable_usd"] for x in rows)
    unpriced = sum(x[1]["unpriced"] for x in rows)
    lines += ["", "## 打ち切りの線と比べる", "",
              f"- 清算した主体の利益(30日あたり、全チェーンの合計。**遅くても取れたお金の上限**): {_money(liq30)}"
              + (f"(値段の分からない清算 {unpriced}件は入っていない)" if unpriced else ""),
              f"- 今も取り残されていて、市場の値段で見ても取れる額: {_money(stuck)}",
              f"- 打ち切りの線: 30日あたり {_money(kill_usd)}。"
              + ("上限と取り残しを足しても届かない。" if liq30 + stuck < kill_usd else
                 "上限は線を超えている。お金のある仕組みで、清算できるようになってから取られるまでの速さを読んで確かめる。")]
    skipped = [r for r in results if r.get("status") != "ok"]
    if skipped:
        why = Counter(r.get("status") or "?" for r in skipped)
        lines += ["", "## 読めなかったチェーン", "",
                  "、".join(f"{k} {n}" for k, n in why.most_common()) + "。",
                  "", "| チェーン | 理由 | くわしく |", "|---|---|---|"]
        lines += [f"| {r['chain']} | {r.get('status')} | {_clean(r.get('note') or '', 120)} |" for r in skipped]
    partial = [r for r in read if r.get("note")]
    if partial:
        lines += ["", "## 途中までしか読めなかったチェーン", ""]
        lines += [f"- {r['chain']}: {r.get('days_read', 0):.1f}日ぶん。{_clean(r['note'], 160)}" for r in partial]
    return lines


def run(targets_path: str | Path, out_dir: str | Path, *, days: float | None = None, chains: list[str] | None = None,
        workers: int | None = None, minutes: float | None = None, redo: bool = False, allow_path: str | Path | None = None,
        out: Callable[[str], None] = print) -> int:
    """python -m factory tail-survey: 材料のチェーンを順に調べ、表(summary.md)を作る。
    すでに結果のあるチェーンは飛ばす(redo で読み直す)。"""
    cfg = settings()
    out_dir = Path(out_dir)
    targets = json.loads(Path(targets_path).read_text(encoding="utf-8"))
    allow = load_domains(allow_path)
    todo = [t for t in targets if (not chains or t["chain"] in chains)
            and (redo or not (out_dir / f"{t['chain']}.json").exists())]
    out(f"裾野の調べ: {len(todo)}チェーン(材料 {len(targets)}チェーン。結果は {out_dir})")

    def one(target: dict) -> dict:
        lines: list[str] = []
        r = survey_chain(target, out_dir=out_dir, cfg=cfg, allow=allow, days=days, minutes=minutes,
                         out=lines.append)
        ok = [p for p in r["places"] if p["status"] == "ok"]
        out(f"{r['chain']}: {r.get('status')}"
            + (f"・仕組み {len(ok)}か所・清算 {len(r['liquidations'])}件・今清算できる {len(r['stranded'])}人"
               f"・{r.get('days_read', 0):.1f}日ぶん・{r.get('seconds', 0):.0f}秒" if r.get("status") == "ok"
               else f"({(r.get('note') or '')[:120]})"))
        return r

    with ThreadPoolExecutor(max_workers=workers or cfg["workers"]) as pool:
        list(pool.map(one, todo))
    lines = describe(load_results(out_dir))
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        out(line)
    return 0
