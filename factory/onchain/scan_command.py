"""python -m factory actor-scan: 清算で稼いでいる主体を探して表にする(読み取りだけ)。"""
from __future__ import annotations

import sys
import time
from collections.abc import Callable

from .. import config, db, logs
from . import actors
from .cache import ChainCache
from .chain import Chain
from .rpc import RpcError

PROCESS = "actor-scan"


def run(chain_name: str = "arbitrum", days: float = 7, top: int = 15, *, chain: Chain | None = None,
        conn=None, out: Callable[[str], None] | None = None) -> int:
    own = conn is None
    conn = conn or db.connect()
    cache = ChainCache() if chain is None else None
    chain = chain or Chain.from_config(chain_name, cache=cache, usage_conn=conn)
    lines: list[str] = []

    def say(text: str) -> None:
        lines.append(text)
        (out or print)(text)

    try:
        block_seconds = config.load_config()["onchain"]["chains"][chain_name].get("block_seconds", 0.25)
        safe = chain.safe_head()
        start = max(0, safe - int(days * 86400 / block_seconds) + 1)
        ranges = actors.plan(conn, chain_name, start, safe)
        todo = sum(b - a + 1 for a, b in ranges)
        say(f"清算の記録を読む: ブロック {start:,}〜{safe:,}(まだ読んでいない {todo:,}ブロック)")
        scanner = actors.Scanner.from_config(conn, chain, out=say)
        added = sum(scanner.scan(a, b) for a, b in ranges)
        say(f"  新しく帳簿に足した清算 {added}件")
        summary = actors.summarize(conn, chain_name, int(time.time() - days * 86400))
        for line in actors.describe(summary, chain=chain_name, days=days, top=top,
                                    basis="block" if scanner.archive_ok else "latest"):
            say(line)
        return 0
    except RpcError as e:
        say(f"エラー: {e}(読み終えた所までは残っている。もう一度動かすと続きから読む)")
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    finally:
        chain.relay.flush()
        if lines:
            logs.write(PROCESS, "\n".join(lines), stage="主体の分析")
        if cache is not None:
            cache.close()
        if own:
            conn.close()
