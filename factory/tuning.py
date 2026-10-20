"""週1回の棚卸し(SPEC 6.3 用途3)。

直近7日の札を数え、最多の札に応じて調査の絞り込みを1段きつくする。翌週に最多の札が変わったら元に戻す。
変更は tuning_log に残す。最新の1行が今の設定(きつくしていない週は setting が空)。
「重複」(照合で除外できた印)と「合格」は工程の弱点ではないので、最多の札を選ぶ時に数えない。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import config, memo
from .models import now_jst

SKIP_TAGS = ("重複", "合格")


def current(conn: sqlite3.Connection) -> dict:
    """調査で使う今の値(fee_multiple / min_occurrences)。"""
    cfg = config.load_config()["investigate"]
    out = {"fee_multiple": cfg["fee_multiple"], "min_occurrences": cfg["min_occurrences"]}
    row = conn.execute("SELECT setting, value FROM tuning_log ORDER BY id DESC LIMIT 1").fetchone()
    if row is not None and row["setting"] in out:
        out[row["setting"]] = row["value"]
    return out


def week_start(now: datetime) -> datetime:
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def weekly(conn: sqlite3.Connection, *, now: datetime | None = None) -> str | None:
    """今週(月曜始まり)の棚卸しがまだなら行い、ログの文を返す。済んでいれば None。"""
    now = now or now_jst()
    monday = week_start(now).isoformat(timespec="seconds")
    if conn.execute("SELECT 1 FROM tuning_log WHERE created_at >= ?", (monday,)).fetchone():
        return None
    counts = [r for r in memo.tag_counts(conn, 7, now=now) if r["tag"] not in SKIP_TAGS]
    top = counts[0]["tag"] if counts else None
    rule = config.load_config().get("tuning", {}).get(top) if top else None
    before = current(conn)
    if rule:
        setting, value = rule["setting"], float(rule["value"])
        note = f"最多の札は{top}({counts[0]['n']}件)。{setting} を {before[setting]:g} → {value:g} にした"
    else:
        setting, value = None, None
        changed = [k for k, v in before.items() if v != config.load_config()["investigate"][k]]
        head = f"最多の札は{top}({counts[0]['n']}件)" if top else "直近7日の札なし"
        note = head + ("。きつくした設定を元に戻した" if changed else "。設定は変えない")
    conn.execute("INSERT INTO tuning_log(created_at, top_tag, setting, value, note) VALUES (?,?,?,?,?)",
                 (now.isoformat(timespec="seconds"), top, setting, value, note))
    conn.commit()
    return note
