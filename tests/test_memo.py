from datetime import timedelta

import pytest

from factory import db, memo
from factory.models import Idea, Memo, match_key, now_jst


def idea(symbol="BTC", tf="1時間", kind="高値抜け", summary="BTC 高値抜けで買い", params=None, type_="値動き"):
    return Idea(summary=summary, symbol=symbol, timeframe=tf, entry_kind=kind, entry_params=params, type=type_)


def test_ids_are_sequential(conn):
    a, _ = memo.register_idea(conn, idea())
    b, _ = memo.register_idea(conn, idea(symbol="ETH"))
    assert (a, b) == ("I-00001", "I-00002")


def test_match_excludes_duplicate_and_logs_memo(conn):
    first, _ = memo.register_idea(conn, idea())
    memo.memo_for_idea(conn, first, "調査", "手数料負け", "値幅0.4%に対し往復手数料0.14%+滑り0.1%で3倍未満")
    idea_id, memo_id = memo.register_idea(conn, idea(symbol=" btc "))
    assert idea_id is None
    m = conn.execute("SELECT * FROM memos WHERE id = ?", (memo_id,)).fetchone()
    assert m["tag"] == "重複" and m["stage"] == "探索"
    assert db.get_idea(conn, first)["stage"] == "否決"


def test_match_ignores_other_tags_and_other_keys(conn):
    first, _ = memo.register_idea(conn, idea())
    memo.memo_for_idea(conn, first, "反証", "一発依存", "最大の1取引が純利益の62%")
    assert memo.register_idea(conn, idea())[0] is not None          # 札が照合対象外
    second, _ = memo.register_idea(conn, idea(kind="平均回帰", params='{"n": 20, "x": 2}'))
    memo.memo_for_idea(conn, second, "調査", "機会不足", "30日で成立4回")
    assert memo.register_idea(conn, idea(kind="平均回帰", params='{"n": 10, "x": 2}'))[0] is not None  # 数字が違う
    assert memo.register_idea(conn, idea(kind="平均回帰", params='{"n": 20, "x": 2.5}'))[0] is not None
    assert memo.register_idea(conn, idea(kind="平均回帰", params='{"x": 2.0, "n": 20}'))[0] is None    # 書き方だけ違う


def test_match_key_includes_the_type(conn):
    """同じ銘柄・入る条件・数字でも、型が違えば照合しない(SPEC 6.3: 同じ型の否決メモとだけ)。"""
    params = '{"x": 0.01}'
    first, _ = memo.register_idea(conn, idea(kind="資金調達料", params=params, type_="歪み"))
    memo.memo_for_idea(conn, first, "調査", "機会不足", "30日で12時間")
    assert memo.register_idea(conn, idea(kind="資金調達料", params=params))[0] is not None          # 値動き型
    assert memo.register_idea(conn, idea(kind="資金調達料", params=params, type_="歪み"))[0] is None
    assert match_key("歪み", " SOL ", "資金調達料", '{"x": 0.010}') == "歪み|sol|資金調達料|n=|x=0.01"


def test_match_window_is_90_days(conn):
    memo.add_memo(conn, Memo(stage="調査", tag="手数料負け", reason="古い", idea_summary="x",
                             symbol="BTC", timeframe="1時間", entry_kind="高値抜け"),
                  at=now_jst() - timedelta(days=91))
    assert memo.register_idea(conn, idea())[0] is not None


def test_memo_validation(conn):
    base = dict(stage="調査", idea_summary="x")
    with pytest.raises(ValueError):
        memo.add_memo(conn, Memo(tag="新しい札", reason="r", **base))
    with pytest.raises(ValueError):
        memo.add_memo(conn, Memo(tag="手数料負け", reason="あ" * 81, **base))
    with pytest.raises(ValueError):
        memo.add_memo(conn, Memo(tag="合格", reason="r", **base))   # 合格は関所だけ
    assert memo.add_memo(conn, Memo(stage="関所", tag="合格", reason="昇格", idea_summary="x"))


def test_tag_counts_and_weekly_metrics(conn):
    first, _ = memo.register_idea(conn, idea())
    memo.memo_for_idea(conn, first, "調査", "手数料負け", "3倍未満")
    memo.register_idea(conn, idea())                    # 除外
    counts = {r["tag"]: r["n"] for r in memo.tag_counts(conn)}
    assert counts == {"手数料負け": 1, "重複": 1}
    week = memo.weekly_metrics(conn, 1)[0]
    assert (week["excluded"], week["registered"], week["passed"]) == (1, 1, 0)


def test_design_notes(conn):
    for i in range(7):
        iid, _ = memo.register_idea(conn, idea(kind=f"種類{i}"))
        memo.memo_for_idea(conn, iid, "paper評価", "連敗", f"連敗8回 {i}")
    assert len(memo.design_notes(conn, "値動き", "BTC", "wallet")) == 5
    assert memo.design_notes(conn, "値動き", "ETH", "wallet") == []
    assert memo.design_notes(conn, "歪み", "BTC", "wallet") == []          # 型が違う


def test_consolidate_merges_same_summary(conn):
    """まとめ(SPEC 6.3 用途3): 案の要約が文字一致するメモを1件にまとめ、まとめ件数に元の数を入れる。"""
    for i in range(3):
        memo.add_memo(conn, Memo(stage="調査", tag="機会不足", reason=f"r{i}", idea_summary="BTC 高値抜けで買い"))
    memo.add_memo(conn, Memo(stage="調査", tag="手数料負け", reason="r", idea_summary="BTC 高値抜けで買い"))
    memo.add_memo(conn, Memo(stage="調査", tag="機会不足", reason="r", idea_summary="BTC 高値抜けで買い", type="歪み"))
    assert memo.consolidate(conn) == (2, 1)
    keep = conn.execute("SELECT * FROM memos WHERE merge_count = 3").fetchone()
    assert keep["reason"] == "r2" and keep["merged_into"] is None
    assert conn.execute("SELECT COUNT(*) FROM memos WHERE merged_into = ?", (keep["id"],)).fetchone()[0] == 2
    assert memo.consolidate(conn) == (0, 0)                                  # 2回目は何もしない
    assert {r["tag"]: r["n"] for r in memo.tag_counts(conn)}["機会不足"] == 4  # 札の集計は落ちた回数のまま


def test_stage_3_database_is_migrated(tmp_path):
    """段階3のデータベース(ボスのPCにあるもの)に、段階3bの列が足され、照合キーと作ったAIが埋まる。"""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)
    stage3 = {"ideas": ["side TEXT", "entry_params TEXT", "take_profit_pct REAL", "stop_loss_pct REAL",
                        "hold_count INTEGER NOT NULL DEFAULT 0"],
              "rules": ["take_profit_pct REAL", "stop_loss_pct REAL"],
              "bots": ["paper_start_ms INTEGER", "refute_note TEXT", "last_error TEXT"]}
    for table, cols in stage3.items():
        for c in cols:
            old.execute(f"ALTER TABLE {table} ADD COLUMN {c}")
    old.execute("""INSERT INTO ideas(id, created_at, source, summary, symbol, timeframe, entry_kind, stage, verdict,
                       updated_at, entry_params) VALUES ('I-00001', 'x', 'wallet', 's', 'BTC', '1h', '高値抜け', '否決',
                       '見送り', 'x', '{"n": 20, "x": 0}')""")
    old.execute("""INSERT INTO memos(id, created_at, stage, tag, reason, idea_summary, idea_id, symbol, entry_kind)
                   VALUES ('M-00001', 'x', '調査', '機会不足', 'r', 's', 'I-00001', 'BTC', '高値抜け')""")
    old.execute("""INSERT INTO memos(id, created_at, stage, tag, reason, idea_summary, symbol, entry_kind)
                   VALUES ('M-00002', 'x', '探索', '重複', 'r', 's', 'ETH', '平均回帰')""")
    old.execute("""INSERT INTO rules(id, symbol, timeframe, entry_condition, exit_condition, max_amount_usd,
                       max_trades_per_day) VALUES ('I-00001', 'BTC', '1h', 'a', 'b', 200, 3)""")
    old.execute("""INSERT INTO bots(id, rule_id, code_path, status, created_at, updated_at)
                   VALUES ('B-0001', 'I-00001', 'bots/B-0001', 'paper評価中', 'x', 'x')""")
    old.execute("""INSERT INTO ai_costs(created_at, stage, bot_id, model, backend, input_tokens, output_tokens, cost_usd)
                   VALUES ('x', 'implement', 'B-0001', 'sonnet', 'api', 1, 1, 0.1)""")
    old.commit()
    old.close()

    conn = db.connect(path)
    try:
        m1, m2 = conn.execute("SELECT * FROM memos ORDER BY id").fetchall()
        assert m1["match_key"] == "値動き|btc|高値抜け|n=20|x=0" and m1["type"] == "値動き" and m1["merge_count"] == 1
        assert m2["match_key"] == "値動き|eth|平均回帰|n=|x="
        b = conn.execute("SELECT * FROM bots").fetchone()
        assert (b["made_by"], b["base_version"], b["continuity"]) == ("sonnet", 0, None)
        r = conn.execute("SELECT * FROM rules").fetchone()
        assert r["type"] == "値動き" and r["max_hold_hours"] is None
        assert conn.execute("SELECT type FROM ideas").fetchone()[0] == "値動き"
    finally:
        conn.close()
    db.connect(path).close()          # 2回目の接続でも壊れない
