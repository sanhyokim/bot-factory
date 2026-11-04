"""機会の見張り(Aave の清算): 借り手を集め、健康度を読み、機会を記録し、誰が取ったかを照らす。"""
import pytest

from factory.onchain import abi, scan_command, watch
from factory.onchain.actors import Scanner
from tests.fakes_chain import (
    LIQUIDATOR,
    MULTICALL,
    POOL,
    WETH,
    FakeNode,
    join_results,
    split_aggregate3,
)
from tests.test_onchain_actors import make_chain

U1, U2, U3, U4 = ("0x" + c * 20 for c in ("11", "22", "33", "44"))


def make_watcher(conn, node, chain=None):
    return watch.Watcher(conn, chain or make_chain(node), pool=POOL, multicall=MULTICALL, batch=2, min_debt_usd=10)


def test_aggregate3_matches_reference_encoding():
    calls = [("0x" + "11" * 20, "0x313ce567"), ("0x" + "33" * 20, "0x" + "ab" * 36)]
    ref = ("0000000000000000000000000000000000000000000000000000000000000020"
           "0000000000000000000000000000000000000000000000000000000000000002"
           "0000000000000000000000000000000000000000000000000000000000000040"
           "00000000000000000000000000000000000000000000000000000000000000e0"
           "0000000000000000000000001111111111111111111111111111111111111111"
           "0000000000000000000000000000000000000000000000000000000000000001"
           "0000000000000000000000000000000000000000000000000000000000000060"
           "0000000000000000000000000000000000000000000000000000000000000004"
           "313ce56700000000000000000000000000000000000000000000000000000000"
           "0000000000000000000000003333333333333333333333333333333333333333"
           "0000000000000000000000000000000000000000000000000000000000000001"
           "0000000000000000000000000000000000000000000000000000000000000060"
           "0000000000000000000000000000000000000000000000000000000000000024"
           "abababababababababababababababababababababababababababababababab"
           "abababab00000000000000000000000000000000000000000000000000000000")   # eth-abi で作った値
    assert abi.encode_aggregate3(calls) == abi.selector("aggregate3((address,bool,bytes)[])") + ref
    answer = ("0x0000000000000000000000000000000000000000000000000000000000000020"
              "0000000000000000000000000000000000000000000000000000000000000002"
              "0000000000000000000000000000000000000000000000000000000000000040"
              "00000000000000000000000000000000000000000000000000000000000000c0"
              "0000000000000000000000000000000000000000000000000000000000000001"
              "0000000000000000000000000000000000000000000000000000000000000040"
              "0000000000000000000000000000000000000000000000000000000000000020"
              "0000000000000000000000000000000000000000000000000000000000000012"
              "0000000000000000000000000000000000000000000000000000000000000000"
              "0000000000000000000000000000000000000000000000000000000000000040"
              "0000000000000000000000000000000000000000000000000000000000000000")
    assert abi.decode_aggregate3(answer) == [(True, "0x" + "00" * 31 + "12"), (False, "0x")]
    assert split_aggregate3(abi.encode_aggregate3(calls)) == calls
    assert abi.decode_aggregate3(join_results([(True, "0x" + "ab" * 36)])) == [(True, "0x" + "ab" * 36)]


def test_estimate_profit():
    assert watch.estimate_profit(0.97, 1_000, 2_000) == pytest.approx(25)      # 半分を返せる × 5%
    assert watch.estimate_profit(0.90, 1_000, 2_000) == pytest.approx(50)      # 0.95未満は全部
    assert watch.estimate_profit(0.50, 1_000, 525) == pytest.approx(25)        # 担保までしか取れない


def test_discover_reads_borrowers_once(conn):
    node = FakeNode(head=10_000)
    for b, u in ((100, U1), (200, U2), (300, U1)):
        node.add_borrow(b, u)
    node.add_liquidation(400)                   # 借りた記録ではない
    w = make_watcher(conn, node)
    assert w.discover(1, 5_000) == 2
    row = conn.execute("SELECT first_block, last_block FROM chain_borrowers WHERE address = ?", (U1,)).fetchone()
    assert tuple(row) == (100, 300)
    node.add_borrow(6_000, U3)
    assert w.discover(5_001, 9_000) == 1


def test_sweep_records_opportunities_and_skips_dust(conn):
    node = FakeNode(head=10_000)
    for u in (U1, U2, U3, U4):
        node.add_borrow(100, u)
    node.accounts = {U1: (2_000, 1_000, 0.83, 0.97),     # 清算できる
                     U2: (20, 5, 0.83, 0.5),             # 借金5ドル(ガス代に負ける)
                     U3: (2_000, 1_000, 0.83, 1.01),     # もう少し
                     U4: (100, 0, 0.0, None)}            # 借金なし
    w = make_watcher(conn, node)
    w.discover(1, 5_000)
    st = w.sweep()
    assert st["read"] == 4 and st["with_debt"] == 3 and st["under"] == 1 and st["found"] == 1 and st["near"] == 1
    opp = conn.execute("SELECT * FROM chain_opportunities").fetchone()
    assert opp["borrower"] == U1 and opp["status"] == "未確定" and opp["est_profit_usd"] == pytest.approx(25)
    assert opp["block"] == 10_000
    pos = conn.execute("SELECT health FROM chain_positions WHERE address = ?", (U4,)).fetchone()
    assert pos[0] == watch.MAX_HEALTH
    # 借金の無い借り手は、また借りるまで読まない
    assert U4 not in w.candidates()
    node.add_borrow(10_001, U4)
    node.head = 10_010
    w.discover(5_001, 10_010)
    assert U4 in w.candidates()
    # 2回目に見ても機会は増やさない
    node.accounts[U1] = (2_000, 1_000, 0.83, 0.96)
    st = w.sweep()
    assert st["found"] == 0 and st["still"] == 1
    opp = conn.execute("SELECT * FROM chain_opportunities").fetchone()
    assert opp["last_block"] == 10_010 and opp["min_health"] == pytest.approx(0.96)


def test_resolve_finds_who_captured_and_what_vanished(conn):
    node = FakeNode(head=10_000)
    for u in (U1, U2):
        node.add_borrow(100, u)
    node.accounts = {U1: (2_000, 1_000, 0.83, 0.97), U2: (2_000, 1_000, 0.83, 0.98)}
    chain = make_chain(node)
    w = make_watcher(conn, node, chain)
    w.discover(1, 9_000)
    w.sweep()
    # U1 は3ブロック後に清算された。U2 は値段が戻った
    node.head = 10_020
    node.add_liquidation(10_003, borrower=U1)
    node.accounts = {U1: (1_000, 500, 0.83, 1.2), U2: (2_000, 1_000, 0.83, 1.1)}
    Scanner(conn, chain, pool=POOL, weth=WETH, out=lambda s: None).scan(9_000, 10_020)
    st = w.sweep()
    assert w.resolve(st["recovered"]) == {"captured": 1, "vanished": 1}
    rows = {r["borrower"]: r for r in conn.execute("SELECT * FROM chain_opportunities")}
    assert rows[U1]["status"] == "取られた" and rows[U1]["captured_by"] == LIQUIDATOR
    assert rows[U1]["delay_blocks"] == 3 and rows[U1]["captured_net_usd"] == pytest.approx(49.97)
    assert rows[U2]["status"] == "消えた"
    text = "\n".join(watch.report(conn, "arbitrum"))
    assert "取られた 1件、消えた 1件、未確定 0件" in text and "3ブロック後に取った" in text


def test_watch_command_runs_rounds(conn):
    node = FakeNode(head=400_000)
    node.add_borrow(399_000, U1)
    node.accounts = {U1: (2_000, 1_000, 0.83, 0.97)}
    lines, slept = [], []
    code = scan_command.run_watch("arbitrum", every=5, rounds=2, chain=make_chain(node), conn=conn,
                                  out=lines.append, sleep=slept.append)
    assert code == 0 and slept == [5]
    text = "\n".join(lines)
    assert "借り手 1人(新しく1人)" in text and "新しい機会 1件" in text
    assert "借り手 1人(新しく0人)" in text                  # 2回目は新しい所だけ
    assert "機会の記録 1件: 取られた 0件、消えた 0件、未確定 1件" in text
