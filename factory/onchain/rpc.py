"""チェーンへの回線の中継(設計書 2026-11-04 の道具1)。

読み込みを1か所で受け、無料の公開接続先に振り分ける。
- 接続先ごとに1分あたりの回数の枠を持ち、使い道(本番 > paper > 調査)ごとに使ってよい割合を分ける。
  調査が自分の分を使い切っても、paper と本番の分は残る(作者は回線の詰まりで同時に動けるBOTが約70本で止まった)
- 断られた接続先(429)は60秒、使える量を使い切った接続先(1日・プランの上限)は30分休ませる
- つながらない・壊れた答え(5xx・時間切れ)は30秒休ませ、続けば60秒・120秒…(10分まで)と延ばす
- 「取引が通らない(revert)」「ログの範囲が広すぎる」は接続先の不調ではないので休ませず、呼んだ側に返す。
  広い範囲を受ける接続先がどれも使えず、狭い範囲なら今すぐ読める接続先がある時は、あきらめずに
  「範囲が広すぎる」として返し、呼んだ側(chain.py)に範囲を狭めさせる
- 同じ読み方(例: 古いブロックの状態)を3回続けて断った接続先は、ほかの接続先がその読み方に答えられるなら、
  その読み方の時だけ10分外す(無料の接続先は古いブロックを読ませないことが多く、毎回試すと回数の無駄)
- 待てる長さは使い道ごとに違う。本番は機会が消えるので短く、調査は急がないので長く待つ
- 変わらない答え(確定したブロックの読み取り)はキャッシュから返す。何をキャッシュするかは chain.py が決める
- 使った回数を接続先・使い道ごとに帳簿(rpc_usage)に残す

鍵は使わない。送信や署名の呼び方はここでは受けない(第0期はお金も鍵も使わない)。
"""
from __future__ import annotations

import itertools
import math
import re
import sqlite3
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from .. import config
from ..models import now_jst
from .cache import ChainCache

PURPOSES = ("live", "paper", "research")            # 本番 > paper > 調査
PURPOSE_NAMES = {"live": "本番", "paper": "paper", "research": "調査"}
DEFAULT_SHARES = {"live": 0.2, "paper": 0.3, "research": 0.5}
DEFAULT_WAITS = {"live": 2.0, "paper": 30.0, "research": 120.0}

# 鍵が要る呼び方。第0期は受けない
SEND_METHODS = frozenset({"eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "eth_signTransaction",
                          "eth_signTypedData", "eth_signTypedData_v4", "personal_sign"})

_RATE = re.compile(r"rate|too many requests|requests per|throttl|capacity|limit reached", re.IGNORECASE)
# 使える量を使い切った(1日・1か月・プランの上限)。すぐには戻らないので長く休ませる
_QUOTA = re.compile(r"usage limit|quota|daily|monthly|credits|current plan|upgrade your plan", re.IGNORECASE)
_REVERT = re.compile(r"execution reverted|\breverted\b", re.IGNORECASE)
# eth_getLogs で範囲が広すぎる・件数が多すぎると断られた時の言い方(接続先ごとに違う)。
# 「使いすぎ」の言い方(limit exceeded など)と取り違えないよう、範囲・件数を言うものだけにする
_RANGE = re.compile(r"range|too many (blocks|results|logs)|more than \d+|max(imum)? results|response size"
                    r"|limited to \d+", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d,]*")

CACHE_ENDPOINT = "cache"
MAX_ERROR_COOLDOWN = 600.0     # つながらないのが続いた時に休ませる長さの上限(秒)

# ブロックを指して読む呼び方と、ブロックの引数の位置(古いブロックを指す時だけ別の読み方として数える)
_BLOCK_ARG = {"eth_call": 1, "eth_getCode": 1, "eth_getBalance": 1, "eth_getTransactionCount": 1,
              "eth_getStorageAt": 2, "eth_getBlockByNumber": 0}


class RpcError(RuntimeError):
    """回線から答えが取れなかった。"""


class AllBusy(RpcError):
    """どの接続先も休み中か、この使い道の枠を使い切っていて、待てる長さを過ぎた。"""


class CallReverted(RpcError):
    """取引(eth_call など)が通らなかった。接続先の不調ではない。"""

    def __init__(self, message: str, data=None):
        super().__init__(message)
        self.data = data


class RangeTooLarge(RpcError):
    """ログを読む範囲が、どの接続先にも広すぎた(呼んだ側が範囲を狭めて読み直す)。
    max_span は、いずれかの接続先が受けそうな一番広いブロック数(分からなければ None)。"""

    def __init__(self, message: str, max_span: int | None = None):
        super().__init__(message)
        self.max_span = max_span


class _TryNext(Exception):
    """この接続先はあきらめて次へ。hard なら、この呼び出しではもうこの接続先を使わない。"""

    def __init__(self, reason: str, hard: bool = False):
        super().__init__(reason)
        self.hard = hard


@dataclass
class Endpoint:
    url: str
    cooldown_until: float = 0.0
    recent: deque = field(default_factory=deque)     # (時刻, 使い道)。直近1分の分だけ持つ
    last_error: str = ""
    log_limit: int | None = None     # ログを1回で読めるブロック数(断られて分かった上限。None は分かっていない)
    log_note: str = ""               # 範囲で断られた時の言い方(画面・確かめに出す)
    error_streak: int = 0            # つながらない・壊れた答えが続いた回数(休ませる長さを倍にしていく)
    quota_note: str = ""             # 使える量を使い切ったと言われた時の言い方
    strikes: Counter = field(default_factory=Counter)        # 読み方 → 続けてはっきり断った回数
    served: set = field(default_factory=set)                 # 答えを返せた読み方
    blocked: dict = field(default_factory=dict)              # 読み方 → この時刻まで、その読み方では使わない
    block_notes: dict = field(default_factory=dict)          # 読み方 → 断られた言い方

    def fits(self, span: int | None) -> bool:
        return span is None or self.log_limit is None or span <= self.log_limit


class Relay:
    def __init__(self, chain: str, endpoints: list[str], *, client: httpx.Client | None = None,
                 cache: ChainCache | None = None, usage_conn: sqlite3.Connection | None = None,
                 per_minute: int = 300, shares: dict | None = None, waits: dict | None = None,
                 cooldown_rate: float = 60.0, cooldown_error: float = 30.0, cooldown_quota: float = 1800.0,
                 skip_after: int = 3, skip_seconds: float = 600.0, timeout: float = 15.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if not endpoints:
            raise ValueError(f"{chain}: 接続先がない")
        self.chain = chain
        self.endpoints = [Endpoint(u) for u in endpoints]
        self.client = client or httpx.Client(timeout=timeout)
        self.cache, self.usage_conn = cache, usage_conn
        self.per_minute = per_minute
        self.shares = {**DEFAULT_SHARES, **(shares or {})}
        self.waits = {**DEFAULT_WAITS, **(waits or {})}
        self.cooldown_rate, self.cooldown_error, self.cooldown_quota = cooldown_rate, cooldown_error, cooldown_quota
        self.skip_after, self.skip_seconds = skip_after, skip_seconds
        self._clock, self._sleep = clock, sleep
        self._ids = itertools.count(1)
        self._pending: dict[tuple[str, str], Counter] = {}   # 帳簿にまだ書いていない回数
        self.totals: dict[tuple[str, str], Counter] = {}     # このプロセスで使った回数(表示用)

    @classmethod
    def from_config(cls, chain: str, **kw) -> Relay:
        cfg = config.load_config()["onchain"]
        rpc = cfg.get("rpc", {})
        return cls(chain, cfg["chains"][chain]["endpoints"], per_minute=rpc.get("per_minute", 300),
                   shares=rpc.get("shares"), waits=rpc.get("wait_seconds"),
                   cooldown_rate=rpc.get("cooldown_rate_seconds", 60),
                   cooldown_error=rpc.get("cooldown_error_seconds", 30),
                   cooldown_quota=rpc.get("cooldown_quota_seconds", 1800),
                   skip_after=rpc.get("skip_after_failures", 3), skip_seconds=rpc.get("skip_seconds", 600),
                   timeout=rpc.get("timeout_seconds", 15), **kw)

    # --- 呼び出し ---
    def call(self, method: str, params: list | tuple = (), *, purpose: str = "research",
             cache: bool | Callable[[object], bool] = False):
        """1回呼んで答え(result)を返す。

        cache: True ならキャッシュを引き、無ければ答えを残す。関数なら、答えを見て残すかを決める
        (例: 受領書は確定したブロックの分だけ残す)。False ならキャッシュを使わない。
        """
        if method in SEND_METHODS:
            raise ValueError(f"{method} は使わない(第0期は送信も署名もしない)")
        if purpose not in PURPOSES:
            raise ValueError(f"使い道が不正: {purpose}")
        params = list(params)
        key = None
        if cache and self.cache is not None:
            key = ChainCache.key(self.chain, method, params)
            found, value = self.cache.get(key)
            if found:
                self._count(CACHE_ENDPOINT, purpose, "cache_hits")
                return value
        result = self._call_network(method, params, purpose)
        if key is not None and (cache is True or cache(result)):
            self.cache.put(key, self.chain, method, result)
        return result

    def _call_network(self, method: str, params: list, purpose: str):
        deadline = self._clock() + self.waits[purpose]
        tried: set[str] = set()
        skip: set[str] = set()       # はっきり断られた(古い状態が無い など)接続先。休ませないが、この呼び出しでは使わない
        span = _log_span(method, params)
        kind = _kind(method, params)
        attempts, max_attempts = 0, 2 * len(self.endpoints)
        while attempts < max_attempts:
            ep, wait = self._pick(purpose, tried, skip, span, kind)
            if ep is None:
                if wait == math.inf or self._clock() + wait > deadline:
                    self._raise_if_narrower(span, skip, kind, deadline)
                if wait == math.inf:
                    break
                if self._clock() + wait > deadline:
                    raise AllBusy(f"{self.chain}: どの接続先も使えない({PURPOSE_NAMES[purpose]}の待ち時間を過ぎた)。"
                                  f"直近のエラー: {self._last_errors()}")
                self._sleep(wait)
                continue
            attempts += 1
            tried.add(ep.url)
            try:
                return self._send(ep, method, params, purpose, kind)
            except _TryNext as e:
                if e.hard:
                    skip.add(ep.url)
                continue
        self._raise_if_narrower(span, skip, kind, deadline)
        if span is not None and not any(ep.fits(span) for ep in self.endpoints):
            limits = [ep.log_limit for ep in self.endpoints if ep.log_limit]
            notes = " / ".join(f"{_short(ep.url)}: {ep.log_note}" for ep in self.endpoints if ep.log_note)
            raise RangeTooLarge(f"{self.chain}: {span:,}ブロックのログはどの接続先も受けない({notes})",
                                max(limits) if limits else None)
        raise RpcError(f"{self.chain}: {method} が{attempts}回とも失敗した。直近のエラー: {self._last_errors()}")

    def _pick(self, purpose: str, tried: set[str], skip: set[str], span: int | None = None,
              kind: str | None = None) -> tuple[Endpoint | None, float]:
        """使える接続先のうち、まだ試していない・直近1分に使った回数が少ないものを選ぶ。
        使えるものが無ければ、一番早く使えるようになるまでの秒数を返す(もう使える接続先が無ければ inf)。
        この読み方を続けて断っている接続先(blocked)は、外している間は数えない。"""
        now = self._clock()
        cap = max(1, int(self.per_minute * self.shares[purpose]))
        ready, waits = [], [math.inf]
        for i, ep in enumerate(self.endpoints):
            while ep.recent and now - ep.recent[0][0] >= 60:
                ep.recent.popleft()
            if ep.url in skip or not ep.fits(span):     # ログの範囲が、この接続先の上限より広い
                continue
            if kind is not None and ep.blocked.get(kind, 0) > now:
                continue
            mine = [t for t, p in ep.recent if p == purpose]
            if ep.cooldown_until > now:
                waits.append(ep.cooldown_until - now)
            elif len(mine) >= cap:
                waits.append(60 - (now - mine[0]) + 0.01)
            else:
                ready.append((ep.url in tried, len(ep.recent), i, ep))
        if ready:
            return min(ready, key=lambda r: r[:3])[3], 0.0
        return None, min(waits)

    def _raise_if_narrower(self, span: int | None, skip: set[str], kind: str, deadline: float) -> None:
        """この範囲を受ける接続先がどれも使えない(はっきり断った・外した・待てる長さを過ぎて休む)が、範囲を
        狭めれば待てる長さのうちに使える接続先がある時は、RangeTooLarge で返す(呼んだ側が、その接続先の
        上限まで狭めて読み直す)。狭められる接続先が複数あれば、一番広く読めるものに合わせる。
        例: 住所を決めないログを断る接続先と止まった接続先しか広い範囲を受けず、狭い範囲なら読める接続先がある。"""
        if span is None:
            return
        now = self._clock()
        usable = [ep for ep in self.endpoints
                  if ep.log_limit and ep.log_limit < span and ep.url not in skip
                  and ep.blocked.get(kind, 0) <= now and ep.cooldown_until <= deadline]
        if usable:
            best = max(usable, key=lambda ep: ep.log_limit)
            raise RangeTooLarge(f"{self.chain}: {span:,}ブロックのログを読める接続先が今は無い。"
                                f"{_short(best.url)} なら{best.log_limit:,}ブロックまで読める"
                                f"(直近のエラー: {self._last_errors()})", best.log_limit)

    def _send(self, ep: Endpoint, method: str, params: list, purpose: str, kind: str | None = None):
        kind = kind or _kind(method, params)
        start = self._clock()
        ep.recent.append((start, purpose))
        self._count(ep.url, purpose, "calls")
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        try:
            r = self.client.post(ep.url, json=body)
        except httpx.TimeoutException:
            self._broken(ep, purpose, "時間切れ")
        except httpx.HTTPError as e:
            self._broken(ep, purpose, f"つながらない({type(e).__name__})")
        finally:
            self._count(ep.url, purpose, "total_ms", int((self._clock() - start) * 1000))
        if r.status_code == 429:
            if _QUOTA.search(r.text):
                self._used_up(ep, purpose, r.text)
            self._fail(ep, purpose, self.cooldown_rate, "断られた(HTTP 429)", rate=True)
        try:
            data = r.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or ("error" not in data and r.status_code >= 400):
            self._broken(ep, purpose, f"HTTP {r.status_code}")
        ep.error_streak = 0                  # 答えは返ってきた(つながる)
        err = data.get("error")
        if err:
            msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            if code == 3 or _REVERT.search(msg):
                self._served(ep, kind)       # 取引を試せた(接続先は答えられる)
                raise CallReverted(msg, err.get("data") if isinstance(err, dict) else None)
            span = _log_span(method, params)
            if span is not None and _RANGE.search(msg):
                # この接続先の上限を覚え、この呼び出しは別の接続先に回す(休ませない)
                # (上限より広い範囲には fits() で使わなくなるので、hard にはしない。狭めた時に使う)
                ep.log_limit = min(ep.log_limit or span, _limit_from(msg, span))
                ep.log_note = msg[:160]
                self._fail(ep, purpose, 0, f"ログの範囲で断られた({span:,}ブロック: {msg[:140]})")
            if _QUOTA.search(msg):
                self._used_up(ep, purpose, msg)
            if code == 429 or _RATE.search(msg):
                self._fail(ep, purpose, self.cooldown_rate, f"断られた({msg[:80]})", rate=True)
            if code == -32005:      # 多くの接続先で「使いすぎ」
                self._fail(ep, purpose, self.cooldown_rate, f"断られた({msg[:80]})", rate=True)
            # その他(古いブロックの状態が無い、呼び方を知らない など)は、休ませずに次の接続先へ。
            # 同じ読み方で続けば、その読み方の時だけしばらく外す
            self._strike(ep, kind, msg)
            self._fail(ep, purpose, 0, msg[:120], hard=True)
        if "result" not in data:
            self._broken(ep, purpose, "答えに result が無い")
        self._served(ep, kind)
        return data["result"]

    def _broken(self, ep: Endpoint, purpose: str, reason: str):
        """つながらない・壊れた答え。続くほど長く休ませる(30秒・60秒・120秒…上限10分)。"""
        cooldown = min(self.cooldown_error * 2 ** ep.error_streak, max(self.cooldown_error, MAX_ERROR_COOLDOWN))
        ep.error_streak += 1
        self._fail(ep, purpose, cooldown, reason)

    def _used_up(self, ep: Endpoint, purpose: str, msg: str):
        """使える量を使い切った(1日・プランの上限)。すぐには戻らないので長く休ませる。"""
        ep.quota_note = msg[:120]
        self._fail(ep, purpose, self.cooldown_quota, f"使える量を使い切った({msg[:80]})", rate=True)

    def _served(self, ep: Endpoint, kind: str):
        ep.served.add(kind)
        ep.strikes.pop(kind, None)

    def _strike(self, ep: Endpoint, kind: str, msg: str):
        """この読み方を続けて断った回数を数え、skip_after 回続いたら skip_seconds 秒外す。
        ほかに、この読み方に答えられて外れていない接続先がある時だけ外す(全部断るなら、頼み方の方が悪い)。"""
        ep.strikes[kind] += 1
        if ep.strikes[kind] < self.skip_after:
            return
        now = self._clock()
        if any(o is not ep and kind in o.served and o.blocked.get(kind, 0) <= now for o in self.endpoints):
            ep.blocked[kind] = now + self.skip_seconds
            ep.block_notes[kind] = msg[:120]

    def _fail(self, ep: Endpoint, purpose: str, cooldown: float, reason: str, *, rate: bool = False,
              hard: bool = False):
        ep.last_error = reason
        if cooldown:
            ep.cooldown_until = self._clock() + cooldown
        self._count(ep.url, purpose, "rate_limited" if rate else "errors")
        raise _TryNext(reason, hard)

    def _last_errors(self) -> str:
        errs = [f"{_short(ep.url)}: {ep.last_error}" for ep in self.endpoints if ep.last_error]
        return " / ".join(errs) or "なし"

    # --- 帳簿 ---
    def _count(self, endpoint: str, purpose: str, name: str, n: int = 1) -> None:
        for book in (self._pending, self.totals):
            book.setdefault((endpoint, purpose), Counter())[name] += n
        if self.usage_conn is not None and name == "calls" and \
                sum(c["calls"] for c in self._pending.values()) >= 200:
            self.flush()

    def flush(self) -> None:
        """使った回数を帳簿(rpc_usage)に足す。日本時間の日ごと・接続先ごと・使い道ごと。"""
        if self.usage_conn is None or not self._pending:
            return
        day = now_jst().date().isoformat()
        for (endpoint, purpose), c in self._pending.items():
            self.usage_conn.execute(
                """INSERT INTO rpc_usage(day, chain, endpoint, purpose, calls, errors, rate_limited,
                                         cache_hits, total_ms) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(day, chain, endpoint, purpose) DO UPDATE SET
                     calls = calls + excluded.calls, errors = errors + excluded.errors,
                     rate_limited = rate_limited + excluded.rate_limited,
                     cache_hits = cache_hits + excluded.cache_hits, total_ms = total_ms + excluded.total_ms""",
                (day, self.chain, endpoint, purpose, c["calls"], c["errors"], c["rate_limited"],
                 c["cache_hits"], c["total_ms"]))
        self.usage_conn.commit()
        self._pending.clear()

    def close(self) -> None:
        self.flush()
        self.client.close()


def _log_span(method: str, params: list) -> int | None:
    """eth_getLogs の読むブロック数(数字で範囲が決まっている時だけ)。"""
    if method != "eth_getLogs" or not params or not isinstance(params[0], dict):
        return None
    lo, hi = params[0].get("fromBlock"), params[0].get("toBlock")
    if not (isinstance(lo, str) and isinstance(hi, str) and lo.startswith("0x") and hi.startswith("0x")):
        return None
    return int(hi, 16) - int(lo, 16) + 1


def _kind(method: str, params: list) -> str:
    """読み方の種類。ブロックの番号を指す読み方は「@old」を付けて分ける(古いブロックを読ませない接続先が多い)。
    "latest" で読む呼び方は、古いブロックで断られ続けても外さない。
    住所を決めないログ(清算の地図・裁定)は「@any」を付けて分ける(住所を決めたログだけ読ませる接続先がある)。"""
    if method == "eth_getLogs":
        old = _log_span(method, params) is not None
        flt = params[0] if params and isinstance(params[0], dict) else {}
        if not flt.get("address"):
            method = f"{method}@any"
    else:
        i = _BLOCK_ARG.get(method)
        tag = params[i] if i is not None and len(params) > i else None
        old = isinstance(tag, str) and tag.startswith("0x")
    return f"{method}@old" if old else method


def kind_name(kind: str) -> str:
    """画面に出す読み方の名前。"""
    return kind.replace("@any", "(住所を決めずに)").replace("@old", "(古いブロック)")


def _limit_from(msg: str, span: int) -> int:
    """断られた言い方に上限の数が書いてあれば、それを上限にする(例: 「max is 2,000」)。無ければ半分。"""
    nums = [int(n.replace(",", "")) for n in _NUMBER.findall(msg)]
    nums = [n for n in nums if 10 <= n < span]
    return max(nums) if nums else max(1, span // 2)


def _short(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def probe(client: httpx.Client, url: str) -> dict:
    """接続先を1つだけ直接確かめる(中継を通さない)。chain_id・最新のブロック・かかった時間。"""
    out = {"url": url, "ok": False, "chain_id": None, "block": None, "ms": None, "error": ""}
    start = time.perf_counter()
    try:
        r = client.post(url, json=[{"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
                                   {"jsonrpc": "2.0", "id": 2, "method": "eth_blockNumber", "params": []}])
        out["ms"] = int((time.perf_counter() - start) * 1000)
        if r.status_code >= 400:
            out["error"] = f"HTTP {r.status_code}"
            return out
        data = r.json()
        rows = {d.get("id"): d for d in data} if isinstance(data, list) else {}
        if not rows:     # まとめて送る呼び方を受けない接続先は1つずつ
            rows = {i: client.post(url, json={"jsonrpc": "2.0", "id": i, "method": m, "params": []}).json()
                    for i, m in ((1, "eth_chainId"), (2, "eth_blockNumber"))}
            out["ms"] = int((time.perf_counter() - start) * 1000)
        for d in rows.values():
            if "error" in d:
                out["error"] = str(d["error"].get("message", d["error"]) if isinstance(d["error"], dict)
                                   else d["error"])[:120]
                return out
        out["chain_id"] = int(rows[1]["result"], 16)
        out["block"] = int(rows[2]["result"], 16)
        out["ok"] = True
    except httpx.TimeoutException:
        out["error"] = "時間切れ"
    except httpx.HTTPError as e:
        out["error"] = f"つながらない({type(e).__name__})"
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        out["error"] = f"答えが読めない({type(e).__name__})"
    return out


def probe_logs(client: httpx.Client, url: str, address: str, head: int,
               spans: tuple[int, ...] = (10_000, 5_000, 2_000, 1_000, 500, 100)) -> dict:
    """接続先を1つだけ直接呼び、ログを1回で何ブロックまで読めるかを広い順に試す(中継を通さない)。
    max_ok は読めた一番広いブロック数(どれも読めなければ None)、error は最初に断られた時の言い方。"""
    out = {"max_ok": None, "count": None, "error": ""}
    end = head - 1_000
    for span in spans:
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                "params": [{"fromBlock": hex(end - span + 1), "toBlock": hex(end), "address": address}]}
        try:
            r = client.post(url, json=body)
            data = r.json()
        except httpx.TimeoutException:
            msg = "時間切れ"
        except httpx.HTTPError as e:
            msg = f"つながらない({type(e).__name__})"
        except ValueError:
            msg = f"HTTP {r.status_code}(答えが読めない)"
        else:
            if isinstance(data, dict) and isinstance(data.get("result"), list):
                out["max_ok"], out["count"] = span, len(data["result"])
                return out
            err = data.get("error") if isinstance(data, dict) else None
            msg = (str(err.get("message", err)) if isinstance(err, dict) else str(err or f"HTTP {r.status_code}"))
        if not out["error"]:
            out["error"] = f"{span:,}ブロック: {msg[:120]}"
    return out
