"""週1回の棚卸し(SPEC 6.3 用途3)。型(値動き/歪み)ごとに行う。

直近7日の札を型ごとに数え、最多の札に応じて探索・調査の絞り込みを1段きつくする。翌週に最多の札が変わったら元に戻す。
変更は tuning_log に型ごとに残す。型ごとの最新の1行が今の設定(きつくしていない週は setting が空)。
「重複」(照合で除外できた印)と「合格」は工程の弱点ではないので、最多の札を選ぶ時に数えない。
同じ週の棚卸しで、案の要約が文字一致する否決メモをまとめる(memo.consolidate)。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import config, memo
from .models import DISTORTION, PRICE, TYPES, now_jst

SKIP_TAGS = ("重複", "合格")


def defaults(type_: str) -> dict:
    """型ごとの、きつくしていない時の値。値動き型は調査、歪み型は探索の入口2と調査の数字。"""
    cfg = config.load_config()
    if type_ == DISTORTION:
        return {"min_hours": cfg["explore_funding"]["min_hours"],
                "funding_fee_multiple": cfg["investigate"]["funding_fee_multiple"]}
    inv = cfg["investigate"]
    return {"fee_multiple": inv["fee_multiple"], "min_occurrences": inv["min_occurrences"]}


def current(conn: sqlite3.Connection, type_: str = PRICE) -> dict:
    """その型の今の値。"""
    out = defaults(type_)
    row = conn.execute("SELECT setting, value FROM tuning_log WHERE type = ? ORDER BY id DESC LIMIT 1",
                       (type_,)).fetchone()
    if row is not None and row["setting"] in out:
        out[row["setting"]] = row["value"]
    return out


def week_start(now: datetime) -> datetime:
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def _one_type(conn: sqlite3.Connection, type_: str, now: datetime) -> str:
    counts = [r for r in memo.tag_counts(conn, 7, type_=type_, now=now) if r["tag"] not in SKIP_TAGS]
    top = counts[0]["tag"] if counts else None
    rule = config.load_config().get("tuning", {}).get(type_, {}).get(top) if top else None
    before = current(conn, type_)
    if rule:
        setting, value = rule["setting"], float(rule["value"])
        note = f"最多の札は{top}({counts[0]['n']}件)。{setting} を {before[setting]:g} → {value:g} にした"
    else:
        setting, value = None, None
        changed = [k for k, v in before.items() if v != defaults(type_)[k]]
        head = f"最多の札は{top}({counts[0]['n']}件)" if top else "直近7日の札なし"
        note = head + ("。きつくした設定を元に戻した" if changed else "。設定は変えない")
    conn.execute("INSERT INTO tuning_log(created_at, top_tag, setting, value, note, type) VALUES (?,?,?,?,?,?)",
                 (now.isoformat(timespec="seconds"), top, setting, value, note, type_))
    conn.commit()
    return f"{type_}型: {note}"


def weekly(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[str]:
    """今週(月曜始まり)の棚卸しがまだの型について行い、ログの文を返す。全部済んでいれば空。"""
    now = now or now_jst()
    monday = week_start(now).isoformat(timespec="seconds")
    notes = []
    for type_ in TYPES:
        if conn.execute("SELECT 1 FROM tuning_log WHERE created_at >= ? AND type = ?", (monday, type_)).fetchone():
            continue
        notes.append(_one_type(conn, type_, now))
    if notes:
        merged, targets = memo.consolidate(conn)
        if merged:
            notes.append(f"メモのまとめ: 案の要約が同じ{merged + targets}件を{targets}件にまとめた")
    return notes
