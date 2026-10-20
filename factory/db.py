"""データベース(SQLite: data/factory.db)の読み書き。SPEC 4章の帳簿をすべてここに置く。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config
from .models import SOURCES, STAGES, VERDICTS, Idea, now_jst

SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
    id            TEXT PRIMARY KEY,          -- I-00041
    created_at    TEXT NOT NULL,             -- 日本時間 ISO8601
    source        TEXT NOT NULL,             -- wallet / market
    wallet        TEXT,
    summary       TEXT NOT NULL,
    hypothesis    TEXT NOT NULL DEFAULT '',
    symbol        TEXT NOT NULL DEFAULT '',
    timeframe     TEXT NOT NULL DEFAULT '',
    entry_kind    TEXT NOT NULL DEFAULT '',
    stage         TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    match_found   INTEGER NOT NULL DEFAULT 0,  -- 照合結果 あり/なし
    match_memo_id TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id               TEXT PRIMARY KEY,       -- 案IDに対応
    symbol           TEXT NOT NULL,
    timeframe        TEXT NOT NULL,
    entry_condition  TEXT NOT NULL,
    exit_condition   TEXT NOT NULL,
    max_amount_usd   REAL NOT NULL,
    max_positions    INTEGER NOT NULL DEFAULT 1 CHECK (max_positions = 1),
    leverage         REAL NOT NULL DEFAULT 1 CHECK (leverage = 1),
    max_trades_per_day INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bots (
    id         TEXT PRIMARY KEY,             -- B-0031
    rule_id    TEXT NOT NULL REFERENCES rules(id),
    code_path  TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0 CHECK (attempts <= 3),
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id       TEXT NOT NULL REFERENCES bots(id),
    kind         TEXT NOT NULL CHECK (kind IN ('paper', 'live')),
    entry_at     TEXT NOT NULL,
    entry_price  REAL NOT NULL,
    entry_qty    REAL NOT NULL,
    exit_at      TEXT,
    exit_price   REAL,
    exit_qty     REAL,
    fee_usd      REAL NOT NULL DEFAULT 0,
    funding_usd  REAL NOT NULL DEFAULT 0,
    net_pnl_usd  REAL
);

CREATE TABLE IF NOT EXISTS memos (
    id           TEXT PRIMARY KEY,           -- M-00123
    created_at   TEXT NOT NULL,              -- 落ちた日(日本時間)
    stage        TEXT NOT NULL,
    tag          TEXT NOT NULL,
    reason       TEXT NOT NULL,              -- 80字以内
    idea_summary TEXT NOT NULL,
    idea_id      TEXT,
    -- 照合・集計用のキー(SPEC 6.3)
    symbol       TEXT NOT NULL DEFAULT '',
    timeframe    TEXT NOT NULL DEFAULT '',
    entry_kind   TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS memos_key ON memos(symbol, timeframe, entry_kind);

CREATE TABLE IF NOT EXISTS ai_costs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    stage         TEXT NOT NULL,
    idea_id       TEXT,
    bot_id        TEXT,
    model         TEXT NOT NULL,             -- haiku / sonnet / opus
    backend       TEXT NOT NULL,             -- api / claude_code / mock
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    batch         INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

ID_FORMATS = {"idea": ("I-", 5), "memo": ("M-", 5), "bot": ("B-", 4)}


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def next_id(conn: sqlite3.Connection, kind: str) -> str:
    prefix, width = ID_FORMATS[kind]
    conn.execute("INSERT OR IGNORE INTO counters(name, value) VALUES (?, 0)", (kind,))
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (kind,))
    value = conn.execute("SELECT value FROM counters WHERE name = ?", (kind,)).fetchone()[0]
    return f"{prefix}{value:0{width}d}"


def insert_idea(conn: sqlite3.Connection, idea: Idea, match_memo_id: str | None = None) -> str:
    if idea.source not in SOURCES:
        raise ValueError(f"出どころが不正: {idea.source}")
    if idea.stage not in STAGES or idea.verdict not in VERDICTS:
        raise ValueError(f"工程/判定が不正: {idea.stage}/{idea.verdict}")
    idea_id = next_id(conn, "idea")
    ts = now_jst().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO ideas(id, created_at, source, wallet, summary, hypothesis, symbol, timeframe,
               entry_kind, stage, verdict, match_found, match_memo_id, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (idea_id, ts, idea.source, idea.wallet, idea.summary, idea.hypothesis[:300], idea.symbol,
         idea.timeframe, idea.entry_kind, idea.stage, idea.verdict, int(match_memo_id is not None),
         match_memo_id, ts),
    )
    conn.commit()
    return idea_id


def set_idea_state(conn: sqlite3.Connection, idea_id: str, stage: str, verdict: str) -> None:
    if stage not in STAGES or verdict not in VERDICTS:
        raise ValueError(f"工程/判定が不正: {stage}/{verdict}")
    conn.execute(
        "UPDATE ideas SET stage = ?, verdict = ?, updated_at = ? WHERE id = ?",
        (stage, verdict, now_jst().isoformat(timespec="seconds"), idea_id),
    )
    conn.commit()


def get_idea(conn: sqlite3.Connection, idea_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()


def list_ideas(conn: sqlite3.Connection, stage: str | None = None, verdict: str | None = None,
               limit: int = 200) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM ideas WHERE 1=1", []
    if stage:
        sql += " AND stage = ?"
        args.append(stage)
    if verdict:
        sql += " AND verdict = ?"
        args.append(verdict)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def stage_verdict_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """工程/判定ごとの件数(ダッシュボード枠2)。"""
    return conn.execute(
        """SELECT stage, verdict, COUNT(*) AS n FROM ideas
           GROUP BY stage, verdict ORDER BY n DESC, stage, verdict"""
    ).fetchall()
