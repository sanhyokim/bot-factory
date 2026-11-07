"""清算の地図: 住所を決めずに清算の記録を読み、仕組みごとに件数・主体・1位の取り分を出す。"""
import copy

from factory import config
from factory.onchain import scan_command, venues
from tests.fakes_chain import LIQUIDATOR, POOL, USDC, WETH, FakeNode, word
from tests.test_onchain_actors import make_chain

COMPTROLLER = "0x" + "c0" * 20
CUSDC, CWETH = "0x" + "c1" * 20, "0x" + "c2" * 20
MORPHO = "0x" + "40" * 20
COMET = "0x" + "c3" * 20
SILO = "0x" + "51" * 20
BOT1, BOT2, BOT3 = ("0x" + c * 20 for c in ("b1", "b2", "b3"))
USER = "0x" + "99" * 20
FAM = {f.name: f for f in venues.FAMILIES}


def pad(addr: str) -> str:
    return "0x" + addr[2:].rjust(64, "0")


def add(node, block, family, emitter, *, topics=(), data_words=(), to=None, sender="0x" + "e0" * 20, n=0):
    tx_hash = "0x" + f"{block:060x}{n:04x}"
    node.logs.append({"address": emitter, "blockNumber": hex(block), "transactionHash": tx_hash,
                      "logIndex": hex(n), "topics": [FAM[family].topic0, *topics],
                      "data": "0x" + "".join(word(w) for w in data_words)})
    node.txs[tx_hash] = {"hash": tx_hash, "from": sender, "to": to or emitter, "blockNumber": hex(block),
                         "gasPrice": hex(2 * 10**7)}


def setup():
    node = FakeNode(head=200_000)
    node.comptrollers = {CUSDC.lower(): COMPTROLLER, CWETH.lower(): COMPTROLLER}
    node.tokens[CUSDC.lower()] = ("cUSDC", 8)
    node.tokens[CWETH.lower()] = ("cWETH", 8)
    # Aave型: 3件、2主体(記録の data の3語目が清算した主体)
    for i, bot in enumerate((BOT1, BOT1, BOT2)):
        add(node, 100_000 + i, "Aave型", POOL, topics=[pad(WETH), pad(USDC), pad(USER)],
            data_words=(10**6, 10**15, int(bot, 16), 0))
    # Compound型: 2つの市場が同じ Comptroller。1主体だけ(data の1語目)
    for i, market in enumerate((CUSDC, CWETH)):
        add(node, 120_000 + i, "Compound型", market,
            data_words=(int(BOT3, 16), int(USER, 16), 5, int(CWETH, 16), 7))
    # Morpho: 清算した主体は topic の2番目
    add(node, 130_000, "Morpho", MORPHO, topics=["0x" + "ab" * 32, pad(BOT2), pad(USER)], data_words=(1, 2, 3, 0, 0))
    # Silo v1 ほか: 主体が記録に無いので、取引の宛先
    add(node, 140_000, "Silo v1 ほか", SILO, topics=[pad(USDC), pad(USER)], data_words=(1, 2), to=LIQUIDATOR)
    # 清算ではない記録は読まない
    node.logs.append({"address": COMET, "blockNumber": hex(150_000), "transactionHash": "0x" + "0f" * 32,
                      "logIndex": "0x0", "topics": ["0x" + "12" * 32], "data": "0x"})
    return node


def test_map_groups_by_venue_and_counts_liquidators(conn):
    node = setup()
    mapper = venues.Mapper(conn, make_chain(node), out=lambda s: None)
    assert mapper.scan(1, 190_000) == 7
    assert mapper.scan(1, 190_000) == 0                    # 2回目は足さない
    s = {v["family"]: v for v in venues.summarize(conn, "arbitrum", 0)}
    aave = s["Aave型"]
    assert (aave["venue"], aave["count"], aave["liquidators"], aave["top"]) == (POOL.lower(), 3, 2, BOT1)
    assert round(aave["top_share"], 2) == 0.67
    comp = s["Compound型"]
    assert (comp["venue"], comp["count"], comp["markets"], comp["liquidators"]) == (COMPTROLLER, 2, 2, 1)
    assert set(comp["symbols"]) == {"cUSDC", "cWETH"} and comp["top"] == BOT3
    assert s["Morpho"]["top"] == BOT2 and s["Morpho"]["borrowers"] == 1
    assert s["Silo v1 ほか"]["top"] == LIQUIDATOR.lower()
    assert s["Aave型"]["median_gwei"] == 0.02
    # 市場の名前と Comptroller は1回だけ読む
    calls = [m for _, m in node.calls if m == "eth_call"]
    assert len(calls) == 5 + 2                             # 5つの仕組みの名前 + Compound型の2市場の Comptroller


def test_venue_scan_command_prints_the_map(conn):
    node = setup()
    lines: list[str] = []
    assert scan_command.run_venues("arbitrum", 7, chain=make_chain(node), conn=conn, out=lines.append) == 0
    text = "\n".join(lines)
    assert "新しく帳簿に足した記録 7件" in text
    assert "清算の記録 7件、仕組み 4か所" in text
    assert "形ごと: Aave型 3件(1か所)、Compound型 2件(1か所)" in text
    assert f"Compound型  {COMPTROLLER}(cUSDC・cWETH)  2件  2市場  1主体  1位 100%({BOT3})" in text


def test_scan_key_changes_when_families_change():
    assert venues.SCAN_KEY.startswith("venues_") and len(venues.SCAN_KEY) == 15
    assert len(venues.BY_TOPIC) == len(venues.FAMILIES)    # 形の topic が重ならない


def markets_answer(*markets):
    return lambda data, block: "0x" + word(32) + word(len(markets)) + "".join(word(int(m, 16)) for m in markets)


def test_known_markets_are_read_by_address(conn):
    """住所を決めない読み方を断る接続先でも、Compound の写しの市場を住所で決めれば読める。"""
    node = setup()
    node.handlers[(COMPTROLLER, "getAllMarkets()")] = markets_answer(CUSDC, CWETH)
    mapper = venues.Mapper(conn, make_chain(node), out=lambda s: None)
    assert mapper.markets(COMPTROLLER) == [CUSDC, CWETH]
    key = venues.known_key(COMPTROLLER)
    assert mapper.scan(1, 190_000, markets=[CUSDC, CWETH], key=key) == 2     # Compound 型の2件だけ
    flt = [b["params"][0] for b in node.bodies if b["method"] == "eth_getLogs"]
    assert flt and all(f["address"] == [CUSDC, CWETH] for f in flt)
    assert all(f["topics"] == [[venues.FAMILY_BY_NAME[venues.COMPTROLLER].topic0]] for f in flt)
    # 住所を決めない読み方の範囲とは別に数える(後で住所を決めずに読むと、残りの5件を足す)
    assert scan_command.actors.plan(conn, "arbitrum", 1, 190_000, key=venues.SCAN_KEY) == [(1, 190_000)]
    assert mapper.scan(1, 190_000) == 5


def test_venue_scan_known_uses_the_config(conn, monkeypatch):
    node = setup()
    node.handlers[(COMPTROLLER, "getAllMarkets()")] = markets_answer(CUSDC, CWETH)
    cfg = copy.deepcopy(config.load_config())
    cfg["onchain"]["chains"]["arbitrum"]["compound_comptrollers"] = {"Lodestar": COMPTROLLER}
    monkeypatch.setattr(config, "load_config", lambda *a: cfg)
    lines: list[str] = []
    assert scan_command.run_venues("arbitrum", 7, chain=make_chain(node), conn=conn, out=lines.append,
                                   known=True) == 0
    text = "\n".join(lines)
    assert "Lodestar の清算の記録を、市場 2か所に決めて読む" in text
    assert "新しく帳簿に足した記録 2件" in text
    assert f"Compound型  {COMPTROLLER}(cUSDC・cWETH)  2件  2市場  1主体  1位 100%({BOT3})" in text


def test_venue_scan_known_needs_the_config(conn):
    lines: list[str] = []
    assert scan_command.run_venues("arbitrum", 7, chain=make_chain(setup()), conn=conn, out=lines.append,
                                   known=True) == 1
    assert "compound_comptrollers)が書いていない" in "\n".join(lines)
