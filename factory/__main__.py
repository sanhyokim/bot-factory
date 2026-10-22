"""コマンド: python -m factory <サブコマンド>"""
from __future__ import annotations

import argparse
import sys

from . import ai_gateway, config, db, memo
from .models import SOURCES, TYPES, Idea, Memo

SAMPLE_IDEAS = [
    ("BTC", "1時間", "高値抜け", "BTCで直近20本の高値を上抜けたら買い、+1.5%で確定・−0.8%で損切り"),
    ("ETH", "15分", "平均回帰", "ETHが20本平均から−2%離れたら買い、平均に戻ったら手仕舞い"),
    ("HYPE", "5分", "高値抜け", "HYPEで出来高急増を伴う高値抜けで買い、+1%で確定・−0.5%で損切り"),
    ("SOL", "1時間", "資金調達料", "SOLの資金調達料が極端なマイナスの時に買い、平常化で手仕舞い"),
    ("BTC", "4時間", "平均回帰", "BTCが4時間足で3本連続陰線の後に買い、+2%で確定・−1%で損切り"),
    ("ETH", "1時間", "安値割れ", "ETHが直近10本の安値を割ったら売り、−1.2%で確定・+0.6%で損切り"),
    ("HYPE", "1時間", "平均回帰", "HYPEの急落(1時間で−4%)後に買い、+2%で確定・−1.5%で損切り"),
    ("SOL", "15分", "高値抜け", "SOLで前日高値を上抜けたら買い、+1%で確定・−0.6%で損切り"),
    ("BTC", "5分", "資金調達料", "BTCの資金調達料の切り替わり直前に逆方向へ入り、30分後に手仕舞い"),
    ("ETH", "4時間", "高値抜け", "ETHが週の高値を上抜けたら買い、+3%で確定・−1.5%で損切り"),
]


def main(argv: list[str] | None = None) -> int:
    config.load_env()
    p = argparse.ArgumentParser(prog="python -m factory")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="データベースを作る")
    sub.add_parser("seed-sample", help="手で書いた案を10件入れる(段階1の確認用)")

    a = sub.add_parser("add-idea", help="案を1件、照合してから登録する")
    a.add_argument("--symbol", required=True)
    a.add_argument("--timeframe", required=True)
    a.add_argument("--entry-kind", required=True, help="入る条件の種類(例: 高値抜け/平均回帰/資金調達料)")
    a.add_argument("--summary", required=True)
    a.add_argument("--hypothesis", default="")
    a.add_argument("--source", default="wallet", choices=SOURCES)
    a.add_argument("--type", default=TYPES[0], choices=TYPES, help="型(値動き/歪み)")
    a.add_argument("--wallet")

    m = sub.add_parser("add-memo", help="否決メモを1件残す")
    m.add_argument("--stage", required=True)
    m.add_argument("--tag", required=True)
    m.add_argument("--reason", required=True)
    m.add_argument("--idea", help="案ID。指定すると案を否決にして要約を写す")
    m.add_argument("--summary", default="")
    m.add_argument("--symbol", default="")
    m.add_argument("--timeframe", default="")
    m.add_argument("--entry-kind", default="")
    m.add_argument("--type", default=TYPES[0], choices=TYPES)

    q = sub.add_parser("ask", help="AI窓口で1問送る(費用帳簿に1行つく)")
    q.add_argument("prompt")
    q.add_argument("--stage", default="manual")

    cb = sub.add_parser("check-bot", help="BOTのフォルダを静的検査し、過去の価格で自動テストする")
    cb.add_argument("path", help="例: bots/sample_breakout")
    cb.add_argument("--days", type=int, default=30)

    ff = sub.add_parser("paper-ff", help="過去の価格で早送りのpaper評価と反証をする(開発用)")
    ff.add_argument("path", help="例: bots/sample_breakout")
    ff.add_argument("--days", type=int, default=30)

    wc = sub.add_parser("explore-check", help="run-daily が読む順に候補の財布を少し読み、探索の絞り込みで何件落ちるかを見る(開発用)")
    wc.add_argument("--wallets", type=int, default=30, help="読む財布の数")

    sub.add_parser("funding-check", help="探索の入口2が今どの銘柄を案にするかを見る(登録はしない。開発用)")

    sub.add_parser("dashboard", help="画面を http://localhost:8800 で開く")
    sub.add_parser("feed", help="価格の受け取り口: 動いているBOTの銘柄の価格を決まった間隔で取り込む(見張り役が起こす)")
    sub.add_parser("watchdog", help="見張り役: 画面と価格の受け取り口を起こし、落ちたら再起動する")
    rd = sub.add_parser("run-daily", help="工場を1周させる(探索〜実装、paper評価・反証)")
    rd.add_argument("--wait-minutes", type=float, help="探索のAIの答えを待つ分数(既定は config.yaml)")
    rd.add_argument("--skip-explore", action="store_true", help="探索を行わない(順位表を読まない)")

    args = p.parse_args(argv)
    if args.cmd == "dashboard":
        from .dashboard.app import serve
        serve()
        return 0
    if args.cmd == "feed":
        from . import logs
        from .hyperliquid import feed
        feed.run_forever(interval_seconds=config.load_config()["feed"]["interval_minutes"] * 60,
                         log=lambda text: logs.write("feed", text))
        return 0
    if args.cmd == "watchdog":
        from . import watchdog
        print("見張り役を始めた。止める時は Ctrl+C(見張っているプロセスも止まる)")
        try:
            watchdog.from_config().run_forever(config.load_config()["watchdog"]["check_seconds"])
        except KeyboardInterrupt:
            pass
        return 0
    if args.cmd == "funding-check":
        from .hyperliquid.reader import Reader, ReaderError
        from .market import Market
        from .stages import explore_funding
        conn = db.connect()
        try:
            rep = explore_funding.find(conn, Market(conn, Reader()),
                                       skip=explore_funding.recent_symbols(
                                           conn, config.load_config()["explore_funding"]["reregister_days"]))
            print("\n".join(explore_funding.describe(rep)))
        except ReaderError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        return 0
    if args.cmd == "run-daily":
        from .runner import run_daily
        run_daily(wait_minutes=args.wait_minutes, skip_explore=args.skip_explore)
        return 0
    if args.cmd in ("check-bot", "paper-ff"):
        return _bot_command(args)
    if args.cmd == "explore-check":
        from . import walletcheck
        from .hyperliquid.reader import Reader, ReaderError
        try:
            print(walletcheck.run(Reader(), args.wallets))
        except ReaderError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        return 0

    conn = db.connect()
    try:
        if args.cmd == "init":
            print(f"OK: {config.db_path()}")
        elif args.cmd == "seed-sample":
            for symbol, tf, kind, summary in SAMPLE_IDEAS:
                idea_id, memo_id = memo.register_idea(conn, Idea(
                    summary=summary, symbol=symbol, timeframe=tf, entry_kind=kind, source="wallet"))
                print(idea_id or f"除外 {memo_id}", summary)
        elif args.cmd == "add-idea":
            idea_id, memo_id = memo.register_idea(conn, Idea(
                summary=args.summary, hypothesis=args.hypothesis, source=args.source, type=args.type,
                wallet=args.wallet, symbol=args.symbol, timeframe=args.timeframe,
                entry_kind=args.entry_kind))
            print(f"登録 {idea_id}" if idea_id else f"照合で除外(否決メモ {memo_id})")
        elif args.cmd == "add-memo":
            if args.idea:
                memo_id = memo.memo_for_idea(conn, args.idea, args.stage, args.tag, args.reason)
            else:
                memo_id = memo.add_memo(conn, Memo(
                    stage=args.stage, tag=args.tag, reason=args.reason, idea_summary=args.summary,
                    symbol=args.symbol, timeframe=args.timeframe, entry_kind=args.entry_kind, type=args.type))
            print(f"否決メモ {memo_id}")
        elif args.cmd == "ask":
            r = ai_gateway.ask(conn, args.stage, args.prompt)
            print(r.text)
            print(f"--- {r.model} 入力{r.input_tokens}/出力{r.output_tokens}トークン ${r.cost_usd:.6f}",
                  file=sys.stderr)
    except (ValueError, KeyError, ai_gateway.AIError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _bot_command(args) -> int:
    from . import botrun, fastforward
    from .botbase import BotRejected
    from .hyperliquid.reader import Reader, ReaderError

    try:
        bot = botrun.load(args.path)
        print(f"静的検査: OK({args.path})")
        data = fastforward.fetch(Reader(), bot, args.days)
        out = fastforward.run(bot, data)
        print(fastforward.report(bot, data, out, with_paper=args.cmd == "paper-ff"))
        if args.cmd == "check-bot":
            return 0 if all(c.ok for c in out["checks"]) else 1
    except (BotRejected, FileNotFoundError) as e:
        print(f"エラー: BOTが土台の決まりを守っていない: {e}", file=sys.stderr)
        return 1
    except (ReaderError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
