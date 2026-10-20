from factory.stages import paper, refute
from tests.helpers import DAY, HOUR, T0, sim, trade


def spaced(nets, days=15):
    step = days * DAY // len(nets)
    return [trade(T0 + (i + 1) * step, n) for i, n in enumerate(nets)]


def test_pass():
    r = paper.evaluate(sim(spaced([5, -2] * 15)))
    assert r.status == "合格" and r.n == 30 and r.avg_net == 1.5 and r.verdict == ("paper評価", "仮説を支持")


def test_stops_counting_at_completion():
    r = paper.evaluate(sim(spaced([5, -2] * 20, days=20)))
    assert r.status == "合格" and r.n == 30 and r.days >= 14


def test_losing_streak():
    r = paper.evaluate(sim(spaced([5] * 10 + [-1] * 8 + [5] * 12)))
    assert (r.status, r.tag) == ("不合格", "連敗")


def test_drawdown():
    r = paper.evaluate(sim(spaced([-15, 1, -15, 1, -15] + [5] * 25)))
    assert (r.status, r.tag) == ("不合格", "落ち込み")


def test_no_signal_for_72_hours():
    s = sim(spaced([5, -2] * 15))
    s.signal_times = [t for t in s.signal_times if not (T0 + 2 * DAY <= t <= T0 + 6 * DAY)]
    r = paper.evaluate(s)
    assert (r.status, r.tag) == ("不合格", "機会なし")


def test_not_completed():
    r = paper.evaluate(sim(spaced([5, -2] * 5), days=30))
    assert r.status == "未完走" and r.verdict == ("paper評価", "継続")


def test_profit_too_small_and_ratio():
    assert paper.evaluate(sim(spaced([1, -0.4] * 15))).tag == "利益不足"
    assert paper.evaluate(sim(spaced([10, -8] * 15))).tag == "損益比不足"


def test_refute_all_pass():
    trades = spaced([5, -2] * 15)
    r = refute.refute(trades, T0, T0 + 15 * DAY)
    assert r.ok and [c.ok for c in r.checks] == [True, True, True]


def test_refute_each_failure():
    first_half_only = spaced([5] * 15 + [-1] * 15)
    assert refute.refute(first_half_only, T0, T0 + 15 * DAY).failed.tag == "前後半不一致"
    thin = [trade(T0 + (i + 1) * HOUR * 10, 0.3, fee=0.5) for i in range(30)]
    assert refute.refute(thin, T0, T0 + 13 * DAY).failed.tag == "手数料2倍で赤字"
    one_shot = spaced([100] + [-1] * 14 + [1] * 15)
    assert refute.refute(one_shot, T0 - DAY, T0 + 15 * DAY).checks[2].ok is False


def test_save_trades(conn):
    conn.execute("""INSERT INTO rules(id, symbol, timeframe, entry_condition, exit_condition, max_amount_usd,
                    max_trades_per_day) VALUES ('I-00001','BTC','15m','高値抜け','+1%/-0.6%',200,6)""")
    conn.execute("""INSERT INTO bots(id, rule_id, code_path, status, created_at, updated_at)
                    VALUES ('B-0001','I-00001','bots/B-0001','paper評価中','x','x')""")
    paper.save_trades(conn, "B-0001", spaced([5, -2]))
    rows = conn.execute("SELECT * FROM trades").fetchall()
    assert len(rows) == 2 and rows[0]["kind"] == "paper" and rows[0]["entry_at"].endswith("+09:00")
