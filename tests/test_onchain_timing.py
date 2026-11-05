"""取られるまでの速さ(Aave の清算): 健康度が1を割ったブロック・遅れ・理由を、偽の接続先で確かめる。"""
import time

import pytest

from factory.onchain import scan_command, timing
from tests.fakes_chain import POOL, WETH, FakeNode
from tests.test_onchain_actors import make_chain, make_scanner

U1, U2, U3, U4, U5 = ("0x" + c * 20 for c in ("11", "22", "33", "44", "55"))
LIQ2 = "0x" + "77" * 20


def crash(block):
    """ブロック 50,000 で担保の値段が落ちて割れる。50,040 で半分清算されるが、まだ1未満。"""
    if block < 50_000:
        return 2_000.0, 1_000.0, 0.8, 1.6
    if block < 50_040:
        return 1_100.0, 1_000.0, 0.8, 0.88
    return 550.0, 500.0, 0.8, 0.88


def interest(block):
    """値段は動かず、借金の利息が積もってブロック 60,001 で割れる。"""
    debt = 1_000.0 + (block - 60_000) * 0.001
    return 1_250.0, debt, 0.8, 1_000.0 / debt


def healthy_until(limit):
    def f(block):
        return (2_000.0, 1_000.0, 0.8, 1.6) if block < limit else (1_100.0, 1_000.0, 0.8, 0.88)
    return f


def setup(conn):
    node = FakeNode(head=100_000)
    node.history = {U1: crash, U2: interest, U3: healthy_until(70_000), U4: healthy_until(80_000),
                    U5: lambda b: (5.0, 10.0, 0.8, 0.4)}
    node.price_changes = [(70_000, WETH, 2_900.0)]          # ブロック 70,000 で WETH の値段が動く
    node.add_liquidation(50_040, borrower=U1)
    node.add_liquidation(50_100, borrower=U1, liquidator=LIQ2)
    node.add_liquidation(60_400, borrower=U2)
    node.add_liquidation(70_000, borrower=U3)
    node.add_liquidation(80_000, borrower=U4, liquidator=LIQ2)
    node.add_liquidation(90_000, borrower=U5)
    make_scanner(conn, node).scan(1, 99_000)
    return node


def measured(conn):
    return {r["borrower"] + "@" + str(r["liq_block"]): r for r in
            conn.execute("SELECT * FROM chain_liquidation_timing").fetchall()}


def test_cross_block_delay_and_cause(conn):
    node = setup(conn)
    timer = timing.Timer(conn, make_chain(node), pool=POOL, max_back=1_000, out=lambda s: None)
    st = timer.run(0)
    assert st["done"] == 6 and st["failed"] == 0
    m = measured(conn)

    a = m[f"{U1}@50040"]                                   # 値段で割れて、40ブロック(10秒)後に取られた
    assert (a["cross_block"], a["delay_blocks"], a["delay_seconds"], a["cause"]) == (50_000, 40, 10, "値段")
    assert a["health"] == pytest.approx(0.88) and a["debt_usd"] == pytest.approx(1_000)

    b = m[f"{U1}@50100"]                                   # 前の清算の後も1未満のまま: 同じ機会の残り
    assert (b["cross_block"], b["delay_blocks"], b["delay_seconds"], b["cause"]) == (50_040, 60, 15, "続き")

    c = m[f"{U2}@60400"]                                   # 利息で割れた(割れる時刻が前もって計算できた)
    assert (c["cross_block"], c["delay_blocks"], c["delay_seconds"], c["cause"]) == (60_001, 399, 100, "利息")
    assert c["reads"] < 40                                  # 1ブロックずつではなく、倍々と半分で探す

    d = m[f"{U3}@70000"]                                   # 同じブロックで値段が動いた: 値段の更新のすぐ後ろ
    assert (d["delay_blocks"], d["cause"]) == (0, "値段")
    e = m[f"{U4}@80000"]                                   # 同じブロックで、値段は動いていない: 利息
    assert (e["delay_blocks"], e["cause"]) == (0, "利息")

    f = m[f"{U5}@90000"]                                   # さかのぼれる所まで、ずっと1未満
    assert f["cause"] == "長く1未満" and f["cross_block"] is None and f["searched_back"] == 1_000

    assert timer.run(0)["todo"] == 0                       # 測った分は測り直さない


def test_a_few_blocks_in_the_same_second_are_not_the_same_block():
    assert timing._bucket({"delay_blocks": 0, "delay_seconds": 0}) == "同じブロック"
    assert timing._bucket({"delay_blocks": 3, "delay_seconds": 0}) == "1秒以内"
    assert timing._bucket({"delay_blocks": 900, "delay_seconds": 225}) == "1時間以内"
    assert timing._bucket({"delay_blocks": 90_000, "delay_seconds": 22_500}) == "1時間より後"


def test_report_lists_speed_causes_and_interest_cases(conn):
    node = setup(conn)
    lines: list[str] = []
    assert scan_command.run_timing("arbitrum", 7, chain=make_chain(node), conn=conn, out=lines.append) == 0
    text = "\n".join(lines)
    assert "まだ測っていない 6件のうち 6件を測った" in text
    assert "割れた理由: 値段 2件" in text and "利息 2件" in text and "続き 1件" in text and "長く1未満 1件" in text
    assert "同じブロック 2件、1秒以内 0件、10秒以内 1件、1分以内 0件、1時間以内 1件" in text
    assert "利息で割れた清算(割れる時刻を前もって計算できた機会)" in text
    assert "遅れ 399ブロック・100秒" in text
    assert LIQ2 in text


def test_no_archive_stops_with_a_clear_message(conn):
    node = setup(conn)
    node.archive = False
    lines: list[str] = []
    assert scan_command.run_timing("arbitrum", 7, chain=make_chain(node), conn=conn, out=lines.append) == 1
    assert "古いブロックの状態を読める接続先が要る" in "\n".join(lines)
    assert conn.execute("SELECT COUNT(*) FROM chain_liquidation_timing").fetchone()[0] == 0


def test_since_filters_old_liquidations(conn):
    node = setup(conn)
    timer = timing.Timer(conn, make_chain(node), pool=POOL, max_back=1_000, out=lambda s: None)
    assert timer.run(int(time.time()) + 3600)["todo"] == 0
