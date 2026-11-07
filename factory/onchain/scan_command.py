"""python -m factory actor-scan: 清算で稼いでいる主体を探して表にする(読み取りだけ)。"""
from __future__ import annotations

import sys
import time
from collections.abc import Callable

from .. import config, db, logs
from ..models import now_jst
from . import actors
from .abi import to_int
from .cache import ChainCache
from .chain import Chain
from .rpc import RpcError

PROCESS = "actor-scan"


def _start_ts(chain: Chain, start: int) -> int:
    """表の窓の始まり。読み始めたブロックの時刻から数える(読み終えた時刻から数えると、読んでいた間の分だけ前が欠ける)。"""
    return to_int((chain.block(start) or {}).get("timestamp", "0x0"))


def run(chain_name: str = "arbitrum", days: float = 7, top: int = 15, *, chain: Chain | None = None,
        conn=None, out: Callable[[str], None] | None = None,
        summary_out: dict | None = None) -> int:
    own = conn is None
    conn = conn or db.connect()
    cache = ChainCache() if chain is None else None
    chain = chain or Chain.from_config(chain_name, cache=cache, usage_conn=conn)
    lines: list[str] = []

    def say(text: str) -> None:
        lines.append(text)
        (out or print)(text)

    try:
        block_seconds = chain.block_time(config.load_config()["onchain"]["chains"][chain_name].get("block_seconds", 0.25))
        safe = chain.safe_head()
        start = max(0, safe - int(days * 86400 / block_seconds) + 1)
        ranges = actors.plan(conn, chain_name, start, safe)
        todo = sum(b - a + 1 for a, b in ranges)
        say(f"清算の記録を読む: ブロック {start:,}〜{safe:,}(まだ読んでいない {todo:,}ブロック)")
        scanner = actors.Scanner.from_config(conn, chain, out=say)
        added = sum(scanner.scan(a, b) for a, b in ranges)
        say(f"  新しく帳簿に足した清算 {added}件")
        since = _start_ts(chain, start)
        actors.backfill_timeboost(conn, chain, since)
        summary = actors.summarize(conn, chain_name, since)
        if summary_out is not None:
            summary_out["actors"] = summary
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
    block_seconds = chain.block_time(cfg["chains"][chain_name].get("block_seconds", 0.25))
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
               conn=None, out: Callable[[str], None] | None = None,
               summary_out: dict | None = None, known: bool = False) -> int:
    """python -m factory venue-scan: 清算の記録を出している仕組みを、住所を決めずにすべて探す(清算の地図)。
    known=True なら、config.yaml の compound_comptrollers に書いた Compound の写しだけを、市場の住所を決めて読む
    (無料の接続先の多くは、住所を決めないログを狭い範囲でしか返さないので)。"""
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
        cfg = config.load_config()["onchain"]["chains"][chain_name]
        block_seconds = chain.block_time(cfg.get("block_seconds", 0.25))
        safe = chain.safe_head()
        start = max(0, safe - int(days * 86400 / block_seconds) + 1)
        mapper = venues.Mapper(conn, chain, out=say)
        if known:
            places = cfg.get("compound_comptrollers") or {}
            if not places:
                say(f"config.yaml の {chain_name} に、住所を決めて読む仕組み(compound_comptrollers)が書いていない")
                return 1
            added, read_any = 0, False
            for name, ctrl in places.items():
                markets = mapper.markets(ctrl)
                if not markets:
                    say(f"{name}({ctrl})の市場の一覧(getAllMarkets())が読めなかった")
                    continue
                key = venues.known_key(ctrl)
                ranges = actors.plan(conn, chain_name, start, safe, key=key)
                todo = sum(b - a + 1 for a, b in ranges)
                say(f"{name} の清算の記録を、市場 {len(markets)}か所に決めて読む: ブロック {start:,}〜{safe:,}"
                    f"(まだ読んでいない {todo:,}ブロック)")
                added += sum(mapper.scan(a, b, markets=markets, key=key) for a, b in ranges)
                read_any = True
            if not read_any:
                return 1
        else:
            ranges = actors.plan(conn, chain_name, start, safe, key=venues.SCAN_KEY)
            todo = sum(b - a + 1 for a, b in ranges)
            say(f"清算の記録を、仕組みを決めずに読む: ブロック {start:,}〜{safe:,}(まだ読んでいない {todo:,}ブロック。"
                f"記録の形 {len(venues.FAMILIES)}種類)")
            added = sum(mapper.scan(a, b) for a, b in ranges)
        say(f"  新しく帳簿に足した記録 {added}件")
        summary = venues.summarize(conn, chain_name, _start_ts(chain, start))
        if summary_out is not None:
            summary_out["venues"] = summary
        for line in venues.describe(summary, chain=chain_name, days=days, top=top):
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


def run_compound(chain_name: str = "arbitrum", days: float = 7, top: int = 5, *, chain: Chain | None = None,
                 conn=None, out: Callable[[str], None] | None = None) -> int:
    """python -m factory compound-scan: 清算の地図に載った Compound の写しの清算を1件ずつ深く読む
    (利益・清算できるようになってから取られるまでの速さ・理由)。"""
    from . import compound

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
        reader = compound.Reader.from_config(conn, chain, out=say)
        st = reader.run(since)
        say(f"Compound の写しの清算を読む: まだ読んでいない {st['todo']}件のうち {st['done']}件を読んだ"
            f"(読み取り {st['reads']:,}回{'、読めずに飛ばした ' + str(st['skipped']) + '件' if st['skipped'] else ''})")
        for line in compound.describe(compound.summarize(conn, chain_name, since), chain=chain_name, days=days,
                                      top=top):
            say(line)
        return 0
    except RpcError as e:
        say(f"エラー: {e}(読み終えた分は残っている。もう一度動かすと続きから読む。"
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


def run_arbs(chain_name: str = "arbitrum", hours: float = 2, top: int = 15, *, chain: Chain | None = None,
             conn=None, out: Callable[[str], None] | None = None,
             summary_out: dict | None = None) -> int:
    """python -m factory arb-scan: 交換の記録を住所を決めずに読み、1回の取引の中で元に戻る交換(裁定)で
    稼いでいる主体を探す。"""
    from . import arbs

    own = conn is None
    conn = conn or db.connect()
    cache = ChainCache() if chain is None else None
    chain = chain or Chain.from_config(chain_name, cache=cache, usage_conn=conn)
    lines: list[str] = []

    def say(text: str) -> None:
        lines.append(text)
        (out or print)(text)

    try:
        cfg = config.load_config()["onchain"]
        block_seconds = chain.block_time(cfg["chains"][chain_name].get("block_seconds", 0.25))
        a = cfg.get("arbs", {})
        safe = chain.safe_head()
        start = max(0, safe - int(hours * 3600 / block_seconds) + 1)
        ranges = actors.plan(conn, chain_name, start, safe, key=arbs.SCAN_KEY)
        todo = sum(b - a_ + 1 for a_, b in ranges)
        say(f"交換の記録を、住所を決めずに読む: ブロック {start:,}〜{safe:,}(まだ読んでいない {todo:,}ブロック。"
            "記録が多いので、初回は時間がかかる)")
        scanner = arbs.ArbScanner.from_config(conn, chain, out=say)
        added = sum(scanner.scan(a_, b) for a_, b in ranges)
        say(f"  新しく帳簿に足した裁定 {added:,}件")
        since = _start_ts(chain, start)
        read = scanner.sample(since, actors_n=a.get("sample_actors", 15), per_actor=a.get("sample_per_actor", 20))
        if read:
            say(f"  上位の主体の受領書を {read}件読んだ(財布・ガス代・優先レーン)")
        summary = arbs.summarize(conn, chain_name, since)
        if summary_out is not None:
            summary_out["arbs"] = summary
        for line in arbs.describe(summary, chain=chain_name, hours=hours, top=top, swaps=scanner.swaps_seen,
                                  txs=scanner.txs_seen):
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


def overview_line(chain_name: str, s: dict) -> str:
    """1つのチェーンの結果を1行に(chain-survey の最後のまとめ)。"""
    parts = [f"{chain_name}:"]
    a = s.get("actors")
    if a is not None:
        net = sum(x["net_usd"] for x in a)
        lead = f"、1位 {a[0]['net_usd'] / net:.0%}" if a and net > 0 else ""
        parts.append(f"Aave の清算 {sum(x['count'] for x in a)}件・{len(a)}主体・純利益 ${net:,.2f}{lead}")
    v = s.get("venues")
    if v is not None:
        thin = [x for x in v if x["liquidators"] <= 2 and x["count"] >= 5]
        parts.append(f"清算の仕組み {len(v)}か所・{sum(x['count'] for x in v)}件"
                     f"(5件以上で主体が2以下 {len(thin)}か所・{sum(x['count'] for x in thin)}件)")
    r = s.get("arbs")
    if r is not None:
        acts = r["actors"]
        lead = f"、1位 {acts[0]['gross_usd'] / r['gross_usd']:.0%}" if acts and r["gross_usd"] > 0 else ""
        same = sum(x["same_block"] for x in acts)
        trig = sum(x["with_trigger"] for x in acts)
        parts.append(f"裁定 {r['count']:,}件・{len(acts)}主体・粗利 ${r['gross_usd']:,.2f}{lead}"
                     + (f"、直前の交換と同じブロック {same / trig:.0%}" if trig else ""))
    failed = s.get("failed", [])
    if len(parts) == 1:
        parts.append("読めなかった")
    elif failed:
        parts.append("読めなかった: " + "・".join(failed))
    return f"  {parts[0]}  " + " / ".join(parts[1:])


def run_survey(chains: list[str] | None = None, days: float = 7, hours: float = 1, top: int = 5, *,
               conn=None, out: Callable[[str], None] | None = None, chain_factory=None) -> int:
    """python -m factory chain-survey: チェーンごとに、Aave の清算(actor-scan)・清算の地図(venue-scan)・
    裁定(arb-scan)を順に動かし、最後に1チェーン1行でまとめる。1つのチェーンで失敗しても、次のチェーンへ進む。
    1つのチェーンの3つの道具は、同じ読み手(中継)を使う。接続先について分かったこと(ログの範囲の上限など)を
    次の道具に持ち越し、チェーンごとに画面に出す。"""
    from .check import endpoint_notes

    own = conn is None
    conn = conn or db.connect()
    say = out or print
    names = chains or [c for c in config.load_config()["onchain"]["chains"] if c != "arbitrum"]
    results: dict[str, dict] = {}
    try:
        for i, name in enumerate(names, 1):
            say(f"===== {i}/{len(names)} {name} =====")
            s: dict = {}
            results[name] = s
            cache = None
            try:
                if chain_factory:
                    chain = chain_factory(name)
                else:
                    cache = ChainCache()
                    chain = Chain.from_config(name, cache=cache, usage_conn=conn)
            except Exception as e:  # noqa: BLE001  1つのチェーンの失敗で、ほかを止めない
                say(f"  {name} は読めなかった({type(e).__name__}: {str(e)[:120]})")
                continue
            try:
                for step, fn, span in (("Aave の清算", run, days), ("清算の地図", run_venues, days),
                                       ("裁定", run_arbs, hours)):
                    try:
                        if fn(name, span, top, chain=chain, conn=conn, out=say, summary_out=s):
                            s.setdefault("failed", []).append(step)
                    except Exception as e:  # noqa: BLE001  1つの道具の失敗で、ほかを止めない
                        s.setdefault("failed", []).append(step)
                        say(f"  {name} の{step}は読めなかった({type(e).__name__}: {str(e)[:120]})")
                notes = endpoint_notes(chain.relay, indent="  ")
                if notes:
                    say(f"{name} の接続先について分かったこと")
                    for line in notes:
                        say(line)
            finally:
                if cache is not None:
                    cache.close()
        say(f"===== まとめ(清算は直近{days:g}日、裁定は直近{hours:g}時間) =====")
        for name in names:
            say(overview_line(name, results[name]))
        return 0
    finally:
        if own:
            conn.close()
