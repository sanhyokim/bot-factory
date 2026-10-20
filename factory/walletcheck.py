"""探索の絞り込みの診断(開発用)。順位表の財布を少し読み、どの条件で何件落ちるかを数字で見る。

記録はデータベースに残さない(「7日間は再分析しない」の対象にもしない)。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config
from .hyperliquid.reader import Reader, ReaderError, TooManyFills, now_ms
from .market import DAY_MS
from .stages import explore


@dataclass
class Row:
    address: str
    account_value: float
    month_pnl: float
    month_roi: float
    fills: int = 0
    check: explore.WalletCheck | None = None
    drift: int = 0          # 前の数え方(足し引きだけで保有を追う)で、取引所の保有とずれた約定の数
    note: str = ""          # 読めなかった・約定が多すぎる、など


def naive_drift(fills) -> int:
    """前の数え方で保有を追った時に、約定の直前の保有が取引所の値と合わなかった約定の数。"""
    pos: dict[str, float] = {}
    n = 0
    for f in sorted(fills, key=lambda f: f.t):
        p = pos.setdefault(f.coin, f.start_position)
        if not explore.same_qty(p, f.start_position):
            n += 1
        pos[f.coin] = p + (f.sz if f.side == "B" else -f.sz)
    return n


def pick(board: list[dict], n: int) -> tuple[list[dict], list[dict]]:
    """30日の損益の順で上位 n 件と、それ以外から30日の利益率の順で上位 n 件。"""
    winners = [r for r in board if r["month_pnl"] > 0]
    by_pnl = sorted(winners, key=lambda r: -r["month_pnl"])[:n]
    taken = {r["address"] for r in by_pnl}
    by_roi = sorted((r for r in winners if r["address"] not in taken), key=lambda r: -r["month_roi"])[:n]
    return by_pnl, by_roi


def inspect(reader: Reader, rows: list[dict], *, end_ms: int | None = None) -> list[Row]:
    cfg = config.load_config()["explore"]
    end = end_ms or now_ms()
    start = end - cfg["days"] * DAY_MS
    perps = set(reader.size_decimals())
    out = []
    for r in rows:
        row = Row(r["address"], r["account_value"], r["month_pnl"], r["month_roi"])
        out.append(row)
        try:
            fills = reader.user_fills(r["address"], start, end, max_pages=cfg["max_fill_pages"])
        except TooManyFills:
            row.note = f"約定が{cfg['max_fill_pages'] * 2000}件を超える(高頻度の取引)"
            continue
        except ReaderError as e:
            row.note = f"読めなかった: {e}"
            continue
        fills = [f for f in fills if f.coin in perps]
        base = r["account_value"] - r["month_pnl"]
        row.fills = len(fills)
        row.drift = naive_drift(fills)
        row.check = explore.check_wallet(r["address"], fills, base if base > 0 else r["account_value"])
    return out


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def report(title: str, rows: list[Row]) -> list[str]:
    cfg = config.load_config()["explore"]
    lines = [f"[{title}]"]
    for i, w in enumerate(rows, 1):
        head = (f"{i:>2} {_short(w.address)} 資金{w.account_value:,.0f}ドル・30日の損益{w.month_pnl:+,.0f}ドル"
                f"({w.month_roi * 100:+.0f}%)")
        c = w.check
        if c is None:
            lines.append(f"{head} → {w.note}")
            continue
        verdict = "通過" if c.ok else c.reason
        lines.append(f"{head} 約定{w.fills}件・取引{len(c.trips)}回・純利益{c.net:+,.0f}ドル・"
                     f"落ち込み{c.max_dd_pct:.0f}%・取引額の中央値{c.median_notional:,.0f}ドル・"
                     f"前の数え方のずれ{w.drift}件 → {verdict}")
    checked = [w for w in rows if w.check is not None]
    each = {
        f"取引{cfg['min_trades']}回以上": sum(len(w.check.trips) >= cfg["min_trades"] for w in checked),
        "純利益がプラス": sum(w.check.net > 0 for w in checked),
        f"落ち込み{cfg['max_drawdown_pct']}%以内": sum(w.check.max_dd_pct <= cfg["max_drawdown_pct"] for w in checked),
        f"中央値{cfg['max_median_notional_usd']}ドル以下":
            sum(0 < w.check.median_notional <= cfg["max_median_notional_usd"] for w in checked),
    }
    lines.append(f"  読めた {len(checked)}/{len(rows)}財布・全部の条件を通過 {sum(w.check.ok for w in checked)}")
    lines.append("  条件ごとに満たした数: " + "・".join(f"{k} {v}" for k, v in each.items()))
    drifted = [w for w in checked if w.drift]
    lines.append(f"  前の数え方で保有がずれた財布 {len(drifted)}"
                 f"(ずれた約定 {sum(w.drift for w in checked)}/{sum(w.fills for w in checked)}件)")
    return lines


def run(reader: Reader, n: int) -> str:
    by_pnl, by_roi = pick(reader.leaderboard(), n)
    lines = [f"探索の絞り込みの診断(開発用・記録は残さない)。各{n}財布"]
    lines += report("30日の損益の順で上位", inspect(reader, by_pnl))
    lines += [""] + report("参考: 30日の利益率の順で上位(上と重ならない財布)", inspect(reader, by_roi))
    return "\n".join(lines)
