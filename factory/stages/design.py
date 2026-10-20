"""新ロジック開発(SPEC 5.3): 調査を通った案を、定型(SPEC 4.2)の売買ルール1つに落とす。AIは Sonnet。

定型の全項目が数字で埋まっているかをプログラムで検査し、欠けていれば1回だけ書き直させる。
それでも欠ければ否決(札 ルール不成立)。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from .. import ai_gateway, config, db, memo
from ..hyperliquid.reader import INTERVAL_MS
from ..patterns import KINDS
from .explore import TIMEFRAMES

MAX_TRADES_PER_DAY = 48
SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "description": "銘柄(案と同じ)"},
        "timeframe": {"type": "string", "enum": TIMEFRAMES, "description": "判断に使うローソク足の長さ"},
        "entry_condition": {"type": "string",
                            "description": "入る条件。買いか売りかと、判定の数字を必ず書く(例: 直前20本の高値を終値で上に抜けたら買い)"},
        "exit_condition": {"type": "string",
                           "description": "出る条件。利益確定と損切りの%を必ず書く(例: +1.5%で利益確定、−0.8%で損切り)"},
        "take_profit_pct": {"type": "number", "description": "利益確定の幅(%)"},
        "stop_loss_pct": {"type": "number", "description": "損切りの幅(%)"},
        "max_trades_per_day": {"type": "integer", "description": "1日の最大取引回数"},
    },
    "required": ["symbol", "timeframe", "entry_condition", "exit_condition", "take_profit_pct",
                 "stop_loss_pct", "max_trades_per_day"],
    "additionalProperties": False,
}
SYSTEM = ("あなたは暗号資産の無期限先物(Hyperliquid)の売買ルールを設計する担当。"
          "案を、ローソク足(始値・高値・安値・終値・出来高)と1時間ごとの資金調達料だけで判定できる、"
          "数字で書いた売買ルール1つにする。定型の項目以外は書かない。"
          "1回の上限額・同時に持てる数(1)・レバレッジ(1倍)は土台が決めるので書かない。")
DIGIT = re.compile(r"[0-9０-９]")


@dataclass
class DesignReport:
    done: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    stopped: str | None = None


def problems(rule: dict | None, symbol: str) -> list[str]:
    """定型の項目が数字で埋まっているかの検査。欠けた項目の一覧を返す(空なら合格)。"""
    if not rule:
        return ["答えがない"]
    out = []
    if str(rule.get("symbol", "")).strip().upper() != symbol.upper():
        out.append("銘柄が案と違う")
    if rule.get("timeframe") not in INTERVAL_MS:
        out.append("時間の単位")
    entry, exit_ = str(rule.get("entry_condition", "")), str(rule.get("exit_condition", ""))
    if not DIGIT.search(entry):
        out.append("入る条件に数字がない")
    if not ("買" in entry or "売" in entry):
        out.append("入る条件に買い・売りの向きがない")
    if not DIGIT.search(exit_):
        out.append("出る条件に数字がない")
    for k, name in (("take_profit_pct", "利益確定"), ("stop_loss_pct", "損切り")):
        try:
            if not 0 < float(rule.get(k)) < 100:
                out.append(f"{name}の%が範囲外")
        except (TypeError, ValueError):
            out.append(f"{name}の%")
    try:
        if not 1 <= int(rule.get("max_trades_per_day")) <= MAX_TRADES_PER_DAY:
            out.append(f"1日の最大取引回数は1〜{MAX_TRADES_PER_DAY}")
    except (TypeError, ValueError):
        out.append("1日の最大取引回数")
    return out


def prompt_for(conn: sqlite3.Connection, idea) -> str:
    notes = memo.design_notes(conn, idea["symbol"], idea["source"])
    lines = [f"案 {idea['id']}: {idea['summary']}",
             f"仮説: {idea['hypothesis']}" if idea["hypothesis"] else "",
             f"銘柄: {idea['symbol']} / 時間の単位: {idea['timeframe']} / 向き: "
             f"{'買い' if idea['side'] == 'long' else '売り'}",
             f"入る条件の種類: {idea['entry_kind']}({KINDS.get(idea['entry_kind'], '')})"
             f" 数字: {idea['entry_params']}",
             f"案の利益確定 {idea['take_profit_pct']}% / 損切り {idea['stop_loss_pct']}%"]
    if notes:
        lines.append("過去にこの銘柄・この出どころの案は次の理由で落ちている。同じ失敗を避けること:")
        lines += [f"- {n['stage']}で{n['tag']}: {n['reason']}({n['idea_summary'][:60]})" for n in notes]
    lines.append("この案を、定型に沿った売買ルール1つにして答えて。")
    return "\n".join(x for x in lines if x)


def save_rule(conn: sqlite3.Connection, idea_id: str, symbol: str, rule: dict) -> None:
    amount = config.load_config()["paper"]["amount_usd"]
    conn.execute(
        """INSERT OR REPLACE INTO rules(id, symbol, timeframe, entry_condition, exit_condition, max_amount_usd,
               max_positions, leverage, max_trades_per_day, take_profit_pct, stop_loss_pct)
           VALUES (?,?,?,?,?,?,1,1,?,?,?)""",
        (idea_id, symbol, rule["timeframe"], rule["entry_condition"].strip(),
         rule["exit_condition"].strip(), amount, int(rule["max_trades_per_day"]),
         float(rule["take_profit_pct"]), float(rule["stop_loss_pct"])))
    conn.commit()


def design_one(conn: sqlite3.Connection, idea) -> bool:
    base = prompt_for(conn, idea)
    missing: list[str] = []
    for attempt in range(2):     # 欠けていれば1回だけ書き直し
        prompt = base if not missing else (
            base + "\n前の答えは定型の項目が欠けていた: " + "、".join(missing) + "。全部を数字で埋めて書き直して。")
        r = ai_gateway.ask(conn, "design", prompt, system=SYSTEM, schema=SCHEMA, idea_id=idea["id"])
        missing = problems(r.data, idea["symbol"])
        if not missing:
            save_rule(conn, idea["id"], idea["symbol"], r.data)
            db.set_idea_state(conn, idea["id"], "新ロジック開発", "完了")
            return True
        if attempt == 0:
            db.set_idea_state(conn, idea["id"], "新ロジック開発", "保留")
    memo.memo_for_idea(conn, idea["id"], "新ロジック開発", "ルール不成立",
                       ("定型の項目が欠けた: " + "、".join(missing))[:memo.REASON_MAX])
    return False


def run(conn: sqlite3.Connection, *, limit: int | None = None) -> DesignReport:
    limit = limit or config.load_config()["design"]["max_per_day"]
    rows = conn.execute(
        """SELECT * FROM ideas WHERE (stage = '調査' AND verdict = '継続')
               OR (stage = '新ロジック開発' AND verdict = '保留') ORDER BY id LIMIT ?""", (limit,)).fetchall()
    rep = DesignReport()
    for idea in rows:
        try:
            (rep.done if design_one(conn, idea) else rep.rejected).append(idea["id"])
        except ai_gateway.AIError as e:
            rep.stopped = str(e)     # 予算切れなど。残りは次の実行で
            break
    return rep
