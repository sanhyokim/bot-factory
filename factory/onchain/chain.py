"""チェーンの読み取り(設計書 2026-11-04 の道具2): ブロック・取引・受領書・ログ・仕組みの状態。

すべて回線の中継(rpc.py)を通す。確定したとみなせるブロック(最新から cache_depth より古い)の読み取りは
キャッシュに残し、2回目からは回線を使わない。
"""
from __future__ import annotations

import time
from collections.abc import Callable

from .. import config
from .abi import to_int
from .rpc import RangeTooLarge, Relay, RpcError, TooManyAddresses

HEAD_TTL_SECONDS = 10      # 最新のブロック番号を覚えておく秒数(確定の判定に使うだけなので少し古くてよい)


class Chain:
    def __init__(self, relay: Relay, *, cache_depth: int = 2400, max_log_blocks: int = 10_000,
                 min_log_blocks: int = 100, clock: Callable[[], float] = time.monotonic):
        self.relay = relay
        self.name = relay.chain
        self.cache_depth = cache_depth
        self.max_log_blocks = max_log_blocks
        self.min_log_blocks = min(min_log_blocks, max_log_blocks)
        self._clock = clock
        self._head: int | None = None
        self._head_at = 0.0
        self.log_step = max_log_blocks      # 今の「1回で読むブロック数」。断られたら半分にして、この後も使う
        self.addr_step: int | None = None   # 1回のログで決める住所の数の上限。住所の数で断られて分かったら、この後も使う
        self._block_time: float | None = None

    @classmethod
    def from_config(cls, name: str, **relay_kw) -> Chain:
        c = config.load_config()["onchain"]["chains"][name]
        return cls(Relay.from_config(name, **relay_kw), cache_depth=c.get("cache_depth", 2400),
                   max_log_blocks=c.get("max_log_blocks", 10_000), min_log_blocks=c.get("min_log_blocks", 100))

    # --- ブロックの番号 ---
    def head(self, purpose: str = "research") -> int:
        n = to_int(self.relay.call("eth_blockNumber", [], purpose=purpose))
        self._head, self._head_at = n, self._clock()
        return n

    def block_time(self, fallback: float, *, sample: int = 10_000, purpose: str = "research") -> float:
        """実際のブロックの間隔(秒)。最新と sample ブロック前の時刻から測る(チェーンごとに違い、変わることもある)。
        読めなければ fallback(設定の目安)。"""
        if self._block_time is None:
            try:
                head = self.head(purpose)
                a = self.block(head, purpose=purpose) or {}
                b = self.block(max(0, head - sample), purpose=purpose) or {}
                dt = to_int(a.get("timestamp", "0x0")) - to_int(b.get("timestamp", "0x0"))
                self._block_time = dt / min(sample, head) if dt > 0 and head else fallback
            except RpcError:
                self._block_time = fallback
        return self._block_time

    def safe_head(self, purpose: str = "research") -> int:
        """これ以下のブロックは確定したとみなす(キャッシュしてよい)。"""
        if self._head is None or self._clock() - self._head_at > HEAD_TTL_SECONDS:
            self.head(purpose)
        return self._head - self.cache_depth

    def _settled(self, block: int, purpose: str) -> bool:
        if self._head is not None and block <= self._head - self.cache_depth:
            return True       # 前に見た最新から数えても十分古い(読み直さない)
        return block <= self.safe_head(purpose)

    def chain_id(self, purpose: str = "research") -> int:
        return to_int(self.relay.call("eth_chainId", [], purpose=purpose, cache=True))

    # --- ブロック・取引・受領書 ---
    def block(self, number: int, *, txs: bool = False, purpose: str = "research") -> dict | None:
        """ブロック。txs=True なら取引の中身も入れる。"""
        return self.relay.call("eth_getBlockByNumber", [hex(number), txs], purpose=purpose,
                               cache=lambda r: r is not None and self._settled(number, purpose))

    def transaction(self, tx_hash: str, *, purpose: str = "research") -> dict | None:
        return self.relay.call("eth_getTransactionByHash", [tx_hash], purpose=purpose,
                               cache=lambda r: self._settled_result(r, purpose))

    def receipt(self, tx_hash: str, *, purpose: str = "research") -> dict | None:
        """取引の受領書(通ったか・使ったガス・出したログ)。"""
        return self.relay.call("eth_getTransactionReceipt", [tx_hash], purpose=purpose,
                               cache=lambda r: self._settled_result(r, purpose))

    def _settled_result(self, r, purpose: str) -> bool:
        return bool(r) and r.get("blockNumber") is not None and self._settled(to_int(r["blockNumber"]), purpose)

    # --- 仕組みの状態 ---
    def call(self, to: str, data: str, *, block: int | str = "latest", sender: str | None = None,
             purpose: str = "research") -> str:
        """取引を送らずに、あるブロックの状態で関数を呼ぶ(eth_call)。通らなければ CallReverted。"""
        tx = {"to": to, "data": data}
        if sender:
            tx["from"] = sender
        tag = hex(block) if isinstance(block, int) else block
        settled = isinstance(block, int) and self._settled(block, purpose)
        return self.relay.call("eth_call", [tx, tag], purpose=purpose, cache=settled)

    def code(self, address: str, *, block: int | str = "latest", purpose: str = "research") -> str:
        """仕組みのプログラム(無ければ "0x")。"""
        tag = hex(block) if isinstance(block, int) else block
        settled = isinstance(block, int) and self._settled(block, purpose)
        return self.relay.call("eth_getCode", [address, tag], purpose=purpose, cache=settled)

    # --- ログ ---
    def logs(self, *, from_block: int, to_block: int, address: str | list[str] | None = None,
             topics: list | None = None, purpose: str = "research", cache: bool = True) -> list[dict]:
        """ログを古い順に。範囲は log_step の倍数の区切りで読む(同じ区切りなら2回目はキャッシュから返る)。
        cache=False なら、確定したブロックでもキャッシュに残さない(交換の記録のように量が多いもの)。
        どの接続先にも広すぎると断られたら、受けてくれそうな広さ(分からなければ半分)にして読み直す。
        min_log_blocks より狭くしないと読めない時は、あきらめて RangeTooLarge(断られ方の取り違えで、
        何万回も呼ぶことにならないように)。
        住所を並べた時に、住所の数で断る接続先しか使えなければ、その数ずつに分けて読み、古い順に並べ直す。"""
        if from_block > to_block:
            return []
        out: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(to_block, (start // self.log_step + 1) * self.log_step - 1)
            flt: dict = {"fromBlock": hex(start), "toBlock": hex(end)}
            if topics:
                flt["topics"] = topics
            try:
                rows = self._read_logs(flt, address, purpose, cache and self._settled(end, purpose))
            except RangeTooLarge as e:
                step = e.max_span if e.max_span and e.max_span < self.log_step else self.log_step // 2
                if step < self.min_log_blocks:
                    raise RangeTooLarge(f"ログを{self.min_log_blocks:,}ブロックずつにしても読めない: {e}") from e
                self.log_step = step
                continue
            except TooManyAddresses as e:
                if not isinstance(address, list) or e.max_addresses < 1 or \
                        (self.addr_step is not None and e.max_addresses >= self.addr_step):
                    raise
                self.addr_step = e.max_addresses
                continue
            out.extend(rows)
            start = end + 1
        return out

    def _read_logs(self, flt: dict, address: str | list[str] | None, purpose: str, cache: bool) -> list[dict]:
        groups: list = [address]
        if isinstance(address, list) and self.addr_step and len(address) > self.addr_step:
            groups = [address[i:i + self.addr_step] for i in range(0, len(address), self.addr_step)]
        rows: list[dict] = []
        for group in groups:
            f = {**flt, "address": group} if group else flt
            rows.extend(self.relay.call("eth_getLogs", [f], purpose=purpose, cache=cache) or [])
        if len(groups) > 1:
            rows.sort(key=lambda lg: (to_int(lg.get("blockNumber") or "0x0"), to_int(lg.get("logIndex") or "0x0")))
        return rows
