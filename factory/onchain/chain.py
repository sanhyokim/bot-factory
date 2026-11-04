"""チェーンの読み取り(設計書 2026-11-04 の道具2): ブロック・取引・受領書・ログ・仕組みの状態。

すべて回線の中継(rpc.py)を通す。確定したとみなせるブロック(最新から cache_depth より古い)の読み取りは
キャッシュに残し、2回目からは回線を使わない。
"""
from __future__ import annotations

import time
from collections.abc import Callable

from .. import config
from .abi import to_int
from .rpc import RangeTooLarge, Relay

HEAD_TTL_SECONDS = 10      # 最新のブロック番号を覚えておく秒数(確定の判定に使うだけなので少し古くてよい)


class Chain:
    def __init__(self, relay: Relay, *, cache_depth: int = 2400, max_log_blocks: int = 10_000,
                 clock: Callable[[], float] = time.monotonic):
        self.relay = relay
        self.name = relay.chain
        self.cache_depth = cache_depth
        self.max_log_blocks = max_log_blocks
        self._clock = clock
        self._head: int | None = None
        self._head_at = 0.0
        self.log_step = max_log_blocks      # 今の「1回で読むブロック数」。断られたら半分にして、この後も使う

    @classmethod
    def from_config(cls, name: str, **relay_kw) -> Chain:
        c = config.load_config()["onchain"]["chains"][name]
        return cls(Relay.from_config(name, **relay_kw), cache_depth=c.get("cache_depth", 2400),
                   max_log_blocks=c.get("max_log_blocks", 10_000))

    # --- ブロックの番号 ---
    def head(self, purpose: str = "research") -> int:
        n = to_int(self.relay.call("eth_blockNumber", [], purpose=purpose))
        self._head, self._head_at = n, self._clock()
        return n

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
             topics: list | None = None, purpose: str = "research") -> list[dict]:
        """ログを古い順に。範囲は log_step ずつに分けて読み、広すぎると断られたら半分にして読み直す。"""
        if from_block > to_block:
            return []
        out: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(to_block, start + self.log_step - 1)
            flt: dict = {"fromBlock": hex(start), "toBlock": hex(end)}
            if address:
                flt["address"] = address
            if topics:
                flt["topics"] = topics
            try:
                rows = self.relay.call("eth_getLogs", [flt], purpose=purpose,
                                       cache=self._settled(end, purpose))
            except RangeTooLarge:
                if self.log_step == 1:
                    raise
                self.log_step = max(1, self.log_step // 2)
                continue
            out.extend(rows or [])
            start = end + 1
        return out
