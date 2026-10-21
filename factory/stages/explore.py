"""探索(SPEC 5.1): 順位表 → 財布の取引履歴 → 絞り込み → Haikuで仮説3つ → 登録前照合 → 案登録。

AIを使うのは4番(仮説づくり)だけ。一括処理(半額)で送り、答えは collect で受け取って登録する。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import ai_gateway, config, memo
from ..hyperliquid.reader import INTERVAL_MS, Fill, ReaderError, TooManyFills
from ..market import DAY_MS, Market
from ..models import JST, Idea, Memo, now_jst
from ..patterns import KINDS, UNCLASSIFIED, UNCLASSIFIED_REASON, describe, finite

PURPOSE = "explore"
EPS = 1e-9
SCAN_METHOD = 2     # 絞り込みの数え方の版。数え方を直したら上げる(前の版で調べた財布は7日を待たずに調べ直す)
TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]

HYPOTHESIS = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "description": "銘柄(例: BTC)。取引履歴にある無期限先物の銘柄"},
        "timeframe": {"type": "string", "enum": TIMEFRAMES, "description": "判断に使うローソク足の長さ"},
        "side": {"type": "string", "enum": ["long", "short"], "description": "long=買い / short=売り"},
        "entry_kind": {"type": "string", "enum": [*KINDS, UNCLASSIFIED], "description": "入る条件の種類"},
        "n": {"type": "integer", "description": "入る条件の本数(例: 直前20本なら20)"},
        "x": {"type": "number", "description": "入る条件の%または倍率(使わない種類は0)"},
        "take_profit_pct": {"type": "number", "description": "利益確定の幅(%)。例: 1.5"},
        "stop_loss_pct": {"type": "number", "description": "損切りの幅(%)。例: 0.8"},
        "summary": {"type": "string", "description": "1〜2文。どの銘柄で、どんな場面で入り、どう出るか"},
        "hypothesis": {"type": "string", "description": "仮説の本文(300字以内)。銘柄・時間の単位・入る条件・出る条件を含む"},
    },
    "required": ["symbol", "timeframe", "side", "entry_kind", "n", "x", "take_profit_pct", "stop_loss_pct",
                 "summary", "hypothesis"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {"hypotheses": {"type": "array", "items": HYPOTHESIS}},
    "required": ["hypotheses"],
    "additionalProperties": False,
}

SYSTEM = (
    "あなたは暗号資産の無期限先物(Hyperliquid)の売買を分析する担当。"
    "儲けている財布の取引履歴から、どんな場面で入り、どんな場面で出ているかの仮説を立てる。"
    "仮説は、ローソク足と資金調達料だけで判定できる数字の条件にする。"
    "入る条件の種類(entry_kind)は次から1つ選ぶ。nとxはその種類の数字:\n"
    + "\n".join(f"- {k}: {v}" for k, v in KINDS.items())
    + f"\n上のどれにも当てはまらない時は「{UNCLASSIFIED}」を選ぶ。"
    "資金50〜500ドル・レバレッジ1倍・同時に1つだけ持つ前提で真似できる仮説にする。"
)


@dataclass
class RoundTrip:
    coin: str
    side: str           # long / short
    entry_t: int
    entry_px: float
    exit_t: int
    exit_px: float
    size: float         # 最大の保有数量
    pnl: float          # 確定した損益(手数料は別)
    fee: float

    @property
    def notional(self) -> float:
        return self.size * self.entry_px

    @property
    def pct(self) -> float:
        sign = 1 if self.side == "long" else -1
        return sign * (self.exit_px / self.entry_px - 1) * 100


@dataclass
class WalletCheck:
    address: str
    ok: bool
    reason: str
    trips: list[RoundTrip] = field(default_factory=list)
    net: float = 0.0
    max_dd_pct: float = 0.0
    median_notional: float = 0.0


def same_qty(a: float, b: float) -> bool:
    return abs(a - b) <= EPS * max(1.0, abs(a), abs(b))


def ordered(fills: list[Fill]) -> list[Fill]:
    """時刻の順。同じ時刻の約定(1つの注文が分かれて約定したもの)は、取引所が返す順が前後することがあるので、
    約定の直前の保有の並び(買いは小さい順、売りは大きい順)でそろえる。"""
    return sorted(fills, key=lambda f: (f.t, f.start_position if f.side == "B" else -f.start_position))


def round_trips(fills: list[Fill]) -> list[RoundTrip]:
    """約定を「入ってから出るまで」の取引にまとめる。期間の前から持っていた分は数えない。

    保有の数量は、約定ごとに取引所が返す「約定の直前の保有」に合わせ直す。足し引きだけで追うと、
    順の前後や欠けた約定で一度ずれた銘柄は、その後ずっと「0に戻らない」=取引0回になるため。
    """
    state: dict[str, dict] = {}
    trips: list[RoundTrip] = []
    for f in ordered(fills):
        st = state.setdefault(f.coin, {"pos": f.start_position, "trade": None})
        pos, tr = st["pos"], st["trade"]
        if not same_qty(pos, f.start_position):
            pos = f.start_position
            if tr is not None and (abs(pos) < EPS or (pos > 0) != (tr["side"] == "long")):
                tr = None       # 見えない約定で手仕舞い・向きの入れ替えがあった。その取引は数えない
        delta = f.sz if f.side == "B" else -f.sz
        if abs(pos) < EPS:
            tr = {"side": "long" if delta > 0 else "short", "entry_t": f.t, "cost": f.px * f.sz, "qty": f.sz,
                  "size": f.sz, "exit_cost": 0.0, "exit_qty": 0.0, "pnl": 0.0, "fee": f.fee}
        elif (delta > 0) == (pos > 0):
            if tr is not None:
                tr["cost"] += f.px * f.sz
                tr["qty"] += f.sz
                tr["size"] = max(tr["size"], abs(pos + delta))
                tr["fee"] += f.fee
        else:
            closing = min(abs(delta), abs(pos))
            if tr is not None:
                tr["exit_cost"] += f.px * closing
                tr["exit_qty"] += closing
                tr["pnl"] += f.closed_pnl
                tr["fee"] += f.fee * closing / f.sz
            if abs(delta) >= abs(pos) - EPS:
                if tr is not None and tr["exit_qty"] > 0:
                    trips.append(RoundTrip(f.coin, tr["side"], tr["entry_t"], tr["cost"] / tr["qty"], f.t,
                                           tr["exit_cost"] / tr["exit_qty"], tr["size"], tr["pnl"], tr["fee"]))
                tr = None
                rest = abs(delta) - abs(pos)
                if rest > EPS:    # 売り越し・買い越しで向きが変わった
                    tr = {"side": "long" if delta > 0 else "short", "entry_t": f.t, "cost": f.px * rest,
                          "qty": rest, "size": rest, "exit_cost": 0.0, "exit_qty": 0.0, "pnl": 0.0,
                          "fee": f.fee * rest / f.sz}
        new = pos + delta
        st["pos"] = 0.0 if abs(new) < EPS else new
        st["trade"] = tr
    trips.sort(key=lambda t: t.exit_t)
    return trips


def check_wallet(address: str, fills: list[Fill], start_equity: float) -> WalletCheck:
    """SPEC 5.1 の3の絞り込み。"""
    cfg = config.load_config()["explore"]
    trips = round_trips(fills)
    net = sum(f.closed_pnl - f.fee for f in fills)
    eq = peak = max(start_equity, 1.0)
    dd = 0.0
    for f in sorted(fills, key=lambda f: f.t):
        eq += f.closed_pnl - f.fee
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 1.0)
    median = statistics.median(t.notional for t in trips) if trips else 0.0
    res = WalletCheck(address, False, "", trips, net, dd * 100, median)
    if len(trips) < cfg["min_trades"]:
        res.reason = f"取引{len(trips)}回(基準{cfg['min_trades']}回以上)"
    elif net <= 0:
        res.reason = f"純利益{net:+.0f}ドル"
    elif res.max_dd_pct > cfg["max_drawdown_pct"]:
        res.reason = f"落ち込み{res.max_dd_pct:.0f}%(基準{cfg['max_drawdown_pct']}%以内)"
    elif median > cfg["max_median_notional_usd"]:
        res.reason = f"取引額の中央値{median:.0f}ドル(基準{cfg['max_median_notional_usd']}ドル以下)"
    else:
        res.ok, res.reason = True, "条件を満たした"
    return res


def _hm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, JST).strftime("%m/%d %H:%M")


def wallet_prompt(address: str, trips: list[RoundTrip]) -> str:
    cfg = config.load_config()["explore"]
    recent = trips[-cfg["trades_for_ai"]:]
    lines = [f"{i + 1} {t.coin} {'買い' if t.side == 'long' else '売り'} 入{_hm(t.entry_t)} {t.entry_px:.6g}"
             f" → 出{_hm(t.exit_t)} {t.exit_px:.6g} {t.pct:+.2f}% 保有{(t.exit_t - t.entry_t) / 3_600_000:.1f}時間"
             for i, t in enumerate(recent)]
    return (f"財布 {address} の直近{len(recent)}件の取引(時刻は日本時間)を読み、どんな場面で入り、"
            f"どんな場面で出ているかの仮説を{cfg['hypotheses_per_wallet']}つ書け。"
            "各仮説は300字以内で、銘柄・時間の単位・入る条件・出る条件(利益確定と損切りの%)を必ず含めること。\n"
            "番号 銘柄 向き 入った時刻 価格 → 出た時刻 価格 値幅 保有時間\n" + "\n".join(lines))


@dataclass
class ScanReport:
    listed: int = 0
    skipped_recent: int = 0
    checked: int = 0
    passed: list[WalletCheck] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)
    errors: int = 0


def _reason_key(reason: str) -> str:
    return reason.split("(")[0].rstrip("0123456789+-.ドル回%").strip() or reason


def candidate_rules() -> list[tuple[str, Callable[[dict], bool]]]:
    """順位表の候補を選ぶ条件(上から順にかける)。名前は診断(explore-check)の表示に使う。"""
    cfg = config.load_config()["explore"]
    low, high = cfg["candidate_account_usd"]
    vlow, vhigh = cfg["candidate_volume_usd"]
    return [
        ("損益プラス", lambda r: r["month_pnl"] > 0),
        (f"資金{low:,}〜{high:,}ドル", lambda r: low <= r["account_value"] <= high),
        (f"30日の取引量{vlow:,}〜{vhigh:,}ドル", lambda r: vlow <= r["month_volume"] <= vhigh),
        # 30日の損益が今の資金より大きい財布は、利益を引き出しながら大きく張っている(10/21 の診断で上位30件すべて)。
        # 落ち込みの計算(5.1 の3)も30日前の資金を「今の資金 − 30日の損益」で推すので、これがプラスの財布に限る
        ("30日前の資金がプラス", lambda r: r["account_value"] - r["month_pnl"] > 0),
    ]


def candidates(board: list[dict], *, steps: list[tuple[str, int]] | None = None) -> list[dict]:
    """順位表から、真似できる大きさで取引していそうな財布を選び、30日の損益の順に並べる(SPEC 5.1 の1)。

    順位表の損益の上位は資金数百万〜数億ドルの財布と高頻度の取引ばかりで、取引額の中央値2000ドル以下(5.1 の3)を
    満たす財布が1件もなかった(2026-10-20 の診断)。そこで先に順位表の数字(資金・30日の取引量・損益)で候補を絞る。
    steps を渡すと、条件ごとに残った数を入れる。
    """
    rows = board
    for name, ok in candidate_rules():
        rows = [r for r in rows if ok(r)]
        if steps is not None:
            steps.append((name, len(rows)))
    return sorted(rows, key=lambda r: -r["month_pnl"])


def scan(conn: sqlite3.Connection, market: Market, *, progress=None) -> ScanReport:
    """候補の財布を損益の順に、7日以内に調べた財布は飛ばして1日に最大300件調べ、
    絞り込みを通った財布を最大30件返す(AIは使わない)。
    progress(調べた数, 調べる上限) を渡すと、50財布ごとに呼ぶ(読み終わるまで時間がかかることがあるため)。"""
    cfg = config.load_config()["explore"]
    reader = market.reader
    now = market.now()
    start = now - cfg["days"] * DAY_MS
    rows = candidates(reader.leaderboard())
    limit = min(cfg["top_wallets"], len(rows))
    perps = set(market.size_decimals())
    cutoff = (now_jst() - timedelta(days=cfg["rescan_days"])).isoformat(timespec="seconds")
    recent = {r[0] for r in conn.execute("SELECT address FROM wallet_scans WHERE scanned_at >= ? AND method = ?",
                                         (cutoff, SCAN_METHOD))}
    rep = ScanReport(listed=len(rows))
    for r in rows:
        if len(rep.passed) >= cfg["max_wallets"] or rep.checked >= limit:
            break
        addr = r["address"]
        if addr in recent:
            rep.skipped_recent += 1
            continue
        try:
            fills = reader.user_fills(addr, start, now, max_pages=cfg["max_fill_pages"])
        except TooManyFills:
            fills, why = None, "約定が多すぎる(高頻度の取引)"
        except ReaderError:
            rep.errors += 1
            if rep.errors >= 5:     # 取引所が不調。今日はここまで
                break
            continue
        conn.execute("INSERT OR REPLACE INTO wallet_scans(address, scanned_at, method) VALUES (?, ?, ?)",
                     (addr, now_jst().isoformat(timespec="seconds"), SCAN_METHOD))
        conn.commit()
        rep.checked += 1
        if progress and rep.checked % 50 == 0 and rep.checked < limit:
            progress(rep.checked, limit)
        if fills is None:
            rep.reasons[why] = rep.reasons.get(why, 0) + 1
            continue
        fills = [f for f in fills if f.coin in perps]
        # 30日前の資金 ≒ 今の資金 − 30日の損益(入出金があると崩れるので、その時は今の資金で代える)
        base = r["account_value"] - r["month_pnl"]
        check = check_wallet(addr, fills, base if base > 0 else r["account_value"])
        if check.ok:
            rep.passed.append(check)
        else:
            key = _reason_key(check.reason)
            rep.reasons[key] = rep.reasons.get(key, 0) + 1
    return rep


def submit(conn: sqlite3.Connection, wallets: list[WalletCheck]) -> tuple[str | None, int]:
    """絞った財布をHaikuに一括で送る。戻り値: (一括処理ID, 送った財布の数)。"""
    cfg = config.load_config()["explore"]
    today = now_jst().date().isoformat()
    used = sum(json.loads(r[0]).get("note", {}).get("chars", 0) for r in conn.execute(
        "SELECT items_json FROM ai_batches WHERE purpose = ? AND substr(created_at, 1, 10) = ?", (PURPOSE, today)))
    items, chars = [], 0
    for w in wallets:
        prompt = wallet_prompt(w.address, w.trips)
        if used + chars + len(prompt) + len(SYSTEM) > cfg["daily_input_chars"]:
            break       # SPEC 5.1 費用の上限: Haikuの入力500万文字/日
        chars += len(prompt) + len(SYSTEM)
        items.append(ai_gateway.BatchItem(key=w.address, prompt=prompt, system=SYSTEM,
                                          meta={"wallet": w.address}))
    batch_id = ai_gateway.submit_batch(conn, "explore", PURPOSE, items, schema=SCHEMA, note={"chars": chars})
    return batch_id, len(items)


def to_idea(h: dict, wallet: str) -> Idea | None:
    """AIの仮説1つを案にする。数字が読めないものは捨てる。"""
    symbol = str(h.get("symbol", "")).strip()       # 書き方(kPEPE など)は調査で取引所に合わせる
    tf = str(h.get("timeframe", "")).strip()
    kind = str(h.get("entry_kind", "")).strip()
    side = str(h.get("side", "")).strip()
    if not symbol or tf not in INTERVAL_MS or kind not in KINDS or side not in ("long", "short"):
        return None
    if not all(finite(h.get(k)) for k in ("n", "x", "take_profit_pct", "stop_loss_pct")):
        return None
    summary = str(h.get("summary", "")).strip() or f"{symbol} {tf}: {describe(kind, side, h['n'], h['x'])}"
    return Idea(summary=summary[:150], hypothesis=str(h.get("hypothesis", "")).strip()[:300], source="wallet",
                wallet=wallet, symbol=symbol, timeframe=tf, entry_kind=kind, side=side,
                entry_params=json.dumps({"n": int(h["n"]), "x": float(h["x"])}),
                take_profit_pct=float(h["take_profit_pct"]), stop_loss_pct=float(h["stop_loss_pct"]))


@dataclass
class CollectReport:
    wallets: int = 0
    registered: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)   # 分類できず否決メモにした仮説
    unreadable: int = 0
    errors: list[str] = field(default_factory=list)


def reject_unclassifiable(conn: sqlite3.Connection, h: dict) -> str:
    """入る条件の種類を7語のどれにも分類できない仮説は、案にせず否決メモだけ残す(指示書A 項目2)。"""
    summary = str(h.get("summary") or h.get("hypothesis") or "").strip()[:150] or "(要約なし)"
    return memo.add_memo(conn, Memo(stage="探索", tag="ルール不成立", reason=UNCLASSIFIED_REASON,
                                    idea_summary=summary, symbol=str(h.get("symbol", "")).strip(),
                                    timeframe=str(h.get("timeframe", "")).strip(), source="wallet"))


def reject_registered_other(conn: sqlite3.Connection) -> list[str]:
    """以前の版で登録された「その他」の案を、同じ札で否決する。試運転中のBOTがあれば止める。"""
    ids = [r["id"] for r in conn.execute("SELECT id FROM ideas WHERE entry_kind = 'その他' AND stage != '否決'")]
    for idea_id in ids:
        conn.execute("UPDATE bots SET status = '否決', updated_at = ? WHERE rule_id = ? AND status != '否決'",
                     (now_jst().isoformat(timespec="seconds"), idea_id))
        memo.memo_for_idea(conn, idea_id, "探索", "ルール不成立", UNCLASSIFIED_REASON)
    return ids


def collect(conn: sqlite3.Connection, *, wait_seconds: float = 0) -> CollectReport:
    """終わった一括処理の仮説を、登録前照合にかけて案登録する(SPEC 5.1 の5・6)。"""
    per_wallet = config.load_config()["explore"]["hypotheses_per_wallet"]
    rep = CollectReport()
    for out in ai_gateway.collect_batches(conn, PURPOSE, wait_seconds=wait_seconds):
        rep.wallets += 1
        if out.error or out.result is None or out.result.data is None:
            rep.errors.append(f"{out.key[:10]}…: {out.error}")
            continue
        for h in (out.result.data.get("hypotheses") or [])[:per_wallet]:
            if str(h.get("entry_kind", "")).strip() not in KINDS:
                rep.unclassified.append(reject_unclassifiable(conn, h))
                continue
            idea = to_idea(h, out.meta.get("wallet", out.key))
            if idea is None:
                rep.unreadable += 1
                continue
            idea_id, memo_id = memo.register_idea(conn, idea)
            if idea_id:
                rep.registered.append(idea_id)
            else:
                rep.excluded.append(memo_id)
    return rep
