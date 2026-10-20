"""早送りのpaper評価(SPEC 10章 段階2): 過去の価格でBOTを動かし、paper評価と反証まで一気に判定する。

開発用の機能。実時間を待たずに試すためのもので、本番の判定には使わない。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import autotest, config
from .botbase import Bot
from .engine import jst_date
from .hyperliquid.reader import Candle, Funding, Reader, now_ms
from .stages import paper, refute

DAY_MS = 24 * 3_600_000


@dataclass
class MarketData:
    candles: list[Candle]
    funding: list[Funding]
    size_decimals: int


def fetch(reader: Reader, bot: Bot, days: int, *, end_ms: int | None = None) -> MarketData:
    end = end_ms or now_ms()
    start = end - days * DAY_MS
    candles = reader.candles(bot.spec.symbol, bot.spec.timeframe, start, end)
    if not candles:
        raise ValueError(f"{bot.spec.symbol} の価格が取れなかった(銘柄名を確認)")
    funding = reader.funding_history(bot.spec.symbol, start, end)
    decimals = reader.size_decimals().get(bot.spec.symbol)
    if decimals is None:
        raise ValueError(f"{bot.spec.symbol} はHyperliquidの無期限先物にない")
    return MarketData(candles, funding, decimals)


def run(bot: Bot, data: MarketData) -> dict:
    hl = config.load_config()["hyperliquid"]
    amount = config.load_config()["paper"]["amount_usd"]
    checks, sim = autotest.run(bot, data.candles, data.funding, amount_usd=amount,
                               fee_rate=hl["taker_fee_rate"], size_decimals=data.size_decimals,
                               min_order_usd=hl["min_order_usd"])
    out: dict = {"checks": checks, "paper": None, "refute": None, "refute_is_reference": False}
    if sim is None or not all(c.ok for c in checks):
        return out
    result = paper.evaluate(sim)
    out["paper"] = result
    if result.trades:
        # 反証は合格したBOTだけが受ける工程。不合格・未完走の時は3検査が動くことの確認用に参考として出す
        out["refute"] = refute.refute(result.trades, result.start_t, result.end_t)
        out["refute_is_reference"] = result.status != "合格"
    return out


def report(bot: Bot, data: MarketData, out: dict, *, with_paper: bool = True) -> str:
    c0, c1 = data.candles[0], data.candles[-1]
    lines = [f"BOT {bot.spec.name}: {bot.spec.symbol} {bot.spec.timeframe} "
             f"利益確定+{bot.spec.take_profit_pct}% 損切り−{bot.spec.stop_loss_pct}%",
             f"価格 {jst_date(c0.t)}〜{jst_date(c1.t)} の{len(data.candles)}本・資金調達料{len(data.funding)}件",
             "", "[自動テスト]"]
    for c in out["checks"]:
        lines.append(f"  {'OK ' if c.ok else 'NG '} {c.name}: {c.detail}")
    if not with_paper:
        return "\n".join(lines)
    p = out["paper"]
    if p is None:
        lines.append("自動テストに落ちたため、paper評価は行わない(修理工程へ)")
        return "\n".join(lines)
    pf = "—" if p.profit_factor is None else f"{p.profit_factor:.2f}"
    lines += ["", "[paper評価(早送り)]",
              f"  判定: {p.status}" + (f"(札: {p.tag})" if p.tag else ""),
              f"  理由: {p.reason}",
              f"  {p.days:.1f}日・{p.n}回・純利益{p.total_net:+.2f}ドル・1取引平均{p.avg_net:+.2f}ドル",
              f"  利益÷損失 {pf}・最大の落ち込み {p.max_drawdown:.2f}ドル・最大連敗 {p.max_losing_streak}回"]
    r = out["refute"]
    if r is not None:
        head = "[反証(参考: paper評価に合格していないので本来は行わない)]" if out["refute_is_reference"] else "[反証]"
        lines += ["", head]
        for c in r.checks:
            lines.append(f"  {'OK ' if c.ok else 'NG '} {c.name}: {c.detail}")
        lines.append(f"  判定: {'合格(関所1へ)' if r.ok else '不合格(札: ' + r.failed.tag + ')'}")
    return "\n".join(lines)
