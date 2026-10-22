from fastapi.testclient import TestClient

from factory.__main__ import main
from factory.dashboard.app import app


def test_ten_manual_ideas_show_on_dashboard(conn):
    assert main(["seed-sample"]) == 0
    html = TestClient(app).get("/").text
    for n in range(1, 11):
        assert f"I-{n:05d}" in html
    assert "探索 / 案登録" in html
    assert "1. 要判断" in html and "5. 否決メモ" in html


def test_filter_and_detail(conn):
    main(["seed-sample"])
    main(["add-memo", "--idea", "I-00002", "--stage", "調査", "--tag", "機会不足", "--reason", "30日で成立4回"])
    client = TestClient(app)
    html = client.get("/", params={"stage": "否決"}).text
    assert "I-00002" in html and "I-00003" not in html.split("案の一覧")[1].split("5. 否決メモ")[0]
    assert "機会不足" in client.get("/ideas/I-00002").text
    assert client.get("/ideas/I-99999").status_code == 404


def test_paper_bots_and_costs_panels(conn, fake_ai, reader):
    from tests.test_implement_paper import ready_idea
    from factory.market import Market
    from factory.stages import implement

    idea = ready_idea(conn)
    implement.run(conn, Market(conn, reader))
    client = TestClient(app)
    html = client.get("/").text
    assert "4. paper評価中" in html and "B-0001" in html and "開始待ち" in html
    assert "6. 費用" in html and "<td>実装</td>" in html and "クレジット残高の見込み" in html
    detail = client.get(f"/ideas/{idea}").text
    assert "BOT B-0001" in detail and "paper評価中" in detail


def test_stage_3b_panels(conn):
    """枠2の型の絞り込み・枠5の型別の集計とまとめ・枠6のAI別の黒字集計と見張り役・枠7の札ごとの歩留まり。"""
    from factory import db, memo
    from factory.models import Idea, Memo

    def bot(bot_id, idea, made_by, continuity, competition, status, live=None, passed=False):
        conn.execute("""INSERT INTO bots(id, rule_id, code_path, status, created_at, updated_at, paper_start_ms,
                            made_by, continuity, competition, refute_note, base_version)
                        VALUES (?,?,?,?,'x','x',1,?,?,?,?,1)""",
                     (bot_id, idea, "bots/" + bot_id, status, made_by, continuity, competition,
                      "反証の結果" if passed else None))
        if live is not None:
            conn.execute("""INSERT INTO trades(bot_id, kind, entry_at, entry_price, entry_qty, net_pnl_usd)
                            VALUES (?, 'live', 'x', 1, 1, ?)""", (bot_id, live))

    ideas = []
    for i, type_ in enumerate(["値動き", "歪み", "歪み"]):
        idea = db.insert_idea(conn, Idea(summary=f"案{i}", symbol="BTC", timeframe="1h", entry_kind="高値抜け",
                                         type=type_, source="funding" if type_ == "歪み" else "wallet"))
        conn.execute("""INSERT INTO rules(id, symbol, timeframe, entry_condition, exit_condition, max_amount_usd,
                            max_trades_per_day, stop_loss_pct, type, max_hold_hours)
                        VALUES (?, 'BTC', '1h', 'a', 'b', 200, 3, 1, ?, 6)""", (idea, type_))
        ideas.append(idea)
    bot("B-0001", ideas[0], "sonnet", "常にある", "競争あり", "否決")
    bot("B-0002", ideas[1], "sonnet", "常にある", "競争なし", "本番運用中", live=4.5, passed=True)
    bot("B-0003", ideas[2], "opus", "一回きり", "競争なし", "本番運用中", live=-1.0, passed=True)
    conn.execute("INSERT INTO ai_costs(created_at, stage, bot_id, model, backend, input_tokens, output_tokens, cost_usd)"
                 " VALUES ('x', 'implement', 'B-0002', 'sonnet', 'api', 1, 1, 0.25)")
    for _ in range(2):
        memo.add_memo(conn, Memo(stage="調査", tag="機会不足", reason="r", idea_summary="同じ要約", type="歪み"))
    memo.consolidate(conn)
    conn.execute("INSERT INTO exchange_refusals(created_at, process, count) VALUES (?, 'run-daily', 7)",
                 (__import__("factory.models", fromlist=["now_jst"]).now_jst().isoformat(),))
    conn.commit()

    client = TestClient(app)
    html = client.get("/").text
    assert "7. 札ごとの歩留まり" in html
    assert "歪み</td><td>2本 → 2本 → 1本" in html and "値動き</td><td>1本 → 0本 → 0本" in html
    assert "一回きり</td><td>1本 → 1本 → 0本" in html and "競争なし</td><td>2本 → 2本 → 1本" in html
    assert "AI別の黒字集計" in html and "Sonnet</a></td><td>2</td><td>1</td><td>1</td>" in html
    assert "$+4.50" in html and "$0.2500" in html and "50.0%" in html
    assert "歪み型: 直近7日の札" in html and "1件にまとめた(元のメモ 2件)" in html
    assert "断られた回数: 今日 <b>7</b>回" in html
    only = client.get("/", params={"type": "歪み"}).text.split("案の一覧")[1].split("4. paper評価中")[0]
    assert ideas[1] in only and ideas[0] not in only
    by_ai = client.get("/", params={"made_by": "opus"}).text.split("案の一覧")[1].split("4. paper評価中")[0]
    assert ideas[2] in by_ai and ideas[1] not in by_ai
    detail = client.get(f"/ideas/{ideas[1]}").text
    assert "土台の版 / 作ったAI" in detail and "Sonnet" in detail and "常にある / 競争なし" in detail
