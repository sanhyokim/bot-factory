"""ダッシュボード(SPEC 8章)。http://localhost:8800 で開く。

段階1で作った枠: 1 要判断 / 2 工程別の集計 / 5 否決メモ。段階3で 4 paper評価中 / 6 費用 を足した。
段階3bで、型・継続性・競争・作ったAIの絞り込み、枠5の型別の集計とまとめ、枠6の見張り役・データ窓口・AI別の黒字集計、
枠7 札ごとの歩留まり、ログの画面(/logs)を足した。枠3(本番運用中)は段階4で足す。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from datetime import timedelta

from .. import config, db, logs, memo, watchdog
from ..hyperliquid.reader import now_ms
from ..models import COMPETITION, CONTINUITY, GATE_STATES, MODELS, STAGES, TAGS, TYPES, VERDICTS, now_jst

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


# 「paper評価に合格した」BOT: 反証へ進んだもの(反証で落ちたものも含む)
PAPER_PASSED = "(b.status IN ('反証中', '本番待ち', '本番運用中', '停止中') OR b.refute_note IS NOT NULL)"
NOT_SET = "未設定(段階3bより前)"


def _live_nets(conn) -> dict[str, float]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT bot_id, SUM(net_pnl_usd) FROM trades WHERE kind = 'live' GROUP BY bot_id")}


def _funnel(conn, column: str, labels: list[str]) -> list[dict]:
    """実装した本数 → paper評価に合格した本数 → 本番で黒字になった本数(column の値ごと)。
    実装した = 自動テストを通って paper評価を始めたBOT。"""
    live = _live_nets(conn)
    rows = conn.execute(
        f"""SELECT {column} AS k, b.id, {PAPER_PASSED} AS passed FROM bots b JOIN rules r ON r.id = b.rule_id
            WHERE b.paper_start_ms IS NOT NULL""").fetchall()
    out = []
    for label in labels + [None]:
        hit = [r for r in rows if r["k"] == label]
        if label is None and not hit:
            continue
        out.append({"label": label or NOT_SET, "implemented": len(hit), "passed": sum(1 for r in hit if r["passed"]),
                    "profitable": sum(1 for r in hit if live.get(r["id"], 0) > 0)})
    return out


def _yields(conn) -> dict[str, list[dict]]:
    """枠7: 札ごとの歩留まり(SPEC 8章)。型・継続性の札・競争の札のそれぞれ。"""
    return {"型": _funnel(conn, "r.type", list(TYPES)), "継続性": _funnel(conn, "b.continuity", list(CONTINUITY)),
            "競争": _funnel(conn, "b.competition", list(COMPETITION))}


def _ai_scoreboard(conn) -> list[dict]:
    """AI別の黒字集計(SPEC 7.3): 作ったAIごとに、実装した本数・paper合格・本番で黒字・本番の純利益・AI代・打率。"""
    live = _live_nets(conn)
    out = []
    names = [r[0] for r in conn.execute("SELECT DISTINCT made_by FROM bots")]
    for model in sorted(names, key=lambda m: (MODELS.index(m) if m in MODELS else 99, m or "")):
        bots = conn.execute(
            f"""SELECT b.id, b.rule_id, b.paper_start_ms IS NOT NULL AS started, {PAPER_PASSED} AS passed
                FROM bots b WHERE b.made_by IS ?""", (model,)).fetchall()
        started = [b for b in bots if b["started"]]
        ids, rules = [b["id"] for b in bots], [b["rule_id"] for b in bots]
        marks = ",".join("?" * len(ids))
        cost = conn.execute(f"""SELECT COALESCE(SUM(cost_usd), 0) FROM ai_costs
                                WHERE bot_id IN ({marks}) OR idea_id IN ({marks})""", ids + rules).fetchone()[0]
        profitable = sum(1 for b in started if live.get(b["id"], 0) > 0)
        out.append({"model": (model or "不明").capitalize(), "implemented": len(started),
                    "broken": len(bots) - len(started), "passed": sum(1 for b in started if b["passed"]),
                    "profitable": profitable, "live_net": sum(live.get(b["id"], 0) for b in started), "cost": cost,
                    "rate": profitable / len(started) if started else None})
    return out


def _watch(conn) -> dict:
    """枠6: 見張り役の再起動の記録と、データ窓口に断られた回数。"""
    week = (now_jst() - timedelta(days=7)).isoformat(timespec="seconds")
    today = now_jst().date().isoformat()
    return {
        "events": conn.execute("SELECT * FROM watchdog_events WHERE event != '起動' ORDER BY id DESC LIMIT 10")
                      .fetchall(),
        "refused_today": conn.execute("SELECT COALESCE(SUM(count), 0) FROM exchange_refusals WHERE created_at >= ?",
                                      (today,)).fetchone()[0],
        "refused_week": conn.execute("SELECT COALESCE(SUM(count), 0) FROM exchange_refusals WHERE created_at >= ?",
                                     (week,)).fetchone()[0],
        "labels": watchdog.LABELS,
    }


def _memo_panel(conn) -> dict:
    """枠5: 直近7日の札の集計表(型ごと)、週次3指標(型ごと)、まとめ、型ごとの今の絞り込み。"""
    merged = conn.execute("""SELECT COUNT(*), COALESCE(SUM(merge_count), 0) FROM memos
                             WHERE merge_count > 1 AND merged_into IS NULL""").fetchone()
    tuning_rows = {t: conn.execute("SELECT * FROM tuning_log WHERE type = ? ORDER BY id DESC LIMIT 1", (t,)).fetchone()
                   for t in TYPES}
    return {"tags": {t: memo.tag_counts(conn, 7, type_=t) for t in TYPES},
            "weekly": {t: memo.weekly_metrics(conn, 8, type_=t) for t in TYPES},
            "merged": {"targets": merged[0], "originals": merged[1]},
            "tuning": {t: r for t, r in tuning_rows.items() if r is not None}}


def _paper_rows(conn):
    """枠4: paper評価中のBOT(経過日数・取引回数・現時点の平均純利益・連敗)。"""
    rows = []
    for b in conn.execute(
            """SELECT b.*, r.symbol, r.timeframe, r.type FROM bots b JOIN rules r ON r.id = b.rule_id
               WHERE b.status IN ('paper評価中', '反証中') ORDER BY b.id"""):
        nets = [r[0] for r in conn.execute(
            "SELECT net_pnl_usd FROM trades WHERE bot_id = ? AND kind = 'paper' ORDER BY exit_at", (b["id"],))]
        streak = worst = 0
        for x in nets:
            streak = streak + 1 if x < 0 else 0
            worst = max(worst, streak)
        days = (now_ms() - b["paper_start_ms"]) / DAY_MS if b["paper_start_ms"] else 0
        rows.append({"id": b["id"], "idea": b["rule_id"], "symbol": b["symbol"], "timeframe": b["timeframe"],
                     "type": b["type"], "base_version": b["base_version"],
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
          tag: str | None = None, type: str | None = None, continuity: str | None = None,
          competition: str | None = None, made_by: str | None = None):
    conn = db.connect()
    try:
        counts = db.stage_verdict_counts(conn, type or None)
        max_n = max((r["n"] for r in counts), default=1)
        where, memo_args = [], []
        if tag:
            where.append("tag = ?")
            memo_args.append(tag)
        if type:
            where.append("type = ?")
            memo_args.append(type)
        memo_sql = "SELECT * FROM memos" + (" WHERE " + " AND ".join(where) if where else "")
        ctx = {
            "gates": _gate_rows(conn),
            "gate_labels": GATE_STATES,
            "given_up": watchdog.given_up(conn),
            "counts": counts,
            "max_n": max_n,
            "ideas": db.list_ideas(conn, stage or None, verdict or None, limit=100, type_=type or None,
                                   continuity=continuity or None, competition=competition or None,
                                   made_by=made_by or None),
            "memo_panel": _memo_panel(conn),
            "memos": conn.execute(memo_sql + " ORDER BY id DESC LIMIT 50", memo_args).fetchall(),
            "costs": _costs(conn),
            "paper_bots": _paper_rows(conn),
            "stage_labels": STAGE_LABELS,
            "implement": _implement_failure(conn),
            "watch": _watch(conn),
            "scoreboard": _ai_scoreboard(conn),
            "yields": _yields(conn),
            "filters": {"stage": stage or "", "verdict": verdict or "", "tag": tag or "", "type": type or "",
                        "continuity": continuity or "", "competition": competition or "", "made_by": made_by or ""},
            "stages": STAGES, "verdicts": VERDICTS, "tags": TAGS + ["合格"], "types": TYPES,
            "continuities": CONTINUITY, "competitions": COMPETITION, "models": MODELS,
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
            "distortion": idea["type"] == TYPES[1],
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


@app.get("/logs", response_class=HTMLResponse)
def log_page(request: Request, day: str | None = None, stage: str | None = None):
    """ログの画面(SPEC 3.3): 日付と工程で引く。"""
    days = logs.days()
    day = day or (days[0] if days else now_jst().date().isoformat())
    ctx = {"day": day, "days": days, "stage": stage or "", "stages": logs.stages(day),
           "rows": logs.read(day, stage=stage or None, limit=1000)}
    return templates.TemplateResponse(request, "logs.html", ctx)


def serve() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=config.load_config()["dashboard"]["port"])
