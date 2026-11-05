"""稼いでいる主体の分析(清算・Aave v3)。偽の接続先で、値段・ガス代・型・続きからの読み取り・まとめを確かめる。"""
import pytest

from factory.onchain import actors, scan_command
from factory.onchain.abi import topic
from factory.onchain.chain import Chain
from factory.onchain.rpc import Relay
from tests.fakes_chain import LIQUIDATOR, POOL, WBTC, WETH, A, B, Clock, FakeNode

BALANCER_FLASH = topic("FlashLoan(address,address,uint256,uint256)")
UNIV3_SWAP = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
OTHER = "0x" + "cd" * 20


def make_chain(node, depth=100):
    clock = Clock()
    relay = Relay("arbitrum", [A, B], client=node.client(), clock=clock, sleep=clock.sleep)
    return Chain(relay, cache_depth=depth, max_log_blocks=10_000)


def make_scanner(conn, node, out=None):
    return actors.Scanner(conn, make_chain(node), pool=POOL, weth=WETH, out=out or (lambda s: None))


def rows(conn):
    return conn.execute("SELECT * FROM chain_liquidations ORDER BY block, log_index").fetchall()


def test_liquidation_value_gas_and_shape(conn):
    node = FakeNode(head=10_000)
    flash = {"topics": [BALANCER_FLASH], "data": "0x"}
    swap = {"topics": [UNIV3_SWAP], "data": "0x"}
    node.add_liquidation(5_000, extra_logs=[flash, swap])
    assert make_scanner(conn, node).scan(1, 9_000) == 1
    r = rows(conn)[0]
    assert r["liquidator"] == LIQUIDATOR and r["sender"] == "0x" + "e0" * 20
    assert r["collateral_usd"] == pytest.approx(1_050) and r["debt_usd"] == pytest.approx(1_000)
    assert r["gross_usd"] == pytest.approx(50)
    assert r["gas_usd"] == pytest.approx(0.03)                 # 100万ガス × 0.01gwei × 3,000ドル
    assert r["net_usd"] == pytest.approx(49.97)
    assert r["price_basis"] == "block" and r["gas_price_gwei"] == pytest.approx(0.01)
    assert r["pattern"] == "WETH担保/USDC借金・自前の仕組み・フラッシュローン(Balancer)・交換1回(UniswapV3)"
    assert r["via"] == "自前の仕組み" and r["flash"] == "Balancer" and r["swaps"] == 1


def test_direct_call_and_gas_split_between_two_liquidations_in_one_tx(conn):
    node = FakeNode(head=10_000)
    h = node.add_liquidation(5_000, to=POOL, log_index=1)
    node.add_liquidation(5_000, to=POOL, log_index=2, tx_hash=h, collateral=WBTC,
                         collateral_amount=2 * 10**6)          # 0.02 WBTC = 1,200ドル
    make_scanner(conn, node).scan(1, 9_000)
    a, b = rows(conn)
    assert a["via"] == b["via"] == "直接"
    assert a["pattern"].endswith("直接・交換なし")
    assert a["gas_usd"] == pytest.approx(0.015) and b["gas_usd"] == pytest.approx(0.015)
    assert b["gross_usd"] == pytest.approx(200)


def test_falls_back_to_latest_prices_without_archive(conn):
    node = FakeNode(head=10_000)
    node.archive = False
    node.latest_prices = {WETH: 4_000.0}
    node.add_liquidation(5_000)
    said = []
    scanner = make_scanner(conn, node, out=said.append)
    scanner.scan(1, 9_000)
    r = rows(conn)[0]
    assert not scanner.archive_ok and r["price_basis"] == "latest"
    assert r["collateral_usd"] == pytest.approx(1_400)        # 今の値段 4,000ドル × 0.35
    assert sum("今の値段で計算する" in s for s in said) == 1    # 言うのは1回だけ


def test_unknown_price_is_left_empty(conn):
    node = FakeNode(head=10_000)
    unknown = "0x" + "99" * 20
    node.tokens[unknown] = ("XYZ", 18)
    node.add_liquidation(5_000, collateral=unknown)
    make_scanner(conn, node).scan(1, 9_000)
    r = rows(conn)[0]
    assert r["collateral_usd"] is None and r["net_usd"] is None and r["debt_usd"] == pytest.approx(1_000)


def test_rescan_adds_nothing_and_plan_reads_only_new_ranges(conn):
    node = FakeNode(head=200_000)
    node.add_liquidation(60_000)
    node.add_liquidation(120_000)
    s = make_scanner(conn, node)
    assert actors.plan(conn, "arbitrum", 100_000, 150_000) == [(100_000, 150_000)]
    assert s.scan(100_000, 150_000) == 1
    assert actors.plan(conn, "arbitrum", 100_000, 150_000) == []
    assert actors.plan(conn, "arbitrum", 50_000, 160_000) == [(50_000, 99_999), (150_001, 160_000)]
    assert s.scan(50_000, 99_999) == 1 and s.scan(100_000, 150_000) == 0
    assert len(rows(conn)) == 2
    assert actors.plan(conn, "arbitrum", 50_000, 150_000) == []
    # 前に読んだ範囲とつながらない時は、前の記録を忘れてその範囲を読む
    assert actors.plan(conn, "arbitrum", 170_000, 180_000) == [(170_000, 180_000)]
    assert conn.execute("SELECT COUNT(*) FROM chain_scans").fetchone()[0] == 0


def test_summary_ranks_actors_by_net(conn):
    node = FakeNode(head=10_000)
    for b in (1_000, 2_000, 3_000):
        node.add_liquidation(b, sender="0x" + f"{b:040x}")
    node.add_liquidation(4_000, liquidator=OTHER, debt_amount=1_045 * 10**6)   # 粗利5ドル
    make_scanner(conn, node).scan(1, 9_000)
    summary = actors.summarize(conn, "arbitrum", 0)
    assert [a["liquidator"] for a in summary] == [LIQUIDATOR, OTHER]
    top = summary[0]
    assert top["count"] == 3 and top["senders"] == 3
    assert top["net_usd"] == pytest.approx(3 * 49.97) and top["best_usd"] == pytest.approx(49.97)
    assert top["shape"] == "自前の仕組み・交換なし"
    text = "\n".join(actors.describe(summary, chain="arbitrum", days=7))
    assert "清算 4件、清算した主体 2件" in text
    assert f"1. {LIQUIDATOR}  3回 3財布 $149.91" in text and "全体の97%" in text


def test_actor_scan_command_reads_window_and_reports(conn):
    node = FakeNode(head=400_000)
    node.add_liquidation(399_000)
    node.add_liquidation(10_000)                 # 1日より前(読まない)
    lines = []
    chain = make_chain(node)
    assert scan_command.run("arbitrum", 1, chain=chain, conn=conn, out=lines.append) == 0
    text = "\n".join(lines)
    assert "新しく帳簿に足した清算 1件" in text and "清算 1件、清算した主体 1件" in text
    lines.clear()
    assert scan_command.run("arbitrum", 1, chain=chain, conn=conn, out=lines.append) == 0
    assert "新しく帳簿に足した清算 0件" in "\n".join(lines)
    assert conn.execute("SELECT SUM(calls) FROM rpc_usage").fetchone()[0] is None   # 帳簿の接続は渡していない


def test_actor_scan_command_reports_rpc_errors(conn):
    node = FakeNode(head=400_000)
    node.mode[A] = node.mode[B] = "down"
    lines = []
    chain = make_chain(node)
    chain.relay.waits["research"] = 0
    assert scan_command.run("arbitrum", 1, chain=chain, conn=conn, out=lines.append) == 1
    assert "エラー" in lines[-1]


def test_timeboost_mark_is_recorded_and_shown_per_actor(conn):
    node = FakeNode(head=400_000)
    boosted = node.add_liquidation(399_000)
    node.add_liquidation(399_100, liquidator=OTHER)
    node.receipts[boosted]["timeboosted"] = True          # 優先レーンで入った取引
    lines = []
    assert scan_command.run("arbitrum", 1, chain=make_chain(node), conn=conn, out=lines.append) == 0
    text = "\n".join(lines)
    assert "優先レーン 1/1件" in text                      # 印があった
    assert "優先レーン 不明" in text                       # 印が無い受領書(接続先が返さない)
    marks = {r[0]: r[1] for r in conn.execute("SELECT liquidator, timeboosted FROM chain_liquidations")}
    assert marks == {LIQUIDATOR: 1, OTHER: -1}


def test_old_rows_get_the_timeboost_mark_from_receipts(conn, tmp_path):
    import sqlite3

    old = sqlite3.connect(tmp_path / "old.db")
    old.row_factory = sqlite3.Row
    old.executescript(actors.SCHEMA.replace("    timeboosted   INTEGER,                   -- 優先レーン"
                                            "(Timeboost)で入った取引か: 1 / 0 / -1(受領書に印が無い)\n", ""))
    assert "timeboosted" not in {r[1] for r in old.execute("PRAGMA table_info(chain_liquidations)")}
    actors.ensure_schema(old)                               # 前の版の帳簿に列を足す
    node = FakeNode(head=400_000)
    tx = node.add_liquidation(399_000)
    node.receipts[tx]["timeboosted"] = "0x0"
    actors.Scanner(old, make_chain(node), pool=POOL, weth=WETH, out=lambda s: None).scan(398_000, 399_500)
    old.execute("UPDATE chain_liquidations SET timeboosted = NULL")
    assert actors.backfill_timeboost(old, make_chain(node), 0) == 1
    assert old.execute("SELECT timeboosted FROM chain_liquidations").fetchone()[0] == 0
    old.close()
