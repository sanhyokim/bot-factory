"""反証(SPEC 5.6)。paper評価に合格したBOTを「たまたまではないか」の3条件で検査する。

手数料2倍検査は、手数料と滑りの両方を2倍にして計算し直す(第2版から。滑りは取引記録の slippage_usd)。

計算はプログラムで行う。AI(Sonnet)には判定の説明文(200字)だけを頼む(explain=True の時)。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .. import ai_gateway, config
from ..engine import SimTrade


@dataclass
class RefuteCheck:
    name: str
    ok: bool
    tag: str        # 落ちた時の札
    detail: str


@dataclass
class RefuteResult:
    ok: bool
    checks: list[RefuteCheck] = field(default_factory=list)
    explanation: str = ""

    @property
    def failed(self) -> RefuteCheck | None:
        return next((c for c in self.checks if not c.ok), None)


def refute(trades: list[SimTrade], start_t: int, end_t: int) -> RefuteResult:
    cfg = config.load_config()["refute"]
    mid = start_t + (end_t - start_t) // 2
    first = sum(t.net_usd for t in trades if t.exit_t <= mid)
    second = sum(t.net_usd for t in trades if t.exit_t > mid)
    c1 = RefuteCheck("前後半検査", first > 0 and second > 0, "前後半不一致",
                     f"前半{first:+.2f}ドル・後半{second:+.2f}ドル")

    mult = cfg["fee_multiplier"]
    doubled = sum(t.net_usd - (mult - 1) * (t.fee_usd + t.slippage_usd) for t in trades)
    c2 = RefuteCheck("手数料2倍検査", doubled > 0, "手数料2倍で赤字",
                     f"手数料と滑りを{mult:g}倍にして純利益{doubled:+.2f}ドル")

    total = sum(t.net_usd for t in trades)
    best = max((t.net_usd for t in trades), default=0.0)
    share = best / total if total > 0 else float("inf")
    c3 = RefuteCheck("偏り検査", share < cfg["max_single_share"], "一発依存",
                     f"最大の1取引{best:+.2f}ドルが純利益{total:+.2f}ドルの"
                     + (f"{share * 100:.0f}%" if total > 0 else "全部(合計がマイナス)"))
    checks = [c1, c2, c3]
    return RefuteResult(ok=all(c.ok for c in checks), checks=checks)


def explain(conn: sqlite3.Connection, result: RefuteResult, *, bot_id: str | None = None) -> str:
    """判定の説明文をAI窓口(反証の割り当てモデル)に200字で書かせる。"""
    lines = "\n".join(f"- {c.name}: {'合格' if c.ok else '不合格'}({c.detail})" for c in result.checks)
    prompt = (f"暗号資産BOTの検査結果を、売買に詳しくない人向けに日本語200字以内で説明して。"
              f"総合判定は{'合格' if result.ok else '不合格'}。\n{lines}")
    r = ai_gateway.ask(conn, "refute", prompt, bot_id=bot_id)
    result.explanation = r.text.strip()[:200]
    return result.explanation
