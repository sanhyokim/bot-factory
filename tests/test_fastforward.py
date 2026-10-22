"""早送りの流れ(自動テスト → paper評価 → 反証 → 表示)が最後まで動くことの確認。

ここでの価格は試験用に作った波形で、実際の相場ではない。実際の相場での確認は
python -m factory paper-ff bots/sample_breakout で行う。
"""
import math
from pathlib import Path

from factory import fastforward
from factory.botrun import load as load_bot
from factory.hyperliquid.reader import Funding
from tests.helpers import DAY, HOUR, STEP, T0, candles

ROOT = Path(__file__).resolve().parent.parent


def wave(days=30):
    rows, price = [], 100.0
    for i in range(days * DAY // STEP):
        nxt = 100 * (1 + 0.03 * math.sin(i / 40)) * (1 + 0.004 * math.sin(i / 3))
        rows.append((price, max(price, nxt) * 1.001, min(price, nxt) * 0.999, nxt))
        price = nxt
    return candles(rows)


def test_sample_bot_runs_end_to_end():
    bot = load_bot(ROOT / "bots" / "sample_breakout")
    data = fastforward.MarketData(wave(), [Funding(T0 + h * HOUR, 0.00001) for h in range(720)], 5)
    out = fastforward.run(bot, data)
    assert all(c.ok for c in out["checks"])
    p = out["paper"]
    assert p.status in ("合格", "不合格", "未完走") and p.n > 0
    assert out["refute"] is not None and len(out["refute"].checks) == 3
    text = fastforward.report(bot, data, out)
    assert "[自動テスト]" in text and "[paper評価(早送り)]" in text and "前後半検査" in text


def test_bot_that_never_enters_stops_at_autotest(tmp_path):
    from tests.helpers import write_bot
    bot = load_bot(write_bot(tmp_path, "def entry_signal(h):\n    return None"))
    out = fastforward.run(bot, fastforward.MarketData(wave(2), [], 5))
    assert [c.ok for c in out["checks"]] == [True, False, True] and out["paper"] is None


def test_bot_that_crashes_is_caught(tmp_path):
    from tests.helpers import write_bot
    bot = load_bot(write_bot(tmp_path, "def entry_signal(h):\n    return h[-1].c / 0"))
    out = fastforward.run(bot, fastforward.MarketData(wave(1), [], 5))
    assert out["checks"][0].ok is False and "ZeroDivisionError" in out["checks"][0].detail


def test_cli_paper_ff(monkeypatch, capsys):
    from factory.__main__ import main

    data = fastforward.MarketData(wave(), [], 5)
    monkeypatch.setattr(fastforward, "fetch", lambda reader, bot, days: data)
    assert main(["paper-ff", str(ROOT / "bots" / "sample_breakout")]) == 0
    out = capsys.readouterr().out
    assert "静的検査: OK" in out and "[反証" in out
    assert main(["check-bot", str(ROOT / "bots" / "sample_breakout")]) == 0
    assert "[paper評価" not in capsys.readouterr().out


def test_cli_reports_rejected_bot(tmp_path, capsys):
    from factory.__main__ import main
    from tests.helpers import write_bot

    d = write_bot(tmp_path, "import os\ndef entry_signal(h):\n    return None")
    assert main(["paper-ff", str(d)]) == 1
    assert "使えないモジュール: os" in capsys.readouterr().err
