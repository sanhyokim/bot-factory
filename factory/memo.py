"""否決メモ: 登録・照合・集計・まとめ(SPEC 6章)。

照合はAIを使わず、「型+銘柄+入る条件の種類+数字(n・x)」の文字一致で行う(SPEC 6.3 用途1)。
集計と照合は型ごとに分ける。まとめた(merged_into のある)メモは、照合・注意書き・一覧には出さない。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import config, db
from .models import MEMO_STAGES, PASS_TAG, TAGS, TYPES, Idea, Memo, match_key, now_jst

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
    if memo.type not in TYPES:
        raise ValueError(f"型が不正: {memo.type}")
    memo_id = db.next_id(conn, "memo")
    ts = (at or now_jst()).isoformat(timespec="seconds")
    key = memo.match_key or match_key(memo.type, memo.symbol, memo.entry_kind, None)
    conn.execute(
        """INSERT INTO memos(id, created_at, stage, tag, reason, idea_summary, idea_id,
               symbol, timeframe, entry_kind, source, type, match_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (memo_id, ts, memo.stage, memo.tag, reason, memo.idea_summary, memo.idea_id,
         _norm(memo.symbol), _norm(memo.timeframe), _norm(memo.entry_kind), memo.source, memo.type, key),
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
        source=idea["source"], type=idea["type"],
        match_key=match_key(idea["type"], idea["symbol"], idea["entry_kind"], idea["entry_params"]),
    ))
    if tag != PASS_TAG:
        db.set_idea_state(conn, idea_id, "否決", "見送り")
    return memo_id


def find_match(conn: sqlite3.Connection, key: str, *, now: datetime | None = None) -> sqlite3.Row | None:
    """登録前照合。過去90日・対象の札で、照合キー(型・銘柄・入る条件の種類・数字)が文字一致する最新のメモ。

    キーに型が入っているので、同じ型のメモとだけ照合する(SPEC 6.3 用途1)。
    """
    cfg = config.load_config()["memo"]
    tags = cfg["match_tags"]
    cutoff = ((now or now_jst()) - timedelta(days=cfg["match_days"])).isoformat(timespec="seconds")
    return conn.execute(
        f"""SELECT * FROM memos
            WHERE match_key = ? AND created_at >= ? AND merged_into IS NULL
              AND tag IN ({','.join('?' * len(tags))})
            ORDER BY created_at DESC LIMIT 1""",
        (key, cutoff, *tags),
    ).fetchone()


def register_idea(conn: sqlite3.Connection, idea: Idea) -> tuple[str | None, str | None]:
    """照合してから案を登録する(SPEC 5.1 の6・7、入口2の4・5)。

    戻り値: (案ID, 除外した時の否決メモID)。除外した時は案IDが None。
    """
    key = match_key(idea.type, idea.symbol, idea.entry_kind, idea.entry_params)
    hit = find_match(conn, key)
    if hit is not None:
        memo_id = add_memo(conn, Memo(
            stage="探索", tag="重複",
            reason=f"{hit['id']}({hit['tag']})と型・銘柄・入り方・数字が一致"[:REASON_MAX],
            idea_summary=idea.summary, symbol=idea.symbol, timeframe=idea.timeframe,
            entry_kind=idea.entry_kind, source=idea.source, type=idea.type, match_key=key,
        ))
        return None, memo_id
    return db.insert_idea(conn, idea), None


def design_notes(conn: sqlite3.Connection, type_: str, symbol: str, source: str) -> list[sqlite3.Row]:
    """設計時の注意書き(SPEC 6.3 用途2): 同じ型・同じ銘柄・同じ出どころのメモを最大5件。"""
    return conn.execute(
        """SELECT * FROM memos WHERE type = ? AND symbol = ? AND source = ? AND tag != ? AND merged_into IS NULL
           ORDER BY created_at DESC LIMIT ?""",
        (type_, _norm(symbol), source, PASS_TAG, DESIGN_NOTES_MAX),
    ).fetchall()


def record_uses(conn: sqlite3.Connection, memo_ids: list[str], idea_id: str | None) -> None:
    """設計時の注意書きでAIに渡したメモを残す(メモが使われているかの測定)。"""
    ts = now_jst().isoformat(timespec="seconds")
    conn.executemany("INSERT INTO memo_uses(created_at, memo_id, idea_id) VALUES (?,?,?)",
                     [(ts, m, idea_id) for m in memo_ids])
    conn.commit()


def record_investigation(conn: sqlite3.Connection, idea_id: str, type_: str, verdict: str,
                         tag: str | None) -> None:
    """調査の結果(継続/見送り)を残す。保留は結果が出ていないので残さない。"""
    if verdict in ("継続", "見送り"):
        conn.execute("INSERT INTO investigate_results(created_at, idea_id, type, result, tag) VALUES (?,?,?,?,?)",
                     (now_jst().isoformat(timespec="seconds"), idea_id, type_, verdict, tag))
        conn.commit()


def unused_memos(conn: sqlite3.Connection, *, type_: str | None = None) -> dict:
    """注意書きで一度もAIに渡されていないメモの数。照合で除外した記録(重複)と合格の記録は数えない。"""
    where, args = (" AND type = ?", [type_]) if type_ else ("", [])
    base = "FROM memos WHERE merged_into IS NULL AND tag NOT IN ('重複', ?)" + where
    total = conn.execute("SELECT COUNT(*) " + base, (PASS_TAG, *args)).fetchone()[0]
    unused = conn.execute("SELECT COUNT(*) " + base + " AND id NOT IN (SELECT memo_id FROM memo_uses)",
                          (PASS_TAG, *args)).fetchone()[0]
    return {"total": total, "unused": unused}


def tag_counts(conn: sqlite3.Connection, days: int = 7, *, type_: str | None = None,
               now: datetime | None = None) -> list[sqlite3.Row]:
    """直近N日の札の集計(SPEC 6.3 用途3・ダッシュボード枠5)。型を渡すとその型だけ。

    まとめたメモも1件ずつ数える(落ちた回数そのものは減らさない)。
    """
    cutoff = ((now or now_jst()) - timedelta(days=days)).isoformat(timespec="seconds")
    where, args = ("AND type = ?", [type_]) if type_ else ("", [])
    return conn.execute(
        f"""SELECT tag, COUNT(*) AS n FROM memos WHERE created_at >= ? {where}
            GROUP BY tag ORDER BY n DESC, tag""",
        (cutoff, *args),
    ).fetchall()


def weekly_metrics(conn: sqlite3.Connection, weeks: int = 8, *, type_: str | None = None,
                   now: datetime | None = None) -> list[dict]:
    """週次3指標(SPEC 6.4)と、著者の記事(2026-10-24)から足した2つ。週は月曜始まり。新しい週が先頭。
    型を渡すとその型だけ。

    合格率は、その週に登録された案のうち反証を通過した(関所1以降に進んだ)数 ÷ 登録数。
    見送り率は、その週に調査の結果が出た案のうち見送った数 ÷ 結果が出た数。外のデータが取れなかった・
    取引所にない(札 未対応)ものは案の良し悪しではないので、分子にも分母にも入れない。
    渡したメモは、その週に設計時の注意書きでAIに渡したメモの延べ数。
    """
    now = now or now_jst()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    where, args = (" AND type = ?", [type_]) if type_ else ("", [])
    rows = []
    for i in range(weeks):
        start = monday - timedelta(weeks=i)
        end = start + timedelta(weeks=1)
        s, e = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
        excluded = conn.execute(
            "SELECT COUNT(*) FROM memos WHERE tag = '重複' AND created_at >= ? AND created_at < ?" + where,
            (s, e, *args)).fetchone()[0]
        registered = conn.execute(
            "SELECT COUNT(*) FROM ideas WHERE created_at >= ? AND created_at < ?" + where,
            (s, e, *args)).fetchone()[0]
        passed = conn.execute(
            """SELECT COUNT(*) FROM ideas WHERE created_at >= ? AND created_at < ?
               AND (stage = '本番' OR (stage = '反証' AND verdict = '完了')
                    OR id IN (SELECT idea_id FROM memos WHERE tag = ?))""" + where,
            (s, e, PASS_TAG, *args)).fetchone()[0]
        res = dict(conn.execute(
            """SELECT result, COUNT(*) FROM investigate_results WHERE created_at >= ? AND created_at < ?
               AND COALESCE(tag, '') != '未対応'""" + where + " GROUP BY result", (s, e, *args)).fetchall())
        judged = res.get("継続", 0) + res.get("見送り", 0)
        uses = conn.execute(
            """SELECT COUNT(*) FROM memo_uses u JOIN memos m ON m.id = u.memo_id
               WHERE u.created_at >= ? AND u.created_at < ?""" + where.replace("type", "m.type"),
            (s, e, *args)).fetchone()[0]
        rows.append({
            "week": start.date().isoformat(), "excluded": excluded, "registered": registered,
            "passed": passed, "pass_rate": (passed / registered) if registered else None,
            "investigated": judged, "skipped": res.get("見送り", 0),
            "skip_rate": (res.get("見送り", 0) / judged) if judged else None, "notes_passed": uses,
        })
    return rows


def consolidate(conn: sqlite3.Connection) -> tuple[int, int]:
    """まとめ(SPEC 6.3 用途3): 案の要約が文字一致するメモを1件にまとめ、まとめ件数に元の数を入れる。

    同じ型・同じ札のものだけをまとめる(札は主因1つなので、札が違えば別の落ち方として残す)。
    残すのは一番新しいメモ。まとめた側は消さずに merged_into で残す(札の集計は落ちた回数のまま)。
    戻り値: (まとめたメモの数, まとめ先の数)。
    """
    groups: dict[tuple, list[sqlite3.Row]] = {}
    for m in conn.execute("SELECT * FROM memos WHERE merged_into IS NULL AND tag != ? ORDER BY created_at, id",
                          (PASS_TAG,)):
        groups.setdefault((m["type"], m["tag"], _norm(m["idea_summary"])), []).append(m)
    merged = targets = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        keep, rest = rows[-1], rows[:-1]
        total = sum(r["merge_count"] for r in rows)
        conn.execute("UPDATE memos SET merge_count = ? WHERE id = ?", (total, keep["id"]))
        conn.executemany("UPDATE memos SET merged_into = ? WHERE id = ?", [(keep["id"], r["id"]) for r in rest])
        merged += len(rest)
        targets += 1
    conn.commit()
    return merged, targets
