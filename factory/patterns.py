"""入る条件の種類(探索のAIが選ぶ語)と、その場面が何回あったかの数え方(SPEC 5.2 の2)。

種類の名前は、否決メモの照合キー「入る条件の種類」にもそのまま使う(SPEC 6.3 用途1)。
数字は2つだけ: n(本数)と x(% または倍率)。向き(買い/売り)は案の side で持つ。
"""
from __future__ import annotations

import bisect
import math

from .hyperliquid.reader import Candle, Funding

KINDS = {
    "高値抜け": "終値が直前n本の高値を上に抜けた",
    "安値割れ": "終値が直前n本の安値を下に割った",
    "平均回帰": "終値がn本平均からx%以上離れた(買いは平均より下、売りは平均より上)",
    "急落": "n本前の終値からx%以上下げた",
    "急騰": "n本前の終値からx%以上上げた",
    "出来高急増": "出来高が直前n本の平均のx倍以上になった",
    "資金調達料": "1時間の資金調達料がx%以上偏った(買いは−x%以下、売りは+x%以上)",
    "その他": "上のどれにも当てはまらない(場面の回数は数えない)",
}
COUNTABLE = tuple(k for k in KINDS if k != "その他")
MAX_N = 1000


def _n(n) -> int:
    try:
        return min(MAX_N, max(1, int(n)))
    except (TypeError, ValueError):
        return 20


def flags(kind: str, side: str, n, x, candles: list[Candle], funding: list[Funding] = (),
          step: int | None = None) -> list[bool]:
    """足ごとに「その足が閉じた時点で条件が成り立っていたか」。"""
    n = _n(n)
    x = abs(float(x or 0))
    out = []
    step = step or (candles[1].t - candles[0].t if len(candles) > 1 else 60_000)
    f_times = [f.t for f in funding]
    for i, c in enumerate(candles):
        ok = False
        if kind == "高値抜け" and i >= n:
            ok = c.c > max(b.h for b in candles[i - n:i])
        elif kind == "安値割れ" and i >= n:
            ok = c.c < min(b.l for b in candles[i - n:i])
        elif kind == "平均回帰" and i >= n - 1:
            mean = sum(b.c for b in candles[i - n + 1:i + 1]) / n
            ok = c.c <= mean * (1 - x / 100) if side == "long" else c.c >= mean * (1 + x / 100)
        elif kind == "急落" and i >= n:
            ok = c.c <= candles[i - n].c * (1 - x / 100)
        elif kind == "急騰" and i >= n:
            ok = c.c >= candles[i - n].c * (1 + x / 100)
        elif kind == "出来高急増" and i >= n:
            avg = sum(b.v for b in candles[i - n:i]) / n
            ok = avg > 0 and c.v >= avg * max(x, 1.0)
        elif kind == "資金調達料" and f_times:
            j = bisect.bisect_right(f_times, c.t + step) - 1
            if j >= 0:
                rate = funding[j].rate * 100
                ok = rate <= -x if side == "long" else rate >= x
        out.append(ok)
    return out


def count_episodes(kind: str, side: str, n, x, candles: list[Candle], funding: list[Funding] = (), *,
                   since: int | None = None, step: int | None = None) -> int | None:
    """条件が成り立ち始めた回数(続いている間は1回と数える)。数えられない種類は None。"""
    if kind not in COUNTABLE or not candles:
        return None
    fl = flags(kind, side, n, x, candles, funding, step)
    count = 0
    for i, ok in enumerate(fl):
        if ok and (i == 0 or not fl[i - 1]) and (since is None or candles[i].t >= since):
            count += 1
    return count


def describe(kind: str, side: str, n, x) -> str:
    side_text = "買い" if side == "long" else "売り"
    text = KINDS.get(kind, kind).replace("n本", f"{_n(n)}本").replace("x%", f"{x}%").replace("x倍", f"{x}倍")
    return f"{text}時に{side_text}"


def finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False
