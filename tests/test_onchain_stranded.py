"""取り残し(stranded-scan): 古い Compound の写しで、今清算できるのに誰も取っていない借り手を、偽の接続先で確かめる。"""
import copy

import pytest

from factory import config
from factory.onchain import compound, scan_command, stranded, venues
from factory.onchain.abi import address, topic, words
from tests.fakes_chain import MULTICALL, FakeNode, word
from tests.test_onchain_actors import make_chain, make_scanner
from tests.test_onchain_compound import (
    CETH,
    COMP,
    CUSDC,
    DF,
    IUSDC,
    IWETH,
    U1,
    U2,
    U3,
    U6,
    Protocol,
    liquidate,
)

BOT = "0x" + "a1" * 20
LIQUIDATOR_CONTRACT = "0x" + "1c" * 20


def arr(*xs):
    return "0x" + word(32) + word(len(xs)) + "".join(word(int(x, 16)) for x in xs)


def num(n):
    return lambda d, b: "0x" + word(n)


def per_user(table, market):
    """(市場, 借り手) → 数 の表から、その市場の「借り手ごとの数」を答える偽の関数。"""
    return lambda d, b: "0x" + word(table.get((market, address(d[10:])), 0))


class Lending(Protocol):
    """借り手ごとに、今清算できるか(利息を足した後か)を決められる偽の仕組み。"""
    def __init__(self, node):
        super().__init__(node)
        self.open = {U1: lambda acc: True, U2: lambda acc: acc, U3: lambda acc: False, U6: lambda acc: acc}
        self.asked: list[tuple[str, str]] = []
        h = node.handlers
        h[(COMP, "getAllMarkets()")] = lambda d, b: arr(CUSDC, CETH)
        h[(DF, "getAlliTokens()")] = lambda d, b: arr(IUSDC, IWETH)
        for m in (COMP, DF):
            h[(m, "closeFactorMantissa()")] = num(5 * 10**17)
        h[(COMP, "liquidationIncentiveMantissa()")] = num(108 * 10**16)
        h[(DF, "liquidationIncentiveMantissa()")] = num(110 * 10**16)
        h[(COMP, "liquidatorContract()")] = lambda d, b: None                 # 本家の写しには無い(revert)
        h[(COMP, "liquidateBorrowAllowed(address,address,address,address,uint256)")] = self.allowed
        usdc, cusdc_rate = 10**6, 2 * 10**14
        debts = {(CUSDC, U1): 1_000 * usdc, (CUSDC, U2): 1_400 * usdc, (IUSDC, U6): 600 * usdc}
        held = {(CETH, U1): 25 * 10**8, (CETH, U2): 10 * 10**8, (IWETH, U6): 3 * 10**17}   # 0.5 ETH・0.2 ETH・0.3 WETH
        for m in (CUSDC, CETH, IUSDC, IWETH):
            h[(m, "borrowBalanceStored(address)")] = per_user(debts, m)
            h[(m, "balanceOf(address)")] = per_user(held, m)
        h[(CUSDC, "exchangeRateStored()")] = num(cusdc_rate)
        h[(IUSDC, "exchangeRateStored()")] = num(10**18)
        h[(CETH, "getCash()")] = num(10 * 10**18)
        h[(IWETH, "getCash()")] = num(10**16)                                 # 0.01 WETH しか無い
        h[(CUSDC, "protocolSeizeShareMantissa()")] = lambda d, b: None

    def short(self, d, b):
        user = address(d[10:])
        return 10**18 if self.open.get(user, lambda acc: False)(self.accrued) else 0

    def allowed(self, d, b):
        w = words("0x" + d[10:])
        liquidator, user = address(f"0x{w[2]:064x}"), address(f"0x{w[3]:064x}")
        self.asked.append((liquidator, user))
        return "0x" + word(0 if user == U1 else 3)


def borrow(node, block, market, user, n=0, wide=False):
    tx = "0x" + f"{block:060x}{n:04x}"
    sig = "Borrow(address,uint256,uint256,uint256,uint256)" if wide else "Borrow(address,uint256,uint256,uint256)"
    node.logs.append({"address": market, "blockNumber": hex(block), "transactionHash": tx, "logIndex": hex(n),
                      "topics": [topic(sig)],
                      "data": "0x" + word(int(user, 16)) + word(1) * (4 if wide else 3)})


def setup(conn):
    node = FakeNode(head=100_000)
    node.tokens[CUSDC.lower()] = ("cUSDC", 8)
    node.tokens[CETH.lower()] = ("cETH", 8)
    lending = Lending(node)
    borrow(node, 10_000, CUSDC, U1)
    borrow(node, 20_000, CUSDC, U2)
    borrow(node, 20_001, CUSDC, U2, n=1)                     # 同じ借り手は1人に数える
    borrow(node, 30_000, IUSDC, U6, wide=True)               # dForce の借りた記録は1語多い
    liquidate(node, 90_000, CUSDC, U3, BOT, 10 * 10**6, CETH, 2 * 10**7)   # 清算の地図に載った借り手
    chain = make_chain(node)
    venues.Mapper(conn, chain, out=lambda s: None).scan(1, 99_000)
    return node, lending, chain


def make(conn, node, chain, now):
    reader = compound.Reader(conn, chain, scanner=make_scanner(conn, node), multicall=MULTICALL, out=lambda s: None)
    return stranded.Finder(conn, chain, reader=reader, out=lambda s: None, clock=lambda: now[0])


def test_finds_open_borrowers_and_measures_them(conn):
    node, lending, chain = setup(conn)
    now = [1_000_000.0]
    f = make(conn, node, chain, now)
    places = f.places({"dForce の写し": DF})
    assert [(p.controller, p.dialect.name, p.markets) for p in places] == [(DF, "dForce", [IUSDC, IWETH]),
                                                                            (COMP, "Compound", [CUSDC, CETH])]
    totals = f.run(places, from_block=1, to_block=99_000)
    assert totals["borrowers"] == 4 and totals["open"] == 3     # U3 は地図から足したが、清算できない
    # 借りた記録は、すべての仕組みの市場の住所と、2つの呼び方の借りた記録の形だけで、まとめて読む
    flt = [b["params"][0] for b in node.bodies if b["method"] == "eth_getLogs" and b["params"][0].get("address")]
    assert flt and {tuple(x["address"]) for x in flt} == {tuple(sorted((CUSDC, CETH, IUSDC, IWETH)))}
    assert all(set(x["topics"][0]) == set(stranded.BORROWS.values()) for x in flt)
    rows = {r["borrower"]: r for r in stranded.summarize(conn, "arbitrum")}
    assert set(rows) == {U1, U2, U6}                            # U2・U6 は利息を足すと割れている

    u1 = rows[U1]
    assert u1["debt_usd"] == pytest.approx(1_000) and u1["collateral_usd"] == pytest.approx(0.5 * 2_600)
    assert u1["repay_market"] == CUSDC and u1["collateral_market"] == CETH
    assert u1["repay_usd"] == pytest.approx(500)                # 借金 × 0.5(担保 ÷ 1.08 より小さい)
    assert u1["profit_usd"] == pytest.approx(500 * (1.08 * (1 - 0.028) - 1))
    assert u1["allowed"] == 0 and u1["cash_ok"] == 1
    assert (stranded.NOBODY.lower(), U1) in lending.asked      # 清算する側を決めていない仕組みには、誰でもない住所で聞く

    u2 = rows[U2]
    assert u2["repay_usd"] == pytest.approx(0.2 * 2_600 / 1.08)  # 担保が足りないので、担保 ÷ 上乗せ まで
    assert u2["allowed"] == 3                                   # 仕組みが断った

    u6 = rows[U6]
    assert u6["debt_usd"] == pytest.approx(600) and u6["collateral_usd"] == pytest.approx(0.3 * 3_000)
    assert u6["repay_usd"] == pytest.approx(300) and u6["profit_usd"] == pytest.approx(300 * 0.1)
    assert u6["allowed"] is None and u6["cash_ok"] == 0          # dForce には聞かない。現金は0.01 WETH しか無い


def test_remembers_how_long_a_borrower_stays_open(conn):
    node, lending, chain = setup(conn)
    now = [1_000_000.0]
    f = make(conn, node, chain, now)
    places = f.places()
    f.run(places, from_block=1, to_block=99_000)
    now[0] += 2 * 86400
    t = f.run(places, from_block=1, to_block=99_000)
    assert t["added"] == 0                                      # 読み終えた範囲は読み直さない
    rows = {r["borrower"]: r for r in stranded.summarize(conn, "arbitrum")}
    assert rows[U1]["runs"] == 2 and rows[U1]["first_seen"] == 1_000_000
    text = "\n".join(stranded.describe(stranded.summarize(conn, "arbitrum"), t, chain="arbitrum", days=180,
                                       now=now[0]))
    assert "初めて見てから2.0日(2回)" in text
    lending.open[U1] = lambda acc: False                        # 誰かが清算した(か、返した)
    t = f.run(places, from_block=1, to_block=99_000)
    assert t["gone"] == 1 and U1 not in {r["borrower"] for r in stranded.summarize(conn, "arbitrum")}


def test_stranded_scan_command(conn, monkeypatch):
    _, _, chain = setup(conn)
    cfg = copy.deepcopy(config.load_config())
    cfg["onchain"]["chains"]["arbitrum"]["compound_comptrollers"] = {"dForce の写し": DF}
    monkeypatch.setattr(config, "load_config", lambda *a: cfg)
    lines: list[str] = []
    assert scan_command.run_stranded("arbitrum", 180, chain=chain, conn=conn, out=lines.append) == 0
    text = "\n".join(lines)
    assert "取り残しを探す: Compound の写し 2か所" in text
    assert "仕組み 2か所、借り手 4人" in text
    assert "今清算できる借り手 3人" in text and "仕組みが清算してよいと答えた分 1人" in text
    assert f"{COMP}  {U1}  $1,000.00(USDC)  $1,300.00(cETH)  $500.00  $24.88  できる  足りる" in text
    assert "断られた(番号 3)" in text and "聞けなかった" in text


def test_stranded_scan_without_places(conn):
    node = FakeNode(head=100_000)
    lines: list[str] = []
    assert scan_command.run_stranded("arbitrum", 180, chain=make_chain(node), conn=conn, out=lines.append) == 1
    assert "調べる Compound の写しが無い" in "\n".join(lines)
