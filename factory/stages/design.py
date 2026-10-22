"""新ロジック開発(SPEC 5.3): 調査を通った案を、型ごとの定型(SPEC 4.2)の売買ルール1つに落とす。AIは Sonnet。

定型の全項目が数字で埋まっているかをプログラムで検査し、欠けていれば1回だけ書き直させる。
それでも欠ければ否決(札 ルール不成立)。AIには継続性と競争の札(SPEC 4.3)も選ばせる。

値動き型: AIが時間の単位・入る条件・出る条件・利益確定・損切り・最長保有・1日の最大取引回数を書く。
歪み型(資金調達料): 入る x はプログラムが決める(調査で見つかった直近30日の最大値の半分。AIが勝手に大きくしない)。
  AIは出る y・最長保有 N・損切り・1日の最大取引回数を数字で答える。入る条件・出る条件の文はプログラムが数字から書く。
資金の上限(SPEC 9章): 1回に要る最小の資金が1本の上限額(paper評価の200ドル)を超える案は、AIに頼む前に否決(札 資金不足)。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field

from .. import ai_gateway, config, db, memo
from ..hyperliquid.reader import INTERVAL_MS, ReaderError
from ..market import DAY_MS
from ..models import COMPETITION, CONTINUITY, DISTORTION
from ..patterns import KINDS
from .explore import TIMEFRAMES

MAX_TRADES_PER_DAY = 48
MAX_HOLD_HOURS = 720            # 最長保有の上限(30日)。paper評価の14日・30回に収まらない長さは書かせない
DISTORTION_TIMEFRAME = "1h"     # 歪み型は1時間ごとの資金調達料で判断するので、足も1時間
TAGS_SCHEMA = {
    "continuity": {"type": "string", "enum": CONTINUITY,
                   "description": "継続性の札。機会を1回取ったら終わるなら 一回きり、同じ機会が繰り返し出るなら 常にある"},
    "competition": {"type": "string", "enum": COMPETITION,
                    "description": "競争の札。同じ機会を狙う他の参加者が見える(上位の財布が同じ銘柄・同じ型で取引しているなど)なら 競争あり"},
}
PRICE_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "description": "銘柄(案と同じ)"},
        "timeframe": {"type": "string", "enum": TIMEFRAMES, "description": "判断に使うローソク足の長さ"},
        "entry_condition": {"type": "string",
                            "description": "入る条件。買いか売りかと、判定の数字を必ず書く(例: 直前20本の高値を終値で上に抜けたら買い)"},
        "exit_condition": {"type": "string",
                           "description": "出る条件。利益確定・損切りの%と保有の上限時間を必ず書く(例: +1.5%で利益確定、−0.8%で損切り、最長12時間)"},
        "take_profit_pct": {"type": "number", "description": "利益確定の幅(%)"},
        "stop_loss_pct": {"type": "number", "description": "損切りの幅(%)"},
        "max_hold_hours": {"type": "number", "description": "保有の上限時間(時間)。持ったら必ず出る"},
        "max_trades_per_day": {"type": "integer", "description": "1日の最大取引回数"},
        **TAGS_SCHEMA,
    },
    "required": ["symbol", "timeframe", "entry_condition", "exit_condition", "take_profit_pct",
                 "stop_loss_pct", "max_hold_hours", "max_trades_per_day", "continuity", "competition"],
    "additionalProperties": False,
}
DISTORTION_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "description": "銘柄(案と同じ)"},
        "funding_y_pct": {"type": "number",
                          "description": "出る資金調達料 y(%/時)。資金調達料が ±y% の内側に戻ったら出る。0 以上で x より小さい"},
        "max_hold_hours": {"type": "number", "description": "最長保有 N(時間)。持ったら必ず出る"},
        "stop_loss_pct": {"type": "number", "description": "損切りの幅(%)。入った値段からこれだけ逆に動いたら出る"},
        "max_trades_per_day": {"type": "integer", "description": "1日の最大取引回数"},
        **TAGS_SCHEMA,
    },
    "required": ["symbol", "funding_y_pct", "max_hold_hours", "stop_loss_pct", "max_trades_per_day",
                 "continuity", "competition"],
    "additionalProperties": False,
}
SCHEMA = PRICE_SCHEMA       # 段階3の呼び名(値動き型)
SYSTEM = ("あなたは暗号資産の無期限先物(Hyperliquid)の売買ルールを設計する担当。"
          "案を、ローソク足(始値・高値・安値・終値・出来高)と1時間ごとの資金調達料だけで判定できる、"
          "数字で書いた売買ルール1つにする。定型の項目以外は書かない。"
          "1回の上限額・同時に持てる数(1)・レバレッジ(1倍)は土台が決めるので書かない。")
SYSTEM_DISTORTION = ("あなたは暗号資産の無期限先物(Hyperliquid)の「資金調達料の歪み」を取る売買ルールを設計する担当。"
                     "資金調達料を受け取る側で入り、受け取りを積み重ねる。危険は持っている間に値段が逆に動くことなので、"
                     "損切りと最長保有時間で守る。入る資金調達料 x は決まっている。定型の項目を数字だけで答える。"
                     "1回の上限額・同時に持てる数(1)・レバレッジ(1倍)は土台が決めるので書かない。")
DIGIT = re.compile(r"[0-9０-９]")


@dataclass
class DesignReport:
    done: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    stopped: str | None = None


def _tag_problems(rule: dict) -> list[str]:
    out = []
    if rule.get("continuity") not in CONTINUITY:
        out.append("継続性の札(" + "/".join(CONTINUITY) + ")")
    if rule.get("competition") not in COMPETITION:
        out.append("競争の札(" + "/".join(COMPETITION) + ")")
    return out


def _number_problems(rule: dict, max_trades: int) -> list[str]:
    out = []
    try:
        if not 0 < float(rule.get("stop_loss_pct")) < 100:
            out.append("損切りの%が範囲外")
    except (TypeError, ValueError):
        out.append("損切りの%")
    try:
        if not 0 < float(rule.get("max_hold_hours")) <= MAX_HOLD_HOURS:
            out.append(f"保有の上限時間は0より大きく{MAX_HOLD_HOURS}時間以下")
    except (TypeError, ValueError):
        out.append("保有の上限時間")
    try:
        if not 1 <= int(rule.get("max_trades_per_day")) <= max_trades:
            out.append(f"1日の最大取引回数は1〜{max_trades}")
    except (TypeError, ValueError):
        out.append("1日の最大取引回数")
    return out


def problems(rule: dict | None, symbol: str) -> list[str]:
    """値動き型の定型の項目が数字で埋まっているかの検査。欠けた項目の一覧を返す(空なら合格)。"""
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
    try:
        if not 0 < float(rule.get("take_profit_pct")) < 100:
            out.append("利益確定の%が範囲外")
    except (TypeError, ValueError):
        out.append("利益確定の%")
    return out + _number_problems(rule, MAX_TRADES_PER_DAY) + _tag_problems(rule)


def distortion_problems(rule: dict | None, symbol: str, x: float) -> list[str]:
    """歪み型の定型の検査。"""
    if not rule:
        return ["答えがない"]
    out = []
    if str(rule.get("symbol", "")).strip().upper() != symbol.upper():
        out.append("銘柄が案と違う")
    try:
        if not 0 <= float(rule.get("funding_y_pct")) < x:
            out.append(f"出る資金調達料 y は0以上で x({x:g}%)より小さく")
    except (TypeError, ValueError):
        out.append("出る資金調達料 y")
    return out + _number_problems(rule, 24) + _tag_problems(rule)


def entry_x(idea) -> float | None:
    """歪み型の入る x(%/時): 調査で見つかった直近30日の最大値の半分(SPEC 5.3 の4)。"""
    try:
        p = json.loads(idea["entry_params"] or "{}")
    except ValueError:
        return None
    for key, scale in (("max30", 0.5), ("x", 1.0)):     # 最大値がない時(調査の前の版)は案の x のまま
        try:
            x = round(float(p[key]) * scale, 6)
        except (KeyError, TypeError, ValueError):
            continue
        if x > 0:
            return x
    return None


def prompt_for(conn: sqlite3.Connection, idea) -> str:
    notes = memo.design_notes(conn, idea["type"], idea["symbol"], idea["source"])
    lines = [f"案 {idea['id']}({idea['type']}型): {idea['summary']}",
             f"仮説: {idea['hypothesis']}" if idea["hypothesis"] else ""]
    if idea["type"] == DISTORTION:
        x = entry_x(idea)
        params = json.loads(idea["entry_params"] or "{}")
        lines += [f"銘柄: {idea['symbol']} / 判断は1時間ごとの資金調達料",
                  f"入る条件(決まっている): 1時間あたりの資金調達料が +{x:g}% 以上なら売りで入る/"
                  f" −{x:g}% 以下なら買いで入る",
                  f"調査の数字: 直近30日の最大 ±{params.get('max30', '?')}%/時・"
                  f"±{params.get('x', '?')}%/時を超えた時間 {params.get('hours30', '?')}時間"]
    else:
        lines += [f"銘柄: {idea['symbol']} / 時間の単位: {idea['timeframe']} / 向き: "
                  f"{'買い' if idea['side'] == 'long' else '売り'}",
                  f"入る条件の種類: {idea['entry_kind']}({KINDS.get(idea['entry_kind'], '')})"
                  f" 数字: {idea['entry_params']}",
                  f"案の利益確定 {idea['take_profit_pct']}% / 損切り {idea['stop_loss_pct']}%"]
    if notes:
        lines.append("過去にこの型・この銘柄・この出どころの案は次の理由で落ちている。同じ失敗を避けること:")
        lines += [f"- {n['stage']}で{n['tag']}: {n['reason']}({n['idea_summary'][:60]})" for n in notes]
    lines.append("この案を、定型に沿った売買ルール1つにして答えて。")
    return "\n".join(x for x in lines if x)


def capital_needed(market, symbol: str) -> float | None:
    """1回の注文に要る最小の資金(ドル): 取引所の最小注文額と、数量の最小単位1つ分の大きい方。
    銘柄が取引所にもうなければ None。価格が取れなければ ReaderError。"""
    hl = config.load_config()["hyperliquid"]
    decimals = market.size_decimals().get(symbol)
    if decimals is None:
        return None
    now = market.now()
    candles = market.candles(symbol, "1h", now - DAY_MS, now)
    if not candles:
        raise ReaderError(f"{symbol} の価格が取れなかった")
    return max(float(hl["min_order_usd"]), 10 ** -decimals * candles[-1].c)


def distortion_texts(x: float, rule: dict) -> tuple[str, str]:
    entry = f"1時間あたりの資金調達料が +{x:g}% 以上なら売りで入る/ −{x:g}% 以下なら買いで入る(受け取る側)"
    exit_ = (f"資金調達料が ±{float(rule['funding_y_pct']):g}% の内側に戻ったら出る。"
             f"最長{float(rule['max_hold_hours']):g}時間で必ず出る。−{float(rule['stop_loss_pct']):g}%で損切り")
    return entry, exit_


def save_rule(conn: sqlite3.Connection, idea, rule: dict, *, x: float | None = None,
              min_capital: float | None = None) -> None:
    amount = config.load_config()["paper"]["amount_usd"]
    type_ = idea["type"]
    if type_ == DISTORTION:
        entry, exit_ = distortion_texts(x, rule)
        timeframe, tp, y = DISTORTION_TIMEFRAME, None, float(rule["funding_y_pct"])
    else:
        entry, exit_ = rule["entry_condition"].strip(), rule["exit_condition"].strip()
        timeframe, tp, y, x = rule["timeframe"], float(rule["take_profit_pct"]), None, None
    conn.execute(
        """INSERT OR REPLACE INTO rules(id, symbol, timeframe, entry_condition, exit_condition, max_amount_usd,
               max_positions, leverage, max_trades_per_day, take_profit_pct, stop_loss_pct, type, max_hold_hours,
               funding_x, funding_y, continuity, competition, min_capital_usd)
           VALUES (?,?,?,?,?,?,1,1,?,?,?,?,?,?,?,?,?,?)""",
        (idea["id"], idea["symbol"], timeframe, entry, exit_, amount, int(rule["max_trades_per_day"]), tp,
         float(rule["stop_loss_pct"]), type_, float(rule["max_hold_hours"]), x, y, rule["continuity"],
         rule["competition"], min_capital))
    conn.commit()


def over_cap(conn: sqlite3.Connection, idea, market) -> tuple[bool, float | None]:
    """資金の上限の検査(SPEC 9章)。戻り値: (否決したか, 1回に要る最小の資金)。
    価格が取れなければ ReaderError(その案は次の実行へ)。market がなければ検査しない。"""
    if market is None:
        return False, None
    need = capital_needed(market, idea["symbol"])
    if need is None:
        memo.memo_for_idea(conn, idea["id"], "新ロジック開発", "未対応", f"{idea['symbol']}はHyperliquidの無期限先物にない")
        return True, None
    cap = config.load_config()["paper"]["amount_usd"]
    if need > cap:
        memo.memo_for_idea(conn, idea["id"], "新ロジック開発", "資金不足",
                           f"1回に要る資金{math.ceil(need)}ドルが1本の上限{cap}ドルを超える"[:memo.REASON_MAX])
        return True, need
    return False, need


def design_one(conn: sqlite3.Connection, idea, market=None) -> bool:
    capped, need = over_cap(conn, idea, market)
    if capped:
        return False
    distortion = idea["type"] == DISTORTION
    x = entry_x(idea) if distortion else None
    if distortion and x is None:
        memo.memo_for_idea(conn, idea["id"], "新ロジック開発", "ルール不成立", "入る資金調達料 x の数字がない")
        return False
    base = prompt_for(conn, idea)
    missing: list[str] = []
    for attempt in range(2):     # 欠けていれば1回だけ書き直し
        prompt = base if not missing else (
            base + "\n前の答えは定型の項目が欠けていた: " + "、".join(missing) + "。全部を数字で埋めて書き直して。")
        r = ai_gateway.ask(conn, "design", prompt, system=SYSTEM_DISTORTION if distortion else SYSTEM,
                           schema=DISTORTION_SCHEMA if distortion else PRICE_SCHEMA, idea_id=idea["id"])
        missing = (distortion_problems(r.data, idea["symbol"], x) if distortion
                   else problems(r.data, idea["symbol"]))
        if not missing:
            save_rule(conn, idea, r.data, x=x, min_capital=need)
            db.set_idea_state(conn, idea["id"], "新ロジック開発", "完了")
            return True
        if attempt == 0:
            db.set_idea_state(conn, idea["id"], "新ロジック開発", "保留")
    memo.memo_for_idea(conn, idea["id"], "新ロジック開発", "ルール不成立",
                       ("定型の項目が欠けた: " + "、".join(missing))[:memo.REASON_MAX])
    return False


def run(conn: sqlite3.Connection, market=None, *, limit: int | None = None) -> DesignReport:
    limit = limit or config.load_config()["design"]["max_per_day"]
    rows = db.interleave_types(conn.execute(
        """SELECT * FROM ideas WHERE (stage = '調査' AND verdict = '継続')
               OR (stage = '新ロジック開発' AND verdict = '保留') ORDER BY id""").fetchall(), limit)
    rep = DesignReport()
    for idea in rows:
        try:
            (rep.done if design_one(conn, idea, market) else rep.rejected).append(idea["id"])
        except ReaderError:
            continue                 # 価格が取れない案は次の実行で
        except ai_gateway.AIError as e:
            rep.stopped = str(e)     # 予算切れなど。残りは次の実行で
            break
    return rep

