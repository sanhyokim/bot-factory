"""paper評価(SPEC 5.5)。AIは使わない。計算だけ。

取引記録を時間順にたどり、途中の不合格条件(連敗・落ち込み・機会なし)と、
完走(14日以上かつ30回以上)の後の合格条件を判定する。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from .. import config
from ..engine import SimResult, SimTrade
from ..models import JST

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


@dataclass
class PaperResult:
    status: str                 # 合格 / 不合格 / 未完走
    tag: str | None             # 不合格の札(SPEC 6.2)
    reason: str                 # 一言の理由(80字以内)
    trades: list[SimTrade] = field(default_factory=list)   # 判定に使った取引
    start_t: int = 0
    end_t: int = 0              # 判定した時点(完走した時点、または不合格になった時点)
    n: int = 0
    total_net: float = 0.0
    avg_net: float = 0.0
    profit_factor: float | None = None
    max_drawdown: float = 0.0
    max_losing_streak: int = 0
    days: float = 0.0

    @property
    def verdict(self) -> tuple[str, str]:
        """案の(工程, 判定)。SPEC 5.5 の判定語に合わせる。"""
        return {"合格": ("paper評価", "仮説を支持"), "不合格": ("否決", "見送り"),
                "未完走": ("paper評価", "継続")}[self.status]


def _stats(trades: list[SimTrade]) -> dict:
    cum = peak = dd = 0.0
    streak = max_streak = 0
    gains = losses = 0.0
    for t in trades:
        cum += t.net_usd
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        if t.net_usd < 0:
            streak += 1
            losses += -t.net_usd
        else:
            streak = 0
            gains += t.net_usd
        max_streak = max(max_streak, streak)
    n = len(trades)
    return {"n": n, "total_net": cum, "avg_net": cum / n if n else 0.0,
            "profit_factor": (gains / losses) if losses > 0 else None,
            "max_drawdown": dd, "max_losing_streak": max_streak}


def _first_failure(sim: SimResult, until: int, cfg: dict) -> tuple[int, str, str] | None:
    """until までに起きた最初の途中不合格を (時刻, 札, 理由) で返す。"""
    events: list[tuple[int, str, str]] = []
    cum = peak = 0.0
    streak = 0
    for t in sim.trades:
        if t.exit_t > until:
            break
        cum += t.net_usd
        peak = max(peak, cum)
        streak = streak + 1 if t.net_usd < 0 else 0
        if streak >= cfg["max_losing_streak"]:
            events.append((t.exit_t, "連敗", f"連敗が{streak}回に達した"))
            break
    cum = peak = 0.0
    for t in sim.trades:
        if t.exit_t > until:
            break
        cum += t.net_usd
        peak = max(peak, cum)
        if peak - cum > cfg["max_drawdown_usd"]:
            events.append((t.exit_t, "落ち込み",
                           f"落ち込みが{peak - cum:.1f}ドルで上限{cfg['max_drawdown_usd']}ドルを超えた"))
            break
    gap = cfg["no_signal_hours"] * HOUR_MS
    last = sim.start_t
    for s in sim.signal_times + [until]:
        if s - last > gap and last + gap <= until:
            events.append((last + gap, "機会なし", f"{cfg['no_signal_hours']}時間、入る条件が一度も成立しなかった"))
            break
        last = max(last, s)
    return min(events) if events else None


def evaluate(sim: SimResult) -> PaperResult:
    cfg = config.load_config()["paper"]
    min_span = cfg["min_days"] * DAY_MS
    closed = sorted(sim.trades, key=lambda t: t.exit_t)
    # 完走する時点 = 14日経過 と 30回目の決着 の遅い方
    done_t = sim.start_t + min_span
    if len(closed) >= cfg["min_trades"]:
        done_t = max(done_t, closed[cfg["min_trades"] - 1].exit_t)
    completed = len(closed) >= cfg["min_trades"] and done_t <= sim.end_t
    until = done_t if completed else sim.end_t

    fail = _first_failure(SimResult(trades=closed, signal_times=sim.signal_times, start_t=sim.start_t,
                                    end_t=sim.end_t), until, cfg)
    if fail is not None:
        until = fail[0]
    used = [t for t in closed if t.exit_t <= until]
    res = PaperResult(status="未完走", tag=None, reason="", trades=used, start_t=sim.start_t, end_t=until,
                      days=(until - sim.start_t) / DAY_MS, **_stats(used))
    if fail is not None:
        res.status, res.tag, res.reason = "不合格", fail[1], fail[2]
        return res
    if not completed:
        res.reason = (f"{res.days:.1f}日・{res.n}回で期間が終わった"
                      f"(完走は{cfg['min_days']}日以上かつ{cfg['min_trades']}回以上)")
        return res

    # 完走後の合格条件。欠けた最初の条件の札を付ける
    pf = res.profit_factor
    if res.avg_net < cfg["pass_avg_net_usd"]:
        res.status, res.tag = "不合格", "利益不足"
        res.reason = f"1取引平均{res.avg_net:.2f}ドルで基準{cfg['pass_avg_net_usd']}ドル未満"
    elif pf is not None and pf < cfg["pass_profit_factor"]:
        res.status, res.tag = "不合格", "損益比不足"
        res.reason = f"利益÷損失が{pf:.2f}で基準{cfg['pass_profit_factor']}未満"
    elif res.max_drawdown > cfg["max_drawdown_usd"]:
        res.status, res.tag = "不合格", "落ち込み"
        res.reason = f"落ち込み{res.max_drawdown:.1f}ドルで上限{cfg['max_drawdown_usd']}ドル超"
    elif res.max_losing_streak >= cfg["max_losing_streak"]:
        res.status, res.tag = "不合格", "連敗"
        res.reason = f"連敗の最大{res.max_losing_streak}回"
    else:
        res.status = "合格"
        pf_text = "損失なし" if pf is None else f"{pf:.2f}"
        res.reason = f"{res.n}回・平均{res.avg_net:.2f}ドル・利益÷損失{pf_text}・落ち込み{res.max_drawdown:.1f}ドル"
    return res


def _jst(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, JST).isoformat(timespec="seconds")


def save_trades(conn: sqlite3.Connection, bot_id: str, trades: list[SimTrade], kind: str = "paper") -> None:
    """取引記録(SPEC 4.4)に書く。日時は日本時間。"""
    conn.executemany(
        """INSERT INTO trades(bot_id, kind, entry_at, entry_price, entry_qty, exit_at, exit_price, exit_qty,
               fee_usd, funding_usd, net_pnl_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [(bot_id, kind, _jst(t.entry_t), t.entry_price, t.qty, _jst(t.exit_t), t.exit_price, t.qty,
          t.fee_usd, t.funding_usd, t.net_usd) for t in trades],
    )
    conn.commit()
