"""調査(SPEC 5.2)。計算はプログラムで行い、AI(Haiku)には見送りの理由の文章化だけを一括処理で頼む。

値動き型:
1. 値幅(利益確定の%)が、往復の手数料+想定の滑りの3倍未満なら見送り(札 手数料負け)
2. 直近30日で入る条件に当てはまる場面が10回未満なら見送り(札 機会不足)。入る条件の種類が7語のどれでもない案は数えられないので否決(札 ルール不成立)
3. 必要資金が200ドルを超えるなら見送り(札 資金不足)
歪み型(資金調達料):
1. 直近30日で、1時間あたりの資金調達料の絶対値が x% を超えた時間が30時間未満なら見送り(札 機会不足)
2. その時間に受け取る側で持っていたとした受け取りの合計が、往復の手数料+滑り(0.14%)× 入った回数 の2倍未満なら見送り(札 手数料負け)。
   入った回数は、x% を超えた時間が続いたひとかたまり(同じ向き)を1回と数える
3. 必要資金は値動き型と同じ
通った歪み型の案には、30日の最大値(新ロジック開発が x を決めるのに使う)を数字に書き足す。
2・3の数字(3倍・10回、歪み型の2倍)は週1回の棚卸しで型ごとに1段きつくなることがある(tuning.py)。
外部データが取れなければ保留。3回で未対応として否決。
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field

from .. import ai_gateway, config, db, memo, tuning
from ..hyperliquid.reader import INTERVAL_MS, ReaderError, interval_code
from ..market import DAY_MS, HOUR_MS, Market
from ..models import DISTORTION
from ..patterns import KINDS, UNCLASSIFIED_REASON, count_episodes

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


def _capital(symbol: str, price: float, decimals: int) -> Decision | None:
    """必要資金の検査(両型で同じ): 200ドルで取引所の最小の注文に届くか。"""
    cfg = config.load_config()
    hl, amount = cfg["hyperliquid"], cfg["paper"]["amount_usd"]
    unit = 10 ** -decimals
    qty = math.floor(amount / price / unit + 1e-9) * unit
    if qty * price < hl["min_order_usd"]:
        return Decision("見送り", "資金不足",
                        f"{amount}ドルでは最小の注文{hl['min_order_usd']}ドルに届かない(1単位{unit * price:.0f}ドル)")
    return None


def _resolve(idea, market: Market) -> str | None:
    symbol = market.resolve(idea["symbol"])
    if symbol is not None and symbol != idea["symbol"]:
        market.conn.execute("UPDATE ideas SET symbol = ? WHERE id = ?", (symbol, idea["id"]))
    return symbol


def funding_runs(rates: list, threshold: float) -> list[list]:
    """しきい値を超えた時間の、続いたひとかたまり(同じ向き・1時間おき)の一覧。"""
    runs: list[list] = []
    for f in rates:
        if abs(f.rate) <= threshold:
            continue
        last = runs[-1][-1] if runs else None
        if last is not None and f.t - last.t <= HOUR_MS * 1.5 and (f.rate > 0) == (last.rate > 0):
            runs[-1].append(f)
        else:
            runs.append([f])
    return runs


def check_distortion(idea, market: Market, settings: dict) -> Decision:
    """歪み型(資金調達料)の調査。"""
    cfg = config.load_config()
    inv, hl = cfg["investigate"], cfg["hyperliquid"]
    if idea["entry_kind"] != "資金調達料":
        return Decision("見送り", "ルール不成立", "歪み型で扱えるのは資金調達料だけ")
    try:
        params = json.loads(idea["entry_params"] or "{}")
        x = float(params["x"])
    except (ValueError, TypeError, KeyError):
        return Decision("見送り", "未対応", "入る資金調達料(x%)の数字がない案のため調べられない")
    symbol = _resolve(idea, market)
    if symbol is None:
        return Decision("見送り", "未対応", f"{idea['symbol']}はHyperliquidの無期限先物にない")
    now = market.now()
    rates = market.funding(symbol, now - inv["days"] * DAY_MS, now)
    if not rates:
        raise ReaderError(f"{symbol} の資金調達料が取れなかった")
    runs = funding_runs(rates, x / 100)
    hours = sum(len(r) for r in runs)
    need = inv["funding_min_hours"]
    if hours < need:
        return Decision("見送り", "機会不足", f"30日で±{x:g}%/時を超えた時間が{hours}時間、基準{need}時間未満")
    got = sum(abs(f.rate) for r in runs for f in r) * 100
    cost = 2 * hl["taker_fee_rate"] * 100 + hl["slippage_pct"]
    mult = settings["funding_fee_multiple"]
    if got < mult * cost * len(runs):
        return Decision("見送り", "手数料負け",
                        f"受け取り計{got:.2f}%が往復{cost:.2f}%×{len(runs)}回の{mult:g}倍"
                        f"({mult * cost * len(runs):.2f}%)未満")
    candles = market.candles(symbol, "1h", now - DAY_MS, now)
    if not candles:
        raise ReaderError(f"{symbol} の価格が取れなかった")
    low = _capital(symbol, candles[-1].c, market.size_decimals()[symbol])
    if low is not None:
        return low
    params["max30"] = round(max(abs(f.rate) for f in rates) * 100, 6)    # 新ロジック開発が x を決めるのに使う
    params["hours30"] = hours
    market.conn.execute("UPDATE ideas SET entry_params = ? WHERE id = ?",
                        (json.dumps(params, ensure_ascii=False), idea["id"]))
    return Decision("継続")


def check(idea, market: Market, settings: dict) -> Decision:
    if idea["type"] == DISTORTION:
        return check_distortion(idea, market, settings)
    cfg = config.load_config()
    inv, hl = cfg["investigate"], cfg["hyperliquid"]
    tp, side = idea["take_profit_pct"], idea["side"]
    if tp is None or side not in ("long", "short"):
        return Decision("見送り", "未対応", "数字(利益確定の%・向き)がない案のため調べられない")
    try:
        code = interval_code(idea["timeframe"])
    except ValueError:
        return Decision("見送り", "未対応", f"時間の単位「{idea['timeframe']}」を扱えない")
    if idea["entry_kind"] not in KINDS:
        return Decision("見送り", "ルール不成立", UNCLASSIFIED_REASON)

    # 1. 手数料負け(データ不要)
    cost = 2 * hl["taker_fee_rate"] * 100 + hl["slippage_pct"]
    mult = settings["fee_multiple"]
    if tp < mult * cost:
        return Decision("見送り", "手数料負け",
                        f"値幅{tp:g}%が往復の手数料+滑り{cost:.2f}%の{mult:g}倍({mult * cost:.2f}%)未満")

    # 2・3 は取引所のデータを使う。取れなければ保留(ReaderError をそのまま上へ)
    decimals = market.size_decimals()
    symbol = _resolve(idea, market)
    if symbol is None:
        return Decision("見送り", "未対応", f"{idea['symbol']}はHyperliquidの無期限先物にない")
    step = INTERVAL_MS[code]
    now = market.now()
    since = now - inv["days"] * DAY_MS
    candles = market.candles(symbol, code, since - WARMUP_BARS * step, now)
    if not candles:
        raise ReaderError(f"{symbol} {code} の価格が取れなかった")

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

    return _capital(symbol, candles[-1].c, decimals[symbol]) or Decision("継続")


def run(conn: sqlite3.Connection, market: Market, *, limit: int | None = None) -> InvestigateReport:
    cfg = config.load_config()["investigate"]
    settings = {}       # 型ごとの今の数字(週1回の棚卸しで変わる)
    rows = conn.execute(
        """SELECT * FROM ideas WHERE (stage = '探索' AND verdict = '案登録') OR (stage = '調査' AND verdict = '保留')
           ORDER BY id LIMIT ?""", (limit or cfg["max_per_day"],)).fetchall()
    rep = InvestigateReport()
    reasons: list[ai_gateway.BatchItem] = []
    for idea in rows:
        try:
            if idea["type"] not in settings:
                settings[idea["type"]] = tuning.current(conn, idea["type"])
            d = check(idea, market, settings[idea["type"]])
        except ReaderError:
            holds = (idea["hold_count"] or 0) + 1
            conn.execute("UPDATE ideas SET hold_count = ? WHERE id = ?", (holds, idea["id"]))
            if holds >= cfg["max_holds"]:
                d = Decision("見送り", "未対応", f"外部データが{holds}回取れなかった")
            else:
                db.set_idea_state(conn, idea["id"], "調査", "保留")
                rep.held.append(idea["id"])
                continue
        memo.record_investigation(conn, idea["id"], idea["type"], d.verdict, d.tag)
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
