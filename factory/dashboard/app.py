"""ダッシュボード(SPEC 8章)。http://localhost:8800 で開く。

段階1で作った枠: 1 要判断 / 2 工程別の集計 / 5 否決メモ。段階3で 4 paper評価中 / 6 費用 を足した。
枠3(本番運用中)は段階4で足す。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import config, db, memo
from ..hyperliquid.reader import now_ms
from ..models import GATE_STATES, STAGES, TAGS, VERDICTS, now_jst

DAY_MS = 86_400_000
# AI費用帳簿の工程名(config.yaml の書き方)を画面の言葉に直す
STAGE_LABELS = {"explore": "探索", "investigate": "調査", "design": "新ロジック開発", "implement": "実装",
                "repair": "修理", "refute": "反証", "manual": "手動の確認"}

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="bot-factory")


def _gate_rows(conn):
    marks = ",".join("?" * len(GATE_STATES))
    return conn.execute(
        f"""SELECT b.*, r.symbol, r.timeframe FROM bots b JOIN rules r ON r.id = b.rule_id
            WHERE b.status IN ({marks}) ORDER BY b.updated_at""",
        list(GATE_STATES)).fetchall()


def _costs(conn):
    """枠6: 今日・今月のAI代、案・BOTごとの上位10件、クレジット残高の見込み(SPEC 7.3)。"""
    now = now_jst()
    today, month = now.date().isoformat(), now.strftime("%Y-%m")
    q = "SELECT COALESCE(SUM(cost_usd),0) FROM ai_costs WHERE substr(created_at,1,{n}) = ?"
    spent_api = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM ai_costs WHERE backend = 'api'").fetchone()[0]
    top = conn.execute(
        """SELECT COALESCE(bot_id, idea_id) AS who, COUNT(*) AS n, SUM(cost_usd) AS usd FROM ai_costs
           WHERE substr(created_at,1,7) = ? AND COALESCE(bot_id, idea_id) IS NOT NULL
           GROUP BY who ORDER BY usd DESC LIMIT 10""", (month,)).fetchall()
    by_stage = conn.execute(
        """SELECT stage, model, COUNT(*) AS n, SUM(cost_usd) AS usd FROM ai_costs WHERE substr(created_at,1,7) = ?
           GROUP BY stage, model ORDER BY usd DESC""", (month,)).fetchall()
    return {
        "today": conn.execute(q.format(n=10), (today,)).fetchone()[0],
        "month": conn.execute(q.format(n=7), (month,)).fetchone()[0],
        "credit_left": config.load_config()["ai"].get("credit_usd", 0) - spent_api,
        "top": top, "by_stage": by_stage,
    }


def _paper_rows(conn):
    """枠4: paper評価中のBOT(経過日数・取引回数・現時点の平均純利益・連敗)。"""
    rows = []
    for b in conn.execute(
            """SELECT b.*, r.symbol, r.timeframe FROM bots b JOIN rules r ON r.id = b.rule_id
               WHERE b.status IN ('paper評価中', '反証中') ORDER BY b.id"""):
        nets = [r[0] for r in conn.execute(
            "SELECT net_pnl_usd FROM trades WHERE bot_id = ? AND kind = 'paper' ORDER BY exit_at", (b["id"],))]
        streak = worst = 0
        for x in nets:
            streak = streak + 1 if x < 0 else 0
            worst = max(worst, streak)
        days = (now_ms() - b["paper_start_ms"]) / DAY_MS if b["paper_start_ms"] else 0
        rows.append({"id": b["id"], "idea": b["rule_id"], "symbol": b["symbol"], "timeframe": b["timeframe"],
                     "days": days, "n": len(nets), "avg": (sum(nets) / len(nets)) if nets else None,
                     "total": sum(nets), "streak": streak, "worst": worst, "status": b["status"],
                     "repairs": b["attempts"]})
    return rows


def _implement_failure(conn):
    """実装・修理のAIを上げるかの目安(SPEC 5.4): 3回修理しても直らなかった割合。"""
    total = conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM bots WHERE last_error LIKE '実装不良%'").fetchone()[0]
    first = conn.execute("SELECT MIN(created_at) FROM bots").fetchone()[0]
    return {"total": total, "failed": failed, "rate": failed / total if total else None, "since": first}


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
            "paper_bots": _paper_rows(conn),
            "stage_labels": STAGE_LABELS,
            "implement": _implement_failure(conn),
            "tuning": conn.execute("SELECT * FROM tuning_log ORDER BY id DESC LIMIT 1").fetchone(),
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
            "bots": conn.execute("SELECT * FROM bots WHERE rule_id = ? ORDER BY id", (idea_id,)).fetchall(),
            "trades": conn.execute(
                """SELECT t.* FROM trades t JOIN bots b ON b.id = t.bot_id WHERE b.rule_id = ?
                   ORDER BY t.entry_at DESC LIMIT 50""", (idea_id,)).fetchall(),
            "memos": conn.execute("SELECT * FROM memos WHERE idea_id = ? ORDER BY id", (idea_id,)).fetchall(),
            "costs": conn.execute("SELECT * FROM ai_costs WHERE idea_id = ? ORDER BY id", (idea_id,)).fetchall(),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "idea.html", ctx)


def serve() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=config.load_config()["dashboard"]["port"])
