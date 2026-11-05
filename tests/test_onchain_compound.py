"""Compound の写しの清算を深く読む: 利益・清算できるようになってから取られるまで・理由を、偽の接続先で確かめる。"""
import pytest

from factory.onchain import compound, scan_command, venues
from factory.onchain.abi import address, selector
from tests.fakes_chain import (
    MULTICALL,
    USDC,
    WETH,
    FakeNode,
    join_results,
    split_aggregate3,
    word,
)
from tests.test_onchain_actors import make_chain, make_scanner

COMP, ORC1 = "0x" + "c0" * 20, "0x" + "c9" * 20            # Compound の呼び方(値段表はドル建て)
CUSDC, CETH = "0x" + "c1" * 20, "0x" + "c2" * 20
DF, ORC2 = "0x" + "d0" * 20, "0x" + "d9" * 20              # dForce の呼び方(値段表は ETH 建て)
IUSDC, IWETH = "0x" + "d1" * 20, "0x" + "d2" * 20
BOT_A, BOT_B, BOT_C = ("0x" + c * 20 for c in ("a1", "a2", "a3"))
U1, U2, U3, U4, U5, U6 = ("0x" + c * 20 for c in ("11", "22", "33", "44", "55", "66"))
LIQUIDATE_BORROW = compound.LIQUIDATE_BORROW


def eth_price(block):
    """Compound の値段表の ETH: 50,000 と 80,000 で下がる。"""
    return 3_000.0 if block < 50_000 else 2_700.0 if block < 80_000 else 2_600.0


# 借り手 → (ブロック, 利息を足したか) → 清算できるか
OPEN = {
    U1: lambda b, acc: b >= 50_000,                  # 値段で割れる
    U2: lambda b, acc: acc and b >= 60_001,          # 利息を足すと割れている(帳簿の上では割れていない)
    U3: lambda b, acc: b >= 70_000,                  # 担保の掛け目が下がって割れる
    U4: lambda b, acc: True,                         # ずっと割れている
    U5: lambda b, acc: b >= 80_000,                  # 清算と同じブロックで値段が動いて割れる
    U6: lambda b, acc: acc and b >= 85_000,          # dForce: 利息を足すと割れている
}


class Protocol:
    def __init__(self, node):
        self.node, self.accrued = node, False
        h = node.handlers
        addr = lambda a: "0x" + word(int(a, 16))
        arr = lambda *xs: "0x" + word(32) + word(len(xs)) + "".join(word(int(x, 16)) for x in xs)
        for m in (CUSDC, CETH):
            h[(m, "comptroller()")] = lambda d, b: addr(COMP)
            h[(m, "accrueInterest()")] = self.accrue
        for m in (IUSDC, IWETH):
            h[(m, "controller()")] = lambda d, b: addr(DF)
            h[(m, "updateInterest()")] = self.accrue
            h[(m, "protocolSeizeShareMantissa()")] = lambda d, b: None
        h[(COMP, "oracle()")] = lambda d, b: addr(ORC1)
        h[(DF, "priceOracle()")] = lambda d, b: addr(ORC2)
        h[(CUSDC, "underlying()")] = h[(IUSDC, "underlying()")] = lambda d, b: addr(USDC)
        h[(CETH, "underlying()")] = lambda d, b: None                          # ETH の市場は元のトークンが無い
        h[(IWETH, "underlying()")] = lambda d, b: addr(WETH)
        h[(CETH, "exchangeRateStored()")] = lambda d, b: "0x" + word(2 * 10**26)   # 1 cETH = 0.02 ETH
        h[(CETH, "protocolSeizeShareMantissa()")] = lambda d, b: "0x" + word(28 * 10**15)   # 2.8%
        h[(IWETH, "exchangeRateStored()")] = lambda d, b: "0x" + word(10**18)
        h[(ORC1, "getUnderlyingPrice(address)")] = lambda d, b: "0x" + word(
            {CUSDC: 10**30, CETH: int(eth_price(b) * 10**18)}[address(d[10:])])
        h[(ORC2, "getUnderlyingPrice(address)")] = lambda d, b: "0x" + word(
            {IUSDC: 10**30 // 3_000, IWETH: 10**18}[address(d[10:])])
        h[(COMP, "markets(address)")] = lambda d, b: "0x" + word(1) + word(
            6 * 10**17 if address(d[10:]) == CETH and b >= 70_000 else 75 * 10**16) + word(0)
        h[(COMP, "getAssetsIn(address)")] = lambda d, b: arr(CUSDC, CETH)
        h[(DF, "getEnteredMarkets(address)")] = lambda d, b: arr(IWETH)
        h[(DF, "getBorrowedAssets(address)")] = lambda d, b: arr(IUSDC)
        h[(COMP, "getAccountLiquidity(address)")] = lambda d, b: "0x" + word(0) + word(0) + word(self.short(d, b))
        h[(DF, "calcAccountEquity(address)")] = lambda d, b: "0x" + word(0) + word(self.short(d, b)) + word(0) * 2
        h[(MULTICALL, "aggregate3((address,bool,bytes)[])")] = self.aggregate

    def accrue(self, d, b):
        self.accrued = True
        return "0x"

    def short(self, d, b):
        return 10**18 if OPEN[address(d[10:])](b, self.accrued) else 0

    def aggregate(self, data, block):
        self.accrued = False
        out = []
        for to, d in split_aggregate3(data):
            fn = next((f for (t, s), f in self.node.handlers.items() if t == to and selector(s) == d[:10]), None)
            got = fn(d, block) if fn else None
            out.append((got is not None, got or "0x"))
        self.accrued = False
        return join_results(out)


def liquidate(node, block, market, borrower, liquidator, repay, coll, seize, n=0):
    tx = "0x" + f"{block:060x}{n:04x}"
    log = {"address": market, "blockNumber": hex(block), "transactionHash": tx, "logIndex": hex(n),
           "topics": [LIQUIDATE_BORROW],
           "data": "0x" + "".join(word(int(x, 16) if isinstance(x, str) else x)
                                  for x in (liquidator, borrower, repay, coll, seize))}
    node.logs.append(log)
    node.txs[tx] = {"hash": tx, "from": "0x" + "e0" * 20, "to": liquidator, "blockNumber": hex(block),
                    "gasPrice": hex(10**7)}
    node.receipts[tx] = {"transactionHash": tx, "blockNumber": hex(block), "status": "0x1", "gasUsed": hex(10**6),
                         "effectiveGasPrice": hex(10**7), "logs": [log]}


def setup(conn):
    node = FakeNode(head=100_000)
    Protocol(node)
    usdc = 10**6
    liquidate(node, 50_040, CUSDC, U1, BOT_A, 1_000 * usdc, CETH, 2 * 10**9)    # 0.4 ETH = 1,080ドル
    liquidate(node, 50_100, CUSDC, U1, BOT_B, 100 * usdc, CETH, 2 * 10**8)
    liquidate(node, 60_400, CUSDC, U2, BOT_B, 200 * usdc, CETH, 4 * 10**8)
    liquidate(node, 72_400, CUSDC, U3, BOT_B, 1_000 * usdc, CETH, 2 * 10**9)
    liquidate(node, 80_000, CUSDC, U5, BOT_A, 100 * usdc, CETH, 25 * 10**7)
    liquidate(node, 85_004, IUSDC, U6, BOT_C, 500 * usdc, IWETH, 18 * 10**16)   # 0.18 WETH = 540ドル
    liquidate(node, 90_000, CUSDC, U4, BOT_A, 10 * usdc, CETH, 2 * 10**7)
    venues.Mapper(conn, make_chain(node), out=lambda s: None).scan(1, 99_000)
    return node


def reader(conn, node, **kw):
    return compound.Reader(conn, make_chain(node), scanner=make_scanner(conn, node), multicall=MULTICALL,
                           max_back=5_000, out=lambda s: None, **kw)


def measured(conn):
    return {f"{r['borrower']}@{r['block']}": r for r in
            conn.execute("SELECT * FROM chain_compound_liquidations").fetchall()}


def test_profit_delay_and_cause_for_both_dialects(conn):
    node = setup(conn)
    said: list[str] = []
    r = reader(conn, node)
    r.out = said.append
    st = r.run(0)
    assert (st["todo"], st["done"], st["skipped"]) == (7, 7, 0)
    m = measured(conn)

    a = m[f"{U1}@50040"]                                   # 値段で割れて、40ブロック(10秒)後に取られた
    assert (a["controller"], a["dialect"], a["liquidator"]) == (COMP, "Compound", BOT_A)
    assert a["repay_usd"] == pytest.approx(1_000) and a["seize_usd"] == pytest.approx(1_080 * 0.972)
    assert a["gas_usd"] == pytest.approx(0.03) and a["net_usd"] == pytest.approx(49.73)
    assert (a["cross_block"], a["delay_blocks"], a["delay_seconds"], a["cause"]) == (50_000, 40, 10, "値段")

    b = m[f"{U1}@50100"]                                   # 前の清算の後も清算できるまま
    assert (b["cross_block"], b["delay_blocks"], b["delay_seconds"], b["cause"]) == (50_040, 60, 15, "続き")

    c = m[f"{U2}@60400"]                                   # 帳簿の上では割れていないが、利息を足すと割れていた
    assert (c["cross_block"], c["delay_blocks"], c["delay_seconds"], c["cause"]) == (60_001, 399, 100, "利息")
    assert c["reads"] < 50

    d = m[f"{U3}@72400"]                                   # 担保の掛け目が下がって割れ、10分後に取られた
    assert (d["cross_block"], d["delay_seconds"], d["cause"]) == (70_000, 600, "設定の変更")

    e = m[f"{U5}@80000"]                                   # 清算と同じブロックで値段が動いた
    assert (e["delay_blocks"], e["cause"]) == (0, "値段")

    f = m[f"{U4}@90000"]                                   # さかのぼれる所まで、ずっと清算できる
    assert f["cause"] == "長く清算できるまま" and f["cross_block"] is None and f["searched_back"] == 5_000

    g = m[f"{U6}@85004"]                                   # dForce の呼び方・ETH 建ての値段表
    assert (g["controller"], g["dialect"], g["collateral_market"]) == (DF, "dForce", IWETH)
    assert g["repay_usd"] == pytest.approx(500) and g["net_usd"] == pytest.approx(39.97)
    assert (g["delay_blocks"], g["delay_seconds"], g["cause"]) == (4, 1, "利息")
    assert any(DF in s and "ETH" in s for s in said)       # 単位を合わせたことを言う

    # 地図の帳簿も dForce の Controller でまとまっている
    assert conn.execute("SELECT venue FROM chain_venues WHERE emitter = ?", (IUSDC,)).fetchone()[0] == DF
    assert r.run(0)["todo"] == 0                           # 読んだ分は読み直さない


def test_without_accrual_interest_looks_instant(conn):
    """利息を足さずに読むと、利息で割れた清算が「同じブロック」に見えてしまう(だから足してから読む)。"""
    node = setup(conn)
    r = reader(conn, node)
    r.multicall = None
    r.run(0)
    c = measured(conn)[f"{U2}@60400"]
    assert (c["delay_blocks"], c["cause"]) == (0, "利息")


def test_summary_by_controller(conn):
    node = setup(conn)
    reader(conn, node).run(0)
    s = compound.summarize(conn, "arbitrum", 0)
    assert [v["controller"] for v in s] == [COMP, DF]
    comp = s[0]
    assert comp["count"] == 6
    assert [(w, n) for w, n, _ in comp["actors"]] == [(BOT_A, 3), (BOT_B, 3)]     # 純利益の多い順
    assert comp["actors"][0][2] == pytest.approx(49.73 + 26.33 + 0.0788)
    assert dict(comp["causes"]) == {"値段": 2, "続き": 1, "利息": 1, "設定の変更": 1, "長く清算できるまま": 1}
    assert dict(comp["speed"]) == {"10秒以内": 1, "同じブロック": 1, "1時間以内": 2}
    assert comp["median_delay"] == 55
    assert [r["borrower"] for r in comp["slow"]] == [U3, U2]


def test_compound_scan_command_prints_the_table(conn):
    node = setup(conn)
    lines: list[str] = []
    assert scan_command.run_compound("arbitrum", 7, chain=make_chain(node), conn=conn, out=lines.append) == 0
    text = "\n".join(lines)
    assert "まだ読んでいない 7件のうち 7件を読んだ" in text
    assert f"{COMP}(名前なし・Compoundの呼び方): 6件" in text
    assert "理由: 設定の変更 1件($49.73)、値段 2件" in text
    assert "同じブロック 1件、10秒以内 1件、1時間以内 2件(中央値 55秒)" in text
    assert "1分より遅く取られた 2件・純利益 $59.65" in text
    assert "遅れ 600秒  設定の変更" in text


def test_no_archive_stops_with_a_clear_message(conn):
    node = setup(conn)
    node.archive = False
    lines: list[str] = []
    assert scan_command.run_compound("arbitrum", 7, chain=make_chain(node), conn=conn, out=lines.append) == 1
    assert "古いブロックの状態を読める接続先が要る" in "\n".join(lines)
    assert conn.execute("SELECT COUNT(*) FROM chain_compound_liquidations").fetchone()[0] == 0


def test_venue_map_groups_dforce_markets_by_controller(conn):
    setup(conn)
    s = {v["venue"]: v for v in venues.summarize(conn, "arbitrum", 0)}
    assert s[DF]["family"] == "Compound型" and s[COMP]["count"] == 6
