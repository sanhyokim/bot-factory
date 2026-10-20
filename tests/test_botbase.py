from pathlib import Path

import pytest

from factory.botbase import BotRejected, History, load_bot, static_check
from tests.helpers import flat, write_bot

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("bad", [
    "import os\ndef entry_signal(h): return None",
    "from subprocess import run\ndef entry_signal(h): return None",
    "def entry_signal(h):\n    open('x', 'w')\n    return None",
    "def entry_signal(h):\n    return h.__class__",
    "def entry_signal(h):\n    return getattr(h, 'x')",
    "def entry_signal(h):\n    return eval('1')",
    "def entry_signal(h):\n    __import__('os')\n    return None",
    "print('top level code')\ndef entry_signal(h): return None",
    "def exit_signal(h, p): return False",          # entry_signal がない
])
def test_static_check_rejects(bad):
    with pytest.raises(BotRejected):
        static_check(bad)


def test_template_and_sample_load():
    assert load_bot(ROOT / "bots" / "_template").spec.symbol == "BTC"
    bot = load_bot(ROOT / "bots" / "sample_breakout")
    assert bot.spec.timeframe == "15m" and bot.exit_signal is None


@pytest.mark.parametrize("field,value", [("leverage", 2), ("max_positions", 2), ("take_profit_pct", 0)])
def test_yaml_rules(tmp_path, field, value):
    d = write_bot(tmp_path, "def entry_signal(h): return None", **{field: value})
    with pytest.raises(BotRejected):
        load_bot(d)


def test_history_hides_future():
    h = History(flat(10), 3)
    assert len(h) == 3 and h[-1].t == flat(10)[2].t
    with pytest.raises(IndexError):
        h[3]
    assert len(h[:]) == 3 and len(h.closes(50)) == 3


def test_strategy_cannot_import_at_runtime(tmp_path):
    d = write_bot(tmp_path, "import math\ndef entry_signal(h):\n    return None if math.isfinite(1.0) else 'long'")
    assert load_bot(d).entry_signal(History(flat(1), 1)) is None


def test_symbol_spelling(tmp_path):
    from tests.helpers import write_bot

    assert load_bot(write_bot(tmp_path, "def entry_signal(h):\n    return None\n", symbol="btc")).spec.symbol == "BTC"
    assert load_bot(write_bot(tmp_path, "def entry_signal(h):\n    return None\n", symbol="kPEPE")).spec.symbol == "kPEPE"
