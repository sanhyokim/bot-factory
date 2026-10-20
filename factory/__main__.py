"""コマンド: python -m factory <サブコマンド>"""
from __future__ import annotations

import argparse
import sys

from . import ai_gateway, config, db, memo
from .models import Idea, Memo

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
    a.add_argument("--source", default="wallet", choices=["wallet", "market"])
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

    q = sub.add_parser("ask", help="AI窓口で1問送る(費用帳簿に1行つく)")
    q.add_argument("prompt")
    q.add_argument("--stage", default="manual")

    sub.add_parser("dashboard", help="画面を http://localhost:8800 で開く")
    sub.add_parser("run-daily", help="工場を1周させる")

    args = p.parse_args(argv)
    if args.cmd == "dashboard":
        from .dashboard.app import serve
        serve()
        return 0
    if args.cmd == "run-daily":
        from .runner import run_daily
        run_daily()
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
                summary=args.summary, hypothesis=args.hypothesis, source=args.source,
                wallet=args.wallet, symbol=args.symbol, timeframe=args.timeframe,
                entry_kind=args.entry_kind))
            print(f"登録 {idea_id}" if idea_id else f"照合で除外(否決メモ {memo_id})")
        elif args.cmd == "add-memo":
            if args.idea:
                memo_id = memo.memo_for_idea(conn, args.idea, args.stage, args.tag, args.reason)
            else:
                memo_id = memo.add_memo(conn, Memo(
                    stage=args.stage, tag=args.tag, reason=args.reason, idea_summary=args.summary,
                    symbol=args.symbol, timeframe=args.timeframe, entry_kind=args.entry_kind))
            print(f"否決メモ {memo_id}")
        elif args.cmd == "ask":
            r = ai_gateway.ask(conn, args.stage, args.prompt)
            print(r.text)
            print(f"--- {r.model} 入力{r.input_tokens}/出力{r.output_tokens}トークン ${r.cost_usd:.6f}",
                  file=sys.stderr)
    except (ValueError, KeyError, ai_gateway.BudgetExceeded, ai_gateway.SecretLeak) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
