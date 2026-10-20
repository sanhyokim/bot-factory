"""調査(SPEC 5.2)。計算はプログラムで行い、AI(Haiku)には見送りの理由の文章化だけを一括処理で頼む。

1. 値幅(利益確定の%)が、往復の手数料+想定の滑りの3倍未満なら見送り(札 手数料負け)
2. 直近30日で入る条件に当てはまる場面が10回未満なら見送り(札 機会不足)
3. 必要資金が200ドルを超えるなら見送り(札 資金不足)
外部データが取れなければ保留。3回で未対応として否決。
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field

from .. import ai_gateway, config, db, memo, tuning
from ..hyperliquid.reader import INTERVAL_MS, ReaderError, interval_code
from ..market import DAY_MS, Market
from ..patterns import COUNTABLE, count_episodes

PURPOSE = "investigate_reason"
WARMUP_BARS = 1000


@dataclass
class Decision:
    verdict: str            # 継続 / 見送り / 保留
    tag: str | None = None
    reason: str = ""


@dataclass
class InvestigateReport:
    passed: list[str] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)
    held: list[str] = field(default_factory=list)


def _params(idea) -> tuple[int, float]:
    try:
        p = json.loads(idea["entry_params"] or "{}")
        return int(p.get("n", 20)), float(p.get("x", 0))
    except (ValueError, TypeError):
        return 20, 0.0


def check(idea, market: Market, settings: dict) -> Decision:
    cfg = config.load_config()
    inv, hl, amount = cfg["investigate"], cfg["hyperliquid"], cfg["paper"]["amount_usd"]
    tp, side = idea["take_profit_pct"], idea["side"]
    if tp is None or side not in ("long", "short"):
        return Decision("見送り", "未対応", "数字(利益確定の%・向き)がない案のため調べられない")
    try:
        code = interval_code(idea["timeframe"])
    except ValueError:
        return Decision("見送り", "未対応", f"時間の単位「{idea['timeframe']}」を扱えない")

    # 1. 手数料負け(データ不要)
    cost = 2 * hl["taker_fee_rate"] * 100 + inv["slippage_pct"]
    mult = settings["fee_multiple"]
    if tp < mult * cost:
        return Decision("見送り", "手数料負け",
                        f"値幅{tp:g}%が往復の手数料+滑り{cost:.2f}%の{mult:g}倍({mult * cost:.2f}%)未満")

    # 2・3 は取引所のデータを使う。取れなければ保留(ReaderError をそのまま上へ)
    decimals = market.size_decimals()
    symbol = market.resolve(idea["symbol"])
    if symbol is None:
        return Decision("見送り", "未対応", f"{idea['symbol']}はHyperliquidの無期限先物にない")
    if symbol != idea["symbol"]:
        market.conn.execute("UPDATE ideas SET symbol = ? WHERE id = ?", (symbol, idea["id"]))
    step = INTERVAL_MS[code]
    now = market.now()
    since = now - inv["days"] * DAY_MS
    candles = market.candles(symbol, code, since - WARMUP_BARS * step, now)
    if not candles:
        raise ReaderError(f"{symbol} {code} の価格が取れなかった")

    if idea["entry_kind"] in COUNTABLE:
        n, x = _params(idea)
        funding = market.funding(symbol, since - DAY_MS, now) if idea["entry_kind"] == "資金調達料" else []
        count = count_episodes(idea["entry_kind"], side, n, x, candles, funding, since=since, step=step)
        counted = [c for c in candles if c.t >= since]
        span_days = (counted[-1].t + step - counted[0].t) / DAY_MS if counted else 0
        # 取引所が30日分を返さない短い足(1分・5分)は、取れた日数から30日分に直して比べる
        scaled = count * inv["days"] / span_days if 0 < span_days < inv["days"] - 0.5 else count
        need = settings["min_occurrences"]
        if scaled < need:
            note = f"(取れた{span_days:.1f}日分を30日に換算)" if scaled != count else ""
            return Decision("見送り", "機会不足", f"30日で{scaled:.0f}回{note}、基準{need:g}回未満")

    price, unit = candles[-1].c, 10 ** -decimals[symbol]
    qty = math.floor(amount / price / unit + 1e-9) * unit
    if qty * price < hl["min_order_usd"]:
        return Decision("見送り", "資金不足",
                        f"{amount}ドルでは最小の注文{hl['min_order_usd']}ドルに届かない(1単位{unit * price:.0f}ドル)")
    return Decision("継続")


def run(conn: sqlite3.Connection, market: Market, *, limit: int | None = None) -> InvestigateReport:
    cfg = config.load_config()["investigate"]
    settings = tuning.current(conn)
    rows = conn.execute(
        """SELECT * FROM ideas WHERE (stage = '探索' AND verdict = '案登録') OR (stage = '調査' AND verdict = '保留')
           ORDER BY id LIMIT ?""", (limit or cfg["max_per_day"],)).fetchall()
    rep = InvestigateReport()
    reasons: list[ai_gateway.BatchItem] = []
    for idea in rows:
        try:
            d = check(idea, market, settings)
        except ReaderError:
            holds = (idea["hold_count"] or 0) + 1
            conn.execute("UPDATE ideas SET hold_count = ? WHERE id = ?", (holds, idea["id"]))
            if holds >= cfg["max_holds"]:
                d = Decision("見送り", "未対応", f"外部データが{holds}回取れなかった")
            else:
                db.set_idea_state(conn, idea["id"], "調査", "保留")
                rep.held.append(idea["id"])
                continue
        if d.verdict == "継続":
            db.set_idea_state(conn, idea["id"], "調査", "継続")
            rep.passed.append(idea["id"])
            continue
        memo_id = memo.memo_for_idea(conn, idea["id"], "調査", d.tag, d.reason[:memo.REASON_MAX])
        rep.rejected[d.tag] = rep.rejected.get(d.tag, 0) + 1
        if d.tag != "未対応":
            reasons.append(ai_gateway.BatchItem(
                key=memo_id, idea_id=idea["id"], meta={"memo_id": memo_id},
                prompt=("暗号資産BOTの案を見送った理由を、売買に詳しくない人向けに日本語1文・"
                        f"{memo.REASON_MAX}字以内で書いて。数字は残し、文だけを答える。\n"
                        f"案: {idea['summary']}\n札: {d.tag}\n計算: {d.reason}")))
    if reasons:
        try:
            ai_gateway.submit_batch(conn, "investigate", PURPOSE, reasons, max_tokens=200)
        except ai_gateway.AIError:
            pass    # 文章化は飾り。送れなくても計算で書いた理由が残る
    return rep


def collect_reasons(conn: sqlite3.Connection) -> int:
    """AIが書いた見送りの理由で、否決メモの一言を置き換える。置き換えた数を返す。"""
    n = 0
    for out in ai_gateway.collect_batches(conn, PURPOSE):
        text = (out.result.text if out.result else "").strip().strip("「」\"")
        if out.error or not text or len(text) > memo.REASON_MAX or "\n" in text:
            continue
        conn.execute("UPDATE memos SET reason = ? WHERE id = ?", (text, out.meta.get("memo_id", out.key)))
        n += 1
    conn.commit()
    return n
