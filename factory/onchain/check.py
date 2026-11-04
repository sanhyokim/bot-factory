"""回線の確かめ(python -m factory chain-check)。ボスのPCで動かして、無料の接続先が使えるかを見る。

1. 接続先を1つずつ直接呼び、チェーンの番号・最新のブロック・かかった時間と、ログを1回で何ブロックまで読めるかを見る
2. 中継を通して、Aave v3 の直近の記録(種類を問わず)と、確定したブロックまでの清算の記録を読む。
   範囲が広すぎると断られた接続先は、ログを読む時だけ外す
3. 同じ範囲をもう一度読み、キャッシュから返る(回線を使わない)ことを見る

読み取りだけ。鍵もお金も使わない。
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import datetime

import httpx

from .. import config
from ..models import JST
from .abi import to_int
from .actors import LIQUIDATION_CALL, parse_liquidation
from .cache import ChainCache
from .chain import Chain
from .rpc import PURPOSE_NAMES, Relay, RpcError, _short, probe, probe_logs


def run(chain_name: str = "arbitrum", hours: float = 24, *, client: httpx.Client | None = None,
        cache: ChainCache | None = None, usage_conn: sqlite3.Connection | None = None,
        out: Callable[[str], None] = print) -> bool:
    cfg = config.load_config()["onchain"]["chains"][chain_name]
    timeout = config.load_config()["onchain"].get("rpc", {}).get("timeout_seconds", 15)
    client = client or httpx.Client(timeout=timeout)
    ok = True

    out(f"チェーン: {chain_name}(chain_id {cfg['chain_id']})")
    out("1. 接続先を1つずつ確かめる")
    alive = 0
    pool = cfg.get("protocols", {}).get("aave_v3_pool")
    for url in cfg["endpoints"]:
        p = probe(client, url)
        if p["ok"] and p["chain_id"] != cfg["chain_id"]:
            out(f"  違う  {url}  chain_id {p['chain_id']}({chain_name} ではない)")
        elif p["ok"]:
            alive += 1
            out(f"  OK    {url}  ブロック {p['block']:,}  {p['ms']}ミリ秒")
            if pool:
                lg = probe_logs(client, url, pool, p["block"])
                if lg["max_ok"] is None:
                    out(f"          ログ: 読めない({lg['error']})")
                elif lg["error"]:
                    out(f"          ログ: {lg['max_ok']:,}ブロックまで読めた(断られた言い方 {lg['error']})")
                else:
                    out(f"          ログ: {lg['max_ok']:,}ブロックまで読めた(Aave の記録 {lg['count']}件)")
        else:
            out(f"  NG    {url}  {p['error']}")
    out(f"  → 使える接続先 {alive}/{len(cfg['endpoints'])}")
    if not alive:
        out("エラー: 使える接続先が1つもない。ネットにつながっているか、会社などのネットで止められていないかを見てください")
        return False

    relay = Relay.from_config(chain_name, client=client, cache=cache, usage_conn=usage_conn)
    chain = Chain(relay, cache_depth=cfg.get("cache_depth", 2400), max_log_blocks=cfg.get("max_log_blocks", 10_000),
                  min_log_blocks=cfg.get("min_log_blocks", 100))
    try:
        out("2. 中継を通して読む")
        head = chain.head()
        safe = chain.safe_head()
        blk = chain.block(safe)
        when = datetime.fromtimestamp(to_int(blk["timestamp"]), JST).strftime("%Y-%m-%d %H:%M:%S")
        out(f"  最新のブロック {head:,}。確定とみなすブロック {safe:,}({when} 日本時間)")
        if pool:
            n_blocks = int(hours * 3600 / cfg.get("block_seconds", 0.25))
            start = max(0, safe - n_blocks + 1)
            if chain.code(pool) in ("0x", "0x0", None):
                out(f"  エラー: Aave v3 の仕組み {pool} がこのチェーンに無い(設定の住所を確かめる)")
                ok = False
            else:
                recent = chain.logs(from_block=safe - 999, to_block=safe, address=pool)
                out(f"  Aave v3 の記録(直近1,000ブロック、種類を問わず): {len(recent)}件"
                    + ("" if recent else "。0件はおかしい(ログが読めていない見込み)"))
                ok = ok and bool(recent)
                logs = chain.logs(from_block=start, to_block=safe, address=pool, topics=[LIQUIDATION_CALL])
                who = Counter(parse_liquidation(lg).liquidator for lg in logs)
                out(f"  Aave v3 の清算の記録(直近{hours:g}時間、ブロック {start:,}〜{safe:,}): {len(logs)}件、"
                    f"清算した主体 {len(who)}件。1回で読んだブロック数 {chain.log_step:,}")
                for addr, n in who.most_common(3):
                    out(f"    {addr}  {n}回")
                if cache is not None:
                    out("3. 同じ範囲をもう一度読む(キャッシュから返るはず)")
                    before = _network_calls(relay)
                    again = chain.logs(from_block=start, to_block=safe, address=pool, topics=[LIQUIDATION_CALL])
                    used = _network_calls(relay) - before
                    same = "同じ" if len(again) == len(logs) else f"違う({len(again)}件)"
                    out(f"  {len(again)}件({same})。回線を使った回数 {used}回(0回なら正しい)")
                    ok = ok and used == 0 and len(again) == len(logs)
    except RpcError as e:
        out(f"エラー: {e}")
        ok = False
    finally:
        relay.flush()

    narrow = [ep for ep in relay.endpoints if ep.log_limit]
    if narrow:
        out("ログの範囲で断られた接続先(ログを読む時だけ外した)")
        for ep in narrow:
            out(f"  {_short(ep.url)}: 断られた言い方「{ep.log_note}」→ ログは{ep.log_limit:,}ブロック以下の時だけ使う")
    out("接続先ごとに使った回数(呼んだ/失敗/断られた/キャッシュ)")
    for (endpoint, purpose), c in sorted(relay.totals.items()):
        name = endpoint if endpoint == "cache" else _short(endpoint)
        out(f"  {name}({PURPOSE_NAMES[purpose]}): {c['calls']}/{c['errors']}/{c['rate_limited']}/{c['cache_hits']}")
    out("結果: OK" if ok else "結果: 問題あり(上のエラーを見てください)")
    return ok


def _network_calls(relay: Relay) -> int:
    return sum(c["calls"] for (ep, _), c in relay.totals.items() if ep != "cache")
