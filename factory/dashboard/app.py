"""ダッシュボード(SPEC 8章)。http://localhost:8800 で開く。

段階1で作る枠: 1 要判断 / 2 工程別の集計 / 5 否決メモ。残りの枠は該当の段階で足す。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import config, db, memo
from ..models import GATE_STATES, STAGES, TAGS, VERDICTS, now_jst

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="bot-factory")


def _gate_rows(conn):
    marks = ",".join("?" * len(GATE_STATES))
    return conn.execute(
        f"""SELECT b.*, r.symbol, r.timeframe FROM bots b JOIN rules r ON r.id = b.rule_id
            WHERE b.status IN ({marks}) ORDER BY b.updated_at""",
        list(GATE_STATES)).fetchall()


def _costs(conn):
    now = now_jst()
    today, month = now.date().isoformat(), now.strftime("%Y-%m")
    q = "SELECT COALESCE(SUM(cost_usd),0) FROM ai_costs WHERE substr(created_at,1,{n}) = ?"
    return {
        "today": conn.execute(q.format(n=10), (today,)).fetchone()[0],
        "month": conn.execute(q.format(n=7), (month,)).fetchone()[0],
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request, stage: str | None = None, verdict: str | None = None,
          tag: str | None = None):
    conn = db.connect()
    try:
        counts = db.stage_verdict_counts(conn)
        max_n = max((r["n"] for r in counts), default=1)
        memo_sql, memo_args = "SELECT * FROM memos", []
        if tag:
            memo_sql += " WHERE tag = ?"
            memo_args.append(tag)
        ctx = {
            "gates": _gate_rows(conn),
            "gate_labels": GATE_STATES,
            "counts": counts,
            "max_n": max_n,
            "ideas": db.list_ideas(conn, stage or None, verdict or None, limit=100),
            "tag_counts": memo.tag_counts(conn, 7),
            "weekly": memo.weekly_metrics(conn, 8),
            "memos": conn.execute(memo_sql + " ORDER BY id DESC LIMIT 50", memo_args).fetchall(),
            "costs": _costs(conn),
            "filters": {"stage": stage or "", "verdict": verdict or "", "tag": tag or ""},
            "stages": STAGES, "verdicts": VERDICTS, "tags": TAGS + ["合格"],
            "refresh": config.load_config()["dashboard"]["refresh_seconds"],
            "now": now_jst().strftime("%Y-%m-%d %H:%M:%S"),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/ideas/{idea_id}", response_class=HTMLResponse)
def idea_detail(request: Request, idea_id: str):
    conn = db.connect()
    try:
        idea = db.get_idea(conn, idea_id)
        if idea is None:
            raise HTTPException(404, "案が見つからない")
        ctx = {
            "idea": idea,
            "rule": conn.execute("SELECT * FROM rules WHERE id = ?", (idea_id,)).fetchone(),
            "memos": conn.execute("SELECT * FROM memos WHERE idea_id = ? ORDER BY id", (idea_id,)).fetchall(),
            "costs": conn.execute("SELECT * FROM ai_costs WHERE idea_id = ? ORDER BY id", (idea_id,)).fetchall(),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "idea.html", ctx)


def serve() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=config.load_config()["dashboard"]["port"])
