"""run-daily を偽の取引所と偽のAIで通しで動かす(SPEC 10章 段階3)。"""
from factory import config, db
from factory.hyperliquid.reader import now_ms
from factory.runner import run_daily
from tests.fakes import DAY, board_row, trader_fills

GOOD = "0x" + "a" * 40
WHALE = "0x" + "b" * 40
LAZY = "0x" + "c" * 40


def setup_wallets(exchange):
    start = now_ms() - 20 * DAY
    exchange.board = [board_row(GOOD), board_row(WHALE, month_pnl=9_000), board_row(LAZY, month_pnl=500),
                      board_row("0x" + "d" * 40, month_pnl=-100)]
    exchange.fills = {GOOD: trader_fills("BTC", start, 60),
                      WHALE: trader_fills("BTC", start, 60, notional=50_000),
                      LAZY: trader_fills("BTC", start, 10)}


def test_day_one_moves_an_idea_from_registration_to_paper(conn, fake_ai, exchange, reader):
    setup_wallets(exchange)
    lines = run_daily(reader=reader, wait_minutes=0, out=lambda s: None)
    text = "\n".join(lines)
    assert "[探索] 順位表の候補 3財布" in text and "絞り込み通過 1" in text, text
    # 3つの仮説: 1つは継続、1つは手数料負け、1つは取引所にない銘柄
    ideas = {r["summary"][:6]: r for r in conn.execute("SELECT * FROM ideas")}
    assert len(ideas) == 3
    tags = sorted(r["tag"] for r in conn.execute("SELECT tag FROM memos"))
    assert tags == ["手数料負け", "未対応"], text
    good = conn.execute("SELECT * FROM ideas WHERE symbol = 'BTC' AND entry_kind = '高値抜け'").fetchone()
    assert (good["stage"], good["verdict"]) == ("paper評価", "継続"), text
    bot = conn.execute("SELECT * FROM bots").fetchone()
    assert bot["status"] == "paper評価中" and bot["rule_id"] == good["id"]
    assert (config.bots_dir() / bot["id"] / "strategy.py").exists()
    assert fake_ai.calls.count("explore") == 1 and "design" in fake_ai.calls and "implement" in fake_ai.calls
    # 財布は7日間は再分析しない
    assert conn.execute("SELECT COUNT(*) FROM wallet_scans").fetchone()[0] == 3
    # ログが残る
    logs = list((config.db_path().parent / "logs").glob("run-daily-*.log"))
    assert logs and "run-daily 終了" in logs[0].read_text(encoding="utf-8")


def test_second_day_skips_scanned_wallets_and_runs_paper(conn, fake_ai, exchange, reader):
    setup_wallets(exchange)
    run_daily(reader=reader, wait_minutes=0, out=lambda s: None)
    # 試運転を20日前から始めていたことにして、もう一度回す
    conn.execute("UPDATE bots SET paper_start_ms = ?", (now_ms() - 20 * DAY,))
    conn.commit()
    before = exchange.count("userFillsByTime")
    lines = run_daily(reader=reader, wait_minutes=0, out=lambda s: None)
    text = "\n".join(lines)
    assert exchange.count("userFillsByTime") == before, "7日以内の財布をまた読んだ"
    assert "7日以内に分析済み 3" in text
    bot = conn.execute("SELECT * FROM bots").fetchone()
    trades = conn.execute("SELECT COUNT(*) FROM trades WHERE bot_id = ?", (bot["id"],)).fetchone()[0]
    assert trades > 0, text
    assert bot["status"] in ("paper評価中", "否決", "本番待ち")


def test_a_failing_stage_does_not_stop_the_others(conn, fake_ai, exchange, reader):
    exchange.fail.add("leaderboard")
    db.insert_idea(conn, __import__("factory.models", fromlist=["Idea"]).Idea(
        summary="手で入れた案", symbol="BTC", timeframe="1h", entry_kind="高値抜け", side="long",
        entry_params='{"n": 20, "x": 0}', take_profit_pct=1.5, stop_loss_pct=0.8))
    lines = run_daily(reader=reader, wait_minutes=0, out=lambda s: None)
    text = "\n".join(lines)
    assert "[探索] 止まった" in text
    assert "[調査] 継続 1" in text and "[実装] paper評価を開始 B-0001" in text, text
