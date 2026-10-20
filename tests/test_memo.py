from datetime import timedelta

import pytest

from factory import db, memo
from factory.models import Idea, Memo, now_jst


def idea(symbol="BTC", tf="1時間", kind="高値抜け", summary="BTC 高値抜けで買い"):
    return Idea(summary=summary, symbol=symbol, timeframe=tf, entry_kind=kind)


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
    second, _ = memo.register_idea(conn, idea(kind="平均回帰"))
    memo.memo_for_idea(conn, second, "調査", "機会不足", "30日で成立4回")
    assert memo.register_idea(conn, idea(tf="5分", kind="平均回帰"))[0] is not None  # 時間が違う


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
    assert len(memo.design_notes(conn, "BTC", "wallet")) == 5
    assert memo.design_notes(conn, "ETH", "wallet") == []
