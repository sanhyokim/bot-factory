"""python -m factory actor-scan: 清算で稼いでいる主体を探して表にする(読み取りだけ)。"""
from __future__ import annotations

import sys
import time
from collections.abc import Callable

from .. import config, db, logs
from ..models import now_jst
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
        since = int(time.time() - days * 86400)
        actors.backfill_timeboost(conn, chain, since)
        summary = actors.summarize(conn, chain_name, since)
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


def run_watch(chain_name: str = "arbitrum", *, every: float = 0, rounds: int | None = None,
              chain: Chain | None = None, conn=None, out: Callable[[str], None] | None = None,
              sleep: Callable[[float], None] = time.sleep) -> int:
    """python -m factory aave-watch: 借り手を集め → 健康度を読み → 清算の記録を読み → 誰が取ったかを照らす。
    every > 0 なら、その秒数ごとにくり返す(rounds 回で止める。None なら Ctrl+C まで)。"""
    from . import watch

    own = conn is None
    conn = conn or db.connect()
    cache = ChainCache() if chain is None else None
    chain = chain or Chain.from_config(chain_name, cache=cache, usage_conn=conn)
    lines: list[str] = []

    def say(text: str) -> None:
        lines.append(text)
        (out or print)(text)

    cfg = config.load_config()["onchain"]
    block_seconds = cfg["chains"][chain_name].get("block_seconds", 0.25)
    days = cfg.get("watch", {}).get("borrower_days", 90)
    watcher = watch.Watcher.from_config(conn, chain)
    scanner = actors.Scanner.from_config(conn, chain, out=say)
    done = 0
    code = 0
    try:
        while True:
            try:
                head = chain.head()
                start = max(0, head - int(days * 86400 / block_seconds) + 1)
                ranges = actors.plan(conn, chain_name, start, head, key=watch.BORROW_KEY)
                todo = sum(b - a + 1 for a, b in ranges)
                if done == 0:
                    say(f"借りた記録を読む: ブロック {start:,}〜{head:,}(まだ読んでいない {todo:,}ブロック"
                        + ("。初回は時間がかかる)" if todo * block_seconds > 86400 else ")"))
                new = sum(watcher.discover(a, b) for a, b in ranges)
                st = watcher.sweep()
                liq_start = max(0, head - int(86400 / block_seconds) + 1)
                for a, b in actors.plan(conn, chain_name, liq_start, st["head"]):
                    scanner.scan(a, b)
                res = watcher.resolve(st["recovered"])
                say(f"[{now_jst().strftime('%H:%M:%S')}] ブロック {st['head']:,}: 借り手 {st['borrowers']}人(新しく{new}人)、"
                    f"健康度を読んだ {st['read']}人(借金あり {st['with_debt']}人)。"
                    f"1未満 {st['under']}人(新しい機会 {st['found']}件)、1〜1.02 {st['near']}人。"
                    f"取られた {res['captured']}件、消えた {res['vanished']}件")
                if done == 0 or (every and done % 30 == 0):
                    for line in watch.report(conn, chain_name):
                        say(line)
            except RpcError as e:
                say(f"エラー: {e}(読み終えた所までは残っている。次の回は続きから読む)")
                code = 1
            done += 1
            if not every or (rounds is not None and done >= rounds):
                return code
            chain.relay.flush()
            if lines:
                logs.write(PROCESS, "\n".join(lines), stage="機会の見張り")
                lines.clear()
            sleep(every)
    except KeyboardInterrupt:
        return code
    finally:
        chain.relay.flush()
        if lines:
            logs.write(PROCESS, "\n".join(lines), stage="機会の見張り")
        if cache is not None:
            cache.close()
        if own:
            conn.close()


def run_timing(chain_name: str = "arbitrum", days: float = 7, top: int = 10, *, chain: Chain | None = None,
               conn=None, out: Callable[[str], None] | None = None) -> int:
    """python -m factory liq-timing: 過去の清算ごとに、割れてから取られるまでの速さと理由を測る。"""
    from . import timing

    own = conn is None
    conn = conn or db.connect()
    cache = ChainCache() if chain is None else None
    chain = chain or Chain.from_config(chain_name, cache=cache, usage_conn=conn)
    lines: list[str] = []

    def say(text: str) -> None:
        lines.append(text)
        (out or print)(text)

    since = int(time.time() - days * 86400)
    try:
        timer = timing.Timer.from_config(conn, chain, out=say)
        st = timer.run(since)
        say(f"清算の速さを測る: まだ測っていない {st['todo']}件のうち {st['done']}件を測った"
            f"(読み取り {st['reads']:,}回{'、読めなかった ' + str(st['failed']) + '件' if st['failed'] else ''})")
        for line in timing.describe(timing.summarize(conn, chain_name, since), chain=chain_name, days=days, top=top):
            say(line)
        return 0
    except RpcError as e:
        say(f"エラー: {e}(測り終えた分は残っている。もう一度動かすと続きから測る。"
            "古いブロックの状態を読める接続先が要る)")
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


def run_venues(chain_name: str = "arbitrum", days: float = 7, top: int = 25, *, chain: Chain | None = None,
               conn=None, out: Callable[[str], None] | None = None) -> int:
    """python -m factory venue-scan: 清算の記録を出している仕組みを、住所を決めずにすべて探す(清算の地図)。"""
    from . import venues

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
        ranges = actors.plan(conn, chain_name, start, safe, key=venues.SCAN_KEY)
        todo = sum(b - a + 1 for a, b in ranges)
        say(f"清算の記録を、仕組みを決めずに読む: ブロック {start:,}〜{safe:,}(まだ読んでいない {todo:,}ブロック。"
            f"記録の形 {len(venues.FAMILIES)}種類)")
        mapper = venues.Mapper(conn, chain, out=say)
        added = sum(mapper.scan(a, b) for a, b in ranges)
        say(f"  新しく帳簿に足した記録 {added}件")
        for line in venues.describe(venues.summarize(conn, chain_name, int(time.time() - days * 86400)),
                                    chain=chain_name, days=days, top=top):
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
