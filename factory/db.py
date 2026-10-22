"""データベース(SQLite: data/factory.db)の読み書き。SPEC 4章の帳簿をすべてここに置く。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config
from .models import SOURCES, STAGES, TYPES, VERDICTS, Idea, match_key, now_jst

SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
    id            TEXT PRIMARY KEY,          -- I-00041
    created_at    TEXT NOT NULL,             -- 日本時間 ISO8601
    source        TEXT NOT NULL,             -- wallet / funding
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

-- 段階3: 探索で分析した財布(7日間は再分析しない)
CREATE TABLE IF NOT EXISTS wallet_scans (
    address    TEXT PRIMARY KEY,
    scanned_at TEXT NOT NULL
);

-- 段階3: AIの一括処理(半額)。結果は次の実行でも受け取れる
CREATE TABLE IF NOT EXISTS ai_batches (
    id         TEXT PRIMARY KEY,
    stage      TEXT NOT NULL,
    purpose    TEXT NOT NULL,
    model      TEXT NOT NULL,
    backend    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL,           -- 処理中 / 受取済み
    items_json TEXT NOT NULL,           -- custom_id ごとの付帯情報
    results_json TEXT                   -- api 以外の接続先で、その場で得た結果
);

-- 段階3: 価格と資金調達料の保管(取引所は直近5000本までしか返さないため)
CREATE TABLE IF NOT EXISTS candles (
    symbol   TEXT NOT NULL,
    interval TEXT NOT NULL,
    t        INTEGER NOT NULL,
    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL, v REAL NOT NULL,
    PRIMARY KEY (symbol, interval, t)
);
CREATE TABLE IF NOT EXISTS funding (
    symbol TEXT NOT NULL,
    t      INTEGER NOT NULL,
    rate   REAL NOT NULL,
    PRIMARY KEY (symbol, t)
);

-- 段階3: 週1回の棚卸しで変えた絞り込み(SPEC 6.3 用途3)
CREATE TABLE IF NOT EXISTS tuning_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    top_tag    TEXT,
    setting    TEXT,
    value      REAL,
    note       TEXT NOT NULL
);

-- 段階3b: 見張り役の記録(SPEC 3.3。否決メモではなくログと画面に残す)
CREATE TABLE IF NOT EXISTS watchdog_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    process    TEXT NOT NULL,
    event      TEXT NOT NULL,          -- 起動 / 再起動 / メモリ超過 / 停止 / 再起動をやめた
    detail     TEXT NOT NULL DEFAULT ''
);

-- 段階3b: データ窓口(取引所)に断られた回数(SPEC 8章 枠6)
CREATE TABLE IF NOT EXISTS exchange_refusals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    process    TEXT NOT NULL,
    count      INTEGER NOT NULL
);
"""

# 既にあるデータベースに後から足した列(段階3)
ADDED_COLUMNS = {
    "ideas": [("side", "TEXT"), ("entry_params", "TEXT"), ("take_profit_pct", "REAL"),
              ("stop_loss_pct", "REAL"), ("hold_count", "INTEGER NOT NULL DEFAULT 0"),
              ("type", "TEXT NOT NULL DEFAULT '値動き'")],                       # 段階3b: 型(SPEC 4.1)
    "rules": [("take_profit_pct", "REAL"), ("stop_loss_pct", "REAL"),
              # 段階3b(SPEC 4.2): 型、最長保有、歪み型の x・y、継続性・競争の札、1回に要る最小の資金
              ("type", "TEXT NOT NULL DEFAULT '値動き'"), ("max_hold_hours", "REAL"), ("funding_x", "REAL"),
              ("funding_y", "REAL"), ("continuity", "TEXT"), ("competition", "TEXT"), ("min_capital_usd", "REAL")],
    "bots": [("paper_start_ms", "INTEGER"), ("refute_note", "TEXT"), ("last_error", "TEXT"),
             # 段階3b(SPEC 4.3): 土台の版(0 は自己完結になる前のBOT)、作ったAI、継続性・競争の札
             ("base_version", "INTEGER NOT NULL DEFAULT 0"), ("made_by", "TEXT"), ("continuity", "TEXT"),
             ("competition", "TEXT")],
    "trades": [("slippage_usd", "REAL NOT NULL DEFAULT 0")],                   # 段階3b: 滑り(SPEC 4.4)
    "memos": [("type", "TEXT NOT NULL DEFAULT '値動き'"), ("match_key", "TEXT"),   # 段階3b: 型・照合キー
              ("merge_count", "INTEGER NOT NULL DEFAULT 1"), ("merged_into", "TEXT")],  # まとめ(SPEC 6.1・6.3)
    "tuning_log": [("type", "TEXT NOT NULL DEFAULT '値動き'")],
    "wallet_scans": [("method", "INTEGER NOT NULL DEFAULT 1")],   # 絞り込みの数え方の版(探索の SCAN_METHOD)
}

ID_FORMATS = {"idea": ("I-", 5), "memo": ("M-", 5), "bot": ("B-", 4)}


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)   # 見張り役の下で別のプロセス(価格の受け取り口など)も書くので長めに待つ
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # 段階3bより前のメモに照合キーを付ける(案があれば案の数字から)
    for m in conn.execute(
            """SELECT m.id, m.type, m.symbol, m.entry_kind, i.type AS i_type, i.symbol AS i_symbol,
                      i.entry_kind AS i_kind, i.entry_params FROM memos m LEFT JOIN ideas i ON i.id = m.idea_id
               WHERE m.match_key IS NULL""").fetchall():
        key = (match_key(m["i_type"], m["i_symbol"], m["i_kind"], m["entry_params"]) if m["i_type"]
               else match_key(m["type"], m["symbol"], m["entry_kind"], None))
        conn.execute("UPDATE memos SET match_key = ? WHERE id = ?", (key, m["id"]))
    # 段階3bより前のBOTの「作ったAI」は、AI費用帳簿の実装の行から写す
    conn.execute("""UPDATE bots SET made_by = (SELECT model FROM ai_costs a WHERE a.bot_id = bots.id
                                               AND a.stage = 'implement' ORDER BY a.id LIMIT 1)
                    WHERE made_by IS NULL""")
    conn.commit()


def next_id(conn: sqlite3.Connection, kind: str) -> str:
    prefix, width = ID_FORMATS[kind]
    conn.execute("INSERT OR IGNORE INTO counters(name, value) VALUES (?, 0)", (kind,))
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (kind,))
    value = conn.execute("SELECT value FROM counters WHERE name = ?", (kind,)).fetchone()[0]
    return f"{prefix}{value:0{width}d}"


def insert_idea(conn: sqlite3.Connection, idea: Idea, match_memo_id: str | None = None) -> str:
    if idea.source not in SOURCES:
        raise ValueError(f"出どころが不正: {idea.source}")
    if idea.type not in TYPES:
        raise ValueError(f"型が不正: {idea.type}")
    if idea.stage not in STAGES or idea.verdict not in VERDICTS:
        raise ValueError(f"工程/判定が不正: {idea.stage}/{idea.verdict}")
    idea_id = next_id(conn, "idea")
    ts = now_jst().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO ideas(id, created_at, source, wallet, summary, hypothesis, symbol, timeframe,
               entry_kind, stage, verdict, match_found, match_memo_id, updated_at,
               side, entry_params, take_profit_pct, stop_loss_pct, type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (idea_id, ts, idea.source, idea.wallet, idea.summary, idea.hypothesis[:300], idea.symbol,
         idea.timeframe, idea.entry_kind, idea.stage, idea.verdict, int(match_memo_id is not None),
         match_memo_id, ts, idea.side, idea.entry_params, idea.take_profit_pct, idea.stop_loss_pct, idea.type),
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


def interleave_types(rows: list, limit: int) -> list:
    """型ごとに古い順に並べ、型を交互に取る(待ちの多い値動き型の後ろで、歪み型が何日も待たないように)。"""
    queues = {t: [r for r in rows if r["type"] == t] for t in reversed(TYPES)}
    out: list = []
    while len(out) < limit and any(queues.values()):
        for q in queues.values():
            if q and len(out) < limit:
                out.append(q.pop(0))
    return out


def record_refusals(conn: sqlite3.Connection, process: str, count: int) -> None:
    """データ窓口(取引所)に断られた回数を残す(SPEC 8章 枠6)。0回なら残さない。"""
    if count > 0:
        conn.execute("INSERT INTO exchange_refusals(created_at, process, count) VALUES (?,?,?)",
                     (now_jst().isoformat(timespec="seconds"), process, count))
        conn.commit()


# 画面の絞り込み(SPEC 8章)のうち、BOTの札で絞るもの: 画面の名前 → bots の列
BOT_FILTERS = {"continuity": "continuity", "competition": "competition", "made_by": "made_by"}


def list_ideas(conn: sqlite3.Connection, stage: str | None = None, verdict: str | None = None,
               limit: int = 200, *, type_: str | None = None, **bot_filters: str | None) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM ideas WHERE 1=1", []
    if stage:
        sql += " AND stage = ?"
        args.append(stage)
    if verdict:
        sql += " AND verdict = ?"
        args.append(verdict)
    if type_:
        sql += " AND type = ?"
        args.append(type_)
    for name, value in bot_filters.items():
        if value:
            sql += f" AND id IN (SELECT rule_id FROM bots WHERE {BOT_FILTERS[name]} = ?)"
            args.append(value)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def stage_verdict_counts(conn: sqlite3.Connection, type_: str | None = None) -> list[sqlite3.Row]:
    """工程/判定ごとの件数(ダッシュボード枠2)。型で絞れる。"""
    where, args = ("WHERE type = ?", [type_]) if type_ else ("", [])
    return conn.execute(
        f"""SELECT stage, verdict, COUNT(*) AS n FROM ideas {where}
            GROUP BY stage, verdict ORDER BY n DESC, stage, verdict""", args
    ).fetchall()
