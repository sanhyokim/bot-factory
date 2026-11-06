"""裁定で稼いでいる主体を探す: 交換の記録から、1回の取引の中で元に戻る交換を見分けて、主体ごとにまとめる。"""
import pytest

from factory.onchain import arbs, scan_command
from factory.onchain.abi import topic
from tests.fakes_chain import USDC, WBTC, WETH, FakeNode, word
from tests.test_onchain_actors import make_chain, make_scanner

V2 = topic("Swap(address,uint256,uint256,uint256,uint256,address)")
V3 = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
PCS = topic("Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)")
BAL = topic("Swap(bytes32,address,address,uint256,uint256)")
VAULT = "0x" + "ba" * 20
BAL_POOL = "0x" + "bb" * 32
XYZ = "0x" + "7a" * 20
P1, P2, P3, P4, P5, P6, PX = ("0x" + c * 20 for c in ("f1", "f2", "f3", "f4", "f5", "f6", "fe"))
POOLS = {P1: (WETH, USDC), P2: (USDC, WETH), P3: (WETH, WBTC), P4: (WBTC, USDC), P5: (XYZ, WETH), P6: (XYZ, WETH)}
BOT1, BOT2, BOT3, ROUTER = ("0x" + c * 20 for c in ("b1", "b2", "b3", "e1"))
E18, E6 = 10**18, 10**6


def pad(a: str) -> str:
    return "0x" + a[2:].rjust(64, "0")


def s256(x: int) -> str:
    return word(x % 2**256)


class Book:
    """偽の接続先に交換の記録と受領書を足す。"""

    def __init__(self, node):
        self.node, self.n = node, {}

    def tx(self, block: int, name: str) -> str:
        return "0x" + f"{block:056x}" + name.encode().hex().rjust(8, "0")

    def _log(self, tx, block, address, topics, data):
        n = self.n.get(block, 0)
        self.n[block] = n + 1
        self.node.logs.append({"address": address, "blockNumber": hex(block), "transactionHash": tx,
                               "logIndex": hex(n), "topics": topics, "data": "0x" + data})

    def v3(self, tx, block, pool, sender, a0, a1, kind=V3):
        extra = word(0) * (2 if kind == PCS else 0)
        self._log(tx, block, pool, [kind, pad(sender), pad(sender)], s256(a0) + s256(a1) + word(1) + word(1)
                  + word(0) + extra)

    def v2(self, tx, block, pool, sender, a0in, a1in, a0out, a1out):
        self._log(tx, block, pool, [V2, pad(sender), pad(sender)], word(a0in) + word(a1in) + word(a0out) + word(a1out))

    def bal(self, tx, block, token_in, token_out, amount_in, amount_out):
        self._log(tx, block, VAULT, [BAL, BAL_POOL, pad(token_in), pad(token_out)], word(amount_in) + word(amount_out))

    def receipt(self, tx, block, sender, index, boosted=False):
        self.node.receipts[tx] = {"transactionHash": tx, "blockNumber": hex(block), "status": "0x1", "from": sender,
                                  "to": "0x" + "cc" * 20, "transactionIndex": hex(index), "gasUsed": hex(10**6),
                                  "effectiveGasPrice": hex(10**7), "timeboosted": boosted, "logs": []}


def setup():
    node = FakeNode(head=100_000)
    node.tokens[XYZ] = ("XYZ", 18)
    for pool, (t0, t1) in POOLS.items():
        node.handlers[(pool, "token0()")] = lambda d, b, t=t0: pad(t)
        node.handlers[(pool, "token1()")] = lambda d, b, t=t1: pad(t)
    k = Book(node)
    # 1000: 人が WETH を売る(1回だけの交換)→ すぐ後ろで BOT1 が P1 で安い WETH を買い、P2 で売る(+10 USDC)
    k.v3(k.tx(1000, "u"), 1000, P1, ROUTER, 10 * E18, -30_000 * E6)
    a1 = k.tx(1000, "a1")
    k.v3(a1, 1000, P1, BOT1, -1 * E18, 2_990 * E6)
    k.v2(a1, 1000, P2, BOT1, 0, 1 * E18, 3_000 * E6, 0)
    k.receipt(a1, 1000, "0x" + "e7" * 20, 2, boosted=True)
    # 2000: 人の乗り継ぎ(WETH → USDC → WBTC)。WETH が減るので裁定ではない
    u2 = k.tx(2000, "u2")
    k.v3(u2, 2000, P1, ROUTER, 1 * E18, -3_000 * E6)
    k.v3(u2, 2000, P4, ROUTER, -5 * 10**6, 3_000 * E6)
    # 3000: BOT2 が Balancer と P1 を回る(+5 USDC)。P1 の直前の交換は 2000
    a2 = k.tx(3000, "a2")
    k.bal(a2, 3000, USDC, WETH, 3_000 * E6, 1_002 * 10**15)
    k.v3(a2, 3000, P1, BOT2, 1_002 * 10**15, -3_005 * E6)
    k.receipt(a2, 3000, "0x" + "e8" * 20, 5)
    # 3500: BOT1 が WETH → WBTC → USDC → WETH(+0.01 WETH = 30ドル)
    a3 = k.tx(3500, "a3")
    k.v3(a3, 3500, P3, BOT1, 1 * E18, -5 * 10**6, kind=PCS)
    k.v3(a3, 3500, P4, BOT1, 5 * 10**6, -3_050 * E6)
    k.v3(a3, 3500, P1, BOT1, -1_010 * 10**15, 3_050 * E6)
    k.receipt(a3, 3500, "0x" + "e9" * 20, 7)
    # 4000: トークンの読めない交換の場を通る取引(見分けられないので入れない)
    x = k.tx(4000, "x")
    k.v3(x, 4000, PX, BOT1, 1 * E18, -1)
    k.v3(x, 4000, P1, BOT1, -2 * E18, 1)
    # 4000: 往復して損をした取引(USDC が減る)は裁定ではない
    lo = k.tx(4000, "lo")
    k.v3(lo, 4000, P1, ROUTER, -1 * E18, 3_000 * E6)
    k.v2(lo, 4000, P2, ROUTER, 0, 1 * E18, 2_998 * E6, 0)
    # 5000: BOT3 が値段表に無いトークン XYZ で回る(+1 XYZ。ドルにできない)
    a4 = k.tx(5000, "a4")
    k.v2(a4, 5000, P5, BOT3, 100 * E18, 0, 0, 1 * E18)
    k.v3(a4, 5000, P6, BOT3, -101 * E18, 1 * E18)
    return node, {"a1": a1, "a2": a2, "a3": a3, "a4": a4}


def make(conn, node, **kw):
    return arbs.ArbScanner(conn, make_chain(node), scanner=make_scanner(conn, node),
                           multicall="0xca11bde05977b3631167028862be2a173976ca11", log_blocks=10_000,
                           out=lambda s: None, **kw)


def rows(conn):
    return {r["tx_hash"]: r for r in conn.execute("SELECT * FROM chain_arbs").fetchall()}


def test_finds_round_trips_and_skips_ordinary_swaps(conn):
    node, tx = setup()
    sc = make(conn, node)
    assert sc.scan(1, 9_999) == 4
    assert sc.scan(1, 9_999) == 0                           # 2回目は足さない
    r = rows(conn)
    assert set(r) == set(tx.values())

    a1 = r[tx["a1"]]
    assert (a1["actor"], a1["swaps"], a1["kinds"], a1["path"]) == (BOT1, 2, "V3,V2", "USDC>WETH>USDC")
    assert a1["gross_usd"] == pytest.approx(10) and a1["profit_amount"] == str(10 * E6)
    assert (a1["trigger_block"], a1["trigger_seconds"]) == (1000, 0)     # 人の交換のすぐ後ろ

    a2 = r[tx["a2"]]
    assert (a2["actor"], a2["kinds"], a2["trigger_block"]) == (BOT2, "Balancer,V3", 2000)
    assert a2["gross_usd"] == pytest.approx(5) and abs(a2["trigger_seconds"] - 250) <= 1

    a3 = r[tx["a3"]]
    assert a3["path"] == "WETH>WBTC>USDC>WETH" and a3["kinds"] == "PancakeV3,V3,V3"
    assert a3["gross_usd"] == pytest.approx(30) and a3["trigger_block"] == 3000

    a4 = r[tx["a4"]]
    assert a4["actor"] == BOT3 and a4["gross_usd"] is None and a4["profit_token"] == XYZ

    # トークンの読めない交換の場も帳簿に残し、2回目からは読まない
    assert conn.execute("SELECT token0 FROM chain_pools WHERE pool = ?", (PX,)).fetchone()[0] == ""


def test_samples_receipts_of_top_actors(conn):
    node, tx = setup()
    sc = make(conn, node)
    sc.scan(1, 9_999)
    assert sc.sample(0, actors_n=2, per_actor=1) == 2      # BOT1 と BOT2 の、粗利の一番大きい裁定
    assert sc.sample(0, actors_n=2, per_actor=1) == 0      # 読んだ分は読み直さない
    a3 = rows(conn)[tx["a3"]]
    assert a3["sampled"] == 1 and a3["sender"] == "0x" + "e9" * 20 and a3["tx_index"] == 7
    assert a3["gas_usd"] == pytest.approx(0.03) and a3["timeboosted"] == 0
    assert rows(conn)[tx["a1"]]["sampled"] == 0


def test_summary_by_actor(conn):
    node, _ = setup()
    sc = make(conn, node)
    sc.scan(1, 9_999)
    sc.sample(0)
    s = arbs.summarize(conn, "arbitrum", 0)
    assert s["count"] == 4 and s["gross_usd"] == pytest.approx(45)
    assert [v["actor"] for v in s["actors"]] == [BOT1, BOT2, BOT3]
    b1 = s["actors"][0]
    assert (b1["count"], b1["same_block"], b1["with_trigger"], b1["median_swaps"]) == (2, 1, 2, 2.5)
    assert b1["tokens"] == ["USDC", "WETH"] and b1["boosted"] == 1 and b1["boost_known"] == 2
    assert b1["net_est"] == pytest.approx(40 - 0.06)
    assert s["actors"][2]["unpriced"] == 1
    assert {k: len(v) for k, v in s["sizes"].items() if v} == {"10ドル未満": 1, "100ドル未満": 2}


def test_velodrome_style_swap_reads_like_v2():
    solidly = topic("Swap(address,address,uint256,uint256,uint256,uint256)")
    s = arbs.parse_swap({"address": P2, "blockNumber": "0x1", "transactionHash": "0xab", "logIndex": "0x0",
                         "topics": [solidly, pad(BOT1), pad(BOT1)],
                         "data": "0x" + word(0) + word(3) + word(5) + word(0)})
    assert (s.kind, s.deltas) == ("Solidly", (5, -3))
    assert arbs.SCAN_KEY.startswith("arbs_") and len(arbs.SCAN_KEY) == 13


def test_parse_swap_signs():
    s = arbs.parse_swap({"address": P1, "blockNumber": "0x1", "transactionHash": "0xab", "logIndex": "0x0",
                         "topics": [V3, pad(BOT1), pad(BOT1)],
                         "data": "0x" + s256(-5) + s256(7) + word(0) * 3})
    assert s.deltas == (5, -7) and s.sender == BOT1 and s.kind == "V3"
    # 同じ名前で引数の並びが違う記録は読まない
    assert arbs.parse_swap({"address": P1, "blockNumber": "0x1", "transactionHash": "0xab", "logIndex": "0x0",
                            "topics": [V2], "data": "0x" + word(1) * 6}) is None


def test_arb_scan_command_prints_the_table(conn):
    node, _ = setup()
    lines: list[str] = []
    assert scan_command.run_arbs("arbitrum", 7, chain=make_chain(node), conn=conn, out=lines.append) == 0
    text = "\n".join(lines)
    assert "新しく帳簿に足した裁定 4件" in text
    assert "直近7時間、4件、主体 3、粗利 $45.00" in text
    assert "1位の取り分(粗利): 89%" in text
    assert f"1. {BOT1}  2件  $40.00  USDC・WETH  V3×3・V2×1・PancakeV3×1  2.5回  1/2件" in text
    assert "WETH>WBTC>USDC>WETH(3回)" in text


def test_window_starts_at_the_first_block_read(conn):
    """表の窓は、読み始めたブロックの時刻から数える(読み終えた時刻からではない)。"""
    node, _ = setup()
    hours = (99_900 - 991 + 1) / 14_400                    # ブロック 991 から読む
    lines: list[str] = []
    assert scan_command.run_arbs("arbitrum", hours, chain=make_chain(node), conn=conn, out=lines.append) == 0
    assert "新しく帳簿に足した裁定 4件" in "\n".join(lines)
    assert f"直近{hours:g}時間、4件" in "\n".join(lines)    # 最初の裁定(ブロック 1,000)も表に入る
