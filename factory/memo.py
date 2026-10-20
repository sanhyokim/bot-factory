"""否決メモ: 登録・照合・集計(SPEC 6章)。

照合はAIを使わず、「銘柄+時間の単位+入る条件の種類」の文字一致で行う(SPEC 6.3 用途1)。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import config, db
from .models import MEMO_STAGES, PASS_TAG, TAGS, Idea, Memo, now_jst

REASON_MAX = 80
DESIGN_NOTES_MAX = 5


def _norm(s: str) -> str:
    """文字一致の前に、前後の空白と大文字小文字の違いだけをそろえる。"""
    return (s or "").strip().casefold()


def add_memo(conn: sqlite3.Connection, memo: Memo, *, at: datetime | None = None) -> str:
    if memo.stage not in MEMO_STAGES:
        raise ValueError(f"工程が不正: {memo.stage}")
    if memo.tag not in TAGS and memo.tag != PASS_TAG:
        raise ValueError(f"札が一覧にない: {memo.tag}(札の追加はボスの承認が必要)")
    if memo.tag == PASS_TAG and memo.stage != "関所":
        raise ValueError("札『合格』は関所(本番昇格)の記録にだけ使う")
    reason = memo.reason.strip()
    if not reason:
        raise ValueError("一言の理由が空")
    if len(reason) > REASON_MAX:
        raise ValueError(f"一言の理由は{REASON_MAX}字以内({len(reason)}字)")
    memo_id = db.next_id(conn, "memo")
    ts = (at or now_jst()).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO memos(id, created_at, stage, tag, reason, idea_summary, idea_id,
               symbol, timeframe, entry_kind, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (memo_id, ts, memo.stage, memo.tag, reason, memo.idea_summary, memo.idea_id,
         _norm(memo.symbol), _norm(memo.timeframe), _norm(memo.entry_kind), memo.source),
    )
    conn.commit()
    return memo_id


def memo_for_idea(conn: sqlite3.Connection, idea_id: str, stage: str, tag: str, reason: str) -> str:
    """案を否決し、その案の要約と照合キーを写した否決メモを残す。"""
    idea = db.get_idea(conn, idea_id)
    if idea is None:
        raise KeyError(idea_id)
    memo_id = add_memo(conn, Memo(
        stage=stage, tag=tag, reason=reason, idea_summary=idea["summary"], idea_id=idea_id,
        symbol=idea["symbol"], timeframe=idea["timeframe"], entry_kind=idea["entry_kind"],
        source=idea["source"],
    ))
    if tag != PASS_TAG:
        db.set_idea_state(conn, idea_id, "否決", "見送り")
    return memo_id


def find_match(conn: sqlite3.Connection, symbol: str, timeframe: str, entry_kind: str,
               *, now: datetime | None = None) -> sqlite3.Row | None:
    """登録前照合。過去90日・対象の札で、3項目が文字一致する最新のメモを返す。"""
    cfg = config.load_config()["memo"]
    tags = cfg["match_tags"]
    cutoff = ((now or now_jst()) - timedelta(days=cfg["match_days"])).isoformat(timespec="seconds")
    return conn.execute(
        f"""SELECT * FROM memos
            WHERE symbol = ? AND timeframe = ? AND entry_kind = ? AND created_at >= ?
              AND tag IN ({','.join('?' * len(tags))})
            ORDER BY created_at DESC LIMIT 1""",
        (_norm(symbol), _norm(timeframe), _norm(entry_kind), cutoff, *tags),
    ).fetchone()


def register_idea(conn: sqlite3.Connection, idea: Idea) -> tuple[str | None, str | None]:
    """照合してから案を登録する(SPEC 5.1 の5・6)。

    戻り値: (案ID, 除外した時の否決メモID)。除外した時は案IDが None。
    """
    hit = find_match(conn, idea.symbol, idea.timeframe, idea.entry_kind)
    if hit is not None:
        memo_id = add_memo(conn, Memo(
            stage="探索", tag="重複",
            reason=f"{hit['id']}({hit['tag']})と銘柄・時間・入り方が一致"[:REASON_MAX],
            idea_summary=idea.summary, symbol=idea.symbol, timeframe=idea.timeframe,
            entry_kind=idea.entry_kind, source=idea.source,
        ))
        return None, memo_id
    return db.insert_idea(conn, idea), None


def design_notes(conn: sqlite3.Connection, symbol: str, source: str) -> list[sqlite3.Row]:
    """設計時の注意書き(SPEC 6.3 用途2): 同じ銘柄・同じ出どころのメモを最大5件。"""
    return conn.execute(
        """SELECT * FROM memos WHERE symbol = ? AND source = ? AND tag != ?
           ORDER BY created_at DESC LIMIT ?""",
        (_norm(symbol), source, PASS_TAG, DESIGN_NOTES_MAX),
    ).fetchall()


def tag_counts(conn: sqlite3.Connection, days: int = 7, *, now: datetime | None = None
               ) -> list[sqlite3.Row]:
    """直近N日の札の集計(SPEC 6.3 用途3・ダッシュボード枠5)。"""
    cutoff = ((now or now_jst()) - timedelta(days=days)).isoformat(timespec="seconds")
    return conn.execute(
        """SELECT tag, COUNT(*) AS n FROM memos WHERE created_at >= ?
           GROUP BY tag ORDER BY n DESC, tag""",
        (cutoff,),
    ).fetchall()


def weekly_metrics(conn: sqlite3.Connection, weeks: int = 8, *, now: datetime | None = None
                   ) -> list[dict]:
    """週次3指標(SPEC 6.4)。週は月曜始まり。新しい週が先頭。

    合格率は、その週に登録された案のうち反証を通過した(関所1以降に進んだ)数 ÷ 登録数。
    """
    now = now or now_jst()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for i in range(weeks):
        start = monday - timedelta(weeks=i)
        end = start + timedelta(weeks=1)
        s, e = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
        excluded = conn.execute(
            "SELECT COUNT(*) FROM memos WHERE tag = '重複' AND created_at >= ? AND created_at < ?",
            (s, e)).fetchone()[0]
        registered = conn.execute(
            "SELECT COUNT(*) FROM ideas WHERE created_at >= ? AND created_at < ?", (s, e)).fetchone()[0]
        passed = conn.execute(
            """SELECT COUNT(*) FROM ideas WHERE created_at >= ? AND created_at < ?
               AND (stage = '本番' OR (stage = '反証' AND verdict = '完了')
                    OR id IN (SELECT idea_id FROM memos WHERE tag = ?))""",
            (s, e, PASS_TAG)).fetchone()[0]
        rows.append({
            "week": start.date().isoformat(), "excluded": excluded, "registered": registered,
            "passed": passed, "pass_rate": (passed / registered) if registered else None,
        })
    return rows
