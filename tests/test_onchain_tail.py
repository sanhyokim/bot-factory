"""裾野の調べ(tail-survey): 市場の値段・接続先の選び方・清算と取り残しの集計を、偽の接続先で確かめる。"""
import json
import sqlite3
import time

import httpx
import pytest

from factory.onchain import tail
from factory.onchain.chain import Chain
from factory.onchain.rpc import Relay
from tests.fakes_chain import (
    POOL,
    PROVIDER,
    USDC,
    WETH,
    A,
    B,
    C,
    Clock,
    FakeNode,
    word,
)
from tests.test_onchain_compound import CETH, COMP, CUSDC, U1, U2, U3, liquidate
from tests.test_onchain_stranded import BOT, Lending, arr, borrow

REG = "0x" + "a9" * 20                                   # Aave の写しの住所録の一覧
A_WETH, V_WETH, S_WETH = ("0x" + c * 20 for c in ("71", "72", "73"))
A_USDC, V_USDC, S_USDC = ("0x" + c * 20 for c in ("74", "75", "76"))
UA, UB = "0x" + "91" * 20, "0x" + "92" * 20
NOW = {tail.NATIVE: 2_600.0, WETH: 3_000.0, USDC: 1.0}   # 市場の今の値段
PAST = {tail.NATIVE: 2_700.0, WETH: 3_000.0, USDC: 1.0}  # 清算の時刻の値段
DEC = {tail.NATIVE: 18, WETH: 18, USDC: 6}


class Llama:
    """偽の値段表。coins.llama.fi と同じ形で答える。"""

    def __init__(self, now=None):
        self.paths: list[str] = []
        self.now = now or int(time.time())
        self.extra: dict[str, dict] = {}      # トークン → 答えを上書きする分(confidence・timestamp など)

    def answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        past = "/historical/" in path
        table = PAST if past else NOW
        at = int(path.split("/")[3]) if past else self.now
        coins = {}
        for coin in path.rsplit("/", 1)[1].split(","):
            token = coin.split(":", 1)[1]
            if token in table:
                coins[coin] = {"price": table[token], "decimals": DEC[token], "symbol": token[:6],
                               "timestamp": at, "confidence": 0.99, **self.extra.get(token, {})}
        return httpx.Response(200, json={"coins": coins})


def client_for(node: FakeNode, llama: Llama) -> httpx.Client:
    def route(request: httpx.Request) -> httpx.Response:
        return llama.answer(request) if "llama" in request.url.host else node.handle(request)
    return httpx.Client(transport=httpx.MockTransport(route))


def test_prices_drop_doubtful_and_old_quotes_and_remember_answers():
    llama = Llama()
    llama.extra = {WETH: {"confidence": 0.5}, USDC: {"symbol": "US<DC>\n!!"}}
    p = tail.Prices(client_for(FakeNode(), llama), "fuse", clock=lambda: llama.now)
    got = p.current([WETH, USDC, tail.NATIVE, "0x" + "ee" * 20])
    assert set(got) == {USDC, tail.NATIVE}                 # 確からしさの低い値段と、値段表に無いトークンは値段なし
    assert got[USDC].symbol == "USDC" and got[USDC].decimals == 6      # 名前は文字と数字だけにする
    assert llama.paths == [f"/prices/current/fuse:{WETH},fuse:{USDC},fuse:{tail.NATIVE},fuse:0x{'ee' * 20}"]
    p.current([USDC, WETH])
    assert len(llama.paths) == 1                           # 2回目は読みに行かない
    llama.extra = {USDC: {"timestamp": llama.now - 3 * 86400}}
    stale = tail.Prices(client_for(FakeNode(), llama), "fuse", clock=lambda: llama.now)
    assert USDC not in stale.current([USDC])               # 古い値段は使わない
    llama.extra = {}
    p.load_past({1_000_200: {WETH, USDC}, 1_000_500: {USDC}})
    assert p.past(1_000_200, WETH).price == 3_000 and p.past(1_000_500, USDC).price == 1
    assert sum("/historical/" in x for x in llama.paths) == 2          # 時刻ごとに1回


def test_prices_survive_a_broken_price_table():
    def broken(request):
        return httpx.Response(502, text="bad gateway")
    p = tail.Prices(httpx.Client(transport=httpx.MockTransport(broken)), "fuse", sleep=lambda s: None)
    assert p.current([USDC]) == {} and p.failed == 1 and p.calls == 2      # 1回だけやり直して、値段なしにする


def test_endpoints_need_no_key_and_the_right_chain(tmp_path):
    allow = tmp_path / "domains.txt"
    allow.write_text("example\n*.fuse.io\n", encoding="utf-8")
    domains = tail.load_domains(allow)
    assert domains == ["example", "fuse.io"]
    assert tail.usable("https://rpc.fuse.io/x", (), domains) and tail.usable(A, (), domains)
    assert not tail.usable("https://rpc.other.io", (), domains)            # 許可していないホスト
    assert not tail.usable("https://rpc.fuse.io/${KEY}", (), domains)      # 鍵を入れる形
    assert not tail.usable("http://rpc.fuse.io", (), domains) and not tail.usable("wss://rpc.fuse.io", (), domains)
    assert not tail.usable("https://lb.routeme.sh/rpc", ("routeme.sh",), None)
    node = FakeNode()
    node.mode[B] = "down"
    good, bad = tail.pick_endpoints(node.client(), [A, B, C], 42161)
    assert set(good) == {A, C} and len(bad) == 1 and bad[0].startswith("b.example")
    good, bad = tail.pick_endpoints(node.client(), [A, C], 1)              # チェーン番号が違う接続先は使わない
    assert good == [] and all("チェーン番号が違う" in x for x in bad)


def test_price_unit_follows_the_markets_that_agree():
    assert tail.consensus([]) is None
    assert tail.consensus([1.0, 1.02, 0.4], hint=1.0) == 1.0               # 崩れた1つに引きずられない
    assert tail.consensus([3_000.0, 3_010.0, 1.0], hint=1.0) == pytest.approx(3_005)    # ETH 建ての値段表
    assert tail.consensus([1e-8, 0.5e-8], hint=1e-8) == 1e-8               # 2つしか無ければ、値段表が言う単位を取る
    assert tail.consensus([2.0, 4.0]) in (2.0, 4.0)


def test_market_profit_uses_market_prices_on_both_sides():
    row = {"repay_usd": 500.0, "profit_usd": 40.0, "market_ratio": 1.0, "repay_ratio": 1.0, "cash_ok": 1, "allowed": 0}
    assert tail.market_profit(row) == pytest.approx(40.0) and tail.takeable(row)
    # 担保の値段が仕組みの値段表だけ高い(市場の2倍)なら、受け取る担保は市場では半分の値打ちしかない
    high = {**row, "market_ratio": 2.0}
    assert tail.market_profit(high) == pytest.approx(540 / 2 - 500) and not tail.takeable(high)
    # 返すトークンが仕組みの値段表だけ安い(市場の半分)なら、市場で買うと2倍かかる
    cheap = {**row, "repay_ratio": 0.5}
    assert tail.market_profit(cheap) == pytest.approx(540 - 1_000) and not tail.takeable(cheap)
    assert tail.market_profit({**row, "market_ratio": None}) is None       # 比べられなければ、取れるとはしない
    assert not tail.takeable({**row, "market_ratio": None})
    assert not tail.takeable({**row, "cash_ok": 0}) and not tail.takeable({**row, "allowed": 3})
    assert tail.takeable({**row, "cash_ok": None, "allowed": None})


def setup(node: FakeNode) -> None:
    """Compound の写し1つ(借り手 U1・U2 が清算できる)と、Aave の写し1つ(借り手 UA が清算できる)。"""
    Lending(node)
    h = node.handlers
    addr = lambda a: lambda d, b: "0x" + word(int(a, 16))
    borrow(node, 70_000, CUSDC, U1)
    borrow(node, 71_000, CUSDC, U2)
    liquidate(node, 90_000, CUSDC, U3, BOT, 1_000 * 10**6, CETH, 2 * 10**9)     # 0.4 ETH を受け取る
    pool = POOL.lower()
    h[(REG, "getAddressesProvidersList()")] = lambda d, b: arr(PROVIDER)
    h[(REG, "getReservesList()")] = lambda d, b: None
    h[(PROVIDER, "getPool()")] = addr(pool)
    h[(pool, "getReservesList()")] = lambda d, b: arr(WETH, USDC)
    h[(pool, "getUserAccountData(address)")] = lambda d, b: node.account_answer(pool, d, b)[1]
    tokens = {WETH: (A_WETH, S_WETH, V_WETH), USDC: (A_USDC, S_USDC, V_USDC)}

    def reserve(d, b):      # Aave v3 の写しの並び(8語目が番号、9語目から aToken・固定の借金・変動の借金)
        a, s, v = tokens["0x" + d[-40:]]
        return "0x" + word(10_500 << 32) + word(0) * 6 + word(3) + "".join(word(int(x, 16)) for x in (a, s, v)) \
            + word(0) * 4
    h[(pool, "getReserveData(address)")] = reserve
    held = {(A_WETH, UA): 2 * 10**18 // 3, (V_USDC, UA): 1_900 * 10**6, (WETH, A_WETH): 10 * 10**18}
    def balance(token):
        return lambda d, b: "0x" + word(held.get((token, "0x" + d[-40:]), 0))
    for t in (A_WETH, S_WETH, V_WETH, A_USDC, S_USDC, V_USDC, WETH):
        h[(t, "balanceOf(address)")] = balance(t)
    node.add_borrow(80_000, UA)
    node.add_borrow(81_000, UB)
    node.accounts[UA] = (2_000.0, 1_900.0, 0.8, 0.84)        # 健康度が1を割っている
    node.accounts[UB] = (2_000.0, 500.0, 0.8, 3.2)
    node.add_liquidation(95_000, borrower=UB)                # 1,000 USDC を返して 0.35 WETH を受け取る


TARGET = {"chain": "arbitrum", "id": 42161, "rpcs": [A, B, "https://key.example/${KEY}"],
          "compound": [["compfork", "comptroller", COMP, False], ["gone", "comptroller", "0x" + "f1" * 20, False]],
          "aave": [["aavefork", "short", REG, True]]}


def test_survey_values_liquidations_and_finds_takeable_stranded_money(tmp_path):
    node = FakeNode(head=100_000)
    setup(node)
    node.handlers[("0x" + "f1" * 20, "getAllMarkets()")] = lambda d, b: None
    node.handlers[("0x" + "f1" * 20, "getAlliTokens()")] = lambda d, b: None
    llama = Llama()
    cfg = dict(tail.DEFAULTS)
    said: list[str] = []
    r = tail.survey_chain(TARGET, out_dir=tmp_path, cfg=cfg, days=1, client=client_for(node, llama), out=said.append)
    assert r["status"] == "ok" and r["endpoints"] == ["a.example", "b.example"]     # 鍵を入れる形の接続先は使わない
    assert json.loads((tmp_path / "arbitrum.json").read_text(encoding="utf-8"))["chain"] == "arbitrum"
    places = {p["name"]: p for p in r["places"]}
    assert places["gone"]["status"] == "読めない"

    comp = places["compfork"]
    assert comp["kind"] == "Compound" and comp["markets"] == 2 and comp["address"] == COMP
    assert comp["liquidations"] == 1 and comp["liquidators"] == 1 and comp["top_share"] == 1.0
    # 0.4 ETH × その時刻の市場の値段 2,700 = 1,080ドル。返した額は 1,000ドル
    assert comp["repay_usd"] == pytest.approx(1_000) and comp["profit_usd"] == pytest.approx(80)
    assert comp["borrowers"] == 3 and comp["open"] == 2      # U1 と、利息を足すと割れている U2(U3 は清算できない)
    # U1: 500ドル返して 500 × 1.08 ×(1 − 0.028)を受け取る。値段は市場と合い、現金もあり、仕組みも清算してよいと答える
    assert comp["takeable"] == 1 and comp["takeable_usd"] == pytest.approx(500 * (1.08 * 0.972 - 1))

    aave = places["aavefork"]
    assert aave["kind"] == "Aave" and aave["insolvent"] and aave["address"] == POOL.lower() and aave["markets"] == 2
    assert aave["liquidations"] == 1 and aave["profit_usd"] == pytest.approx(0.35 * 3_000 - 1_000)
    assert aave["borrowers"] == 2 and aave["open"] == 1
    # UA: 借金 1,900 の半分を返し、上乗せ5%を受け取る
    assert aave["takeable"] == 1 and aave["takeable_usd"] == pytest.approx(950 * 0.05)
    ua = next(s for s in r["stranded"] if s["borrower"] == UA)
    assert ua["market_ratio"] == pytest.approx(1) and ua["repay_ratio"] == pytest.approx(1) and ua["cash_ok"] == 1
    assert ua["collateral_symbol"] == WETH[:6] and ua["debt_usd"] == pytest.approx(1_900)

    lines = tail.describe(tail.load_results(tmp_path))
    text = "\n".join(lines)
    assert "| arbitrum | aavefork(返されない借金あり) | Aave |" in text and "| arbitrum | compfork | Compound |" in text
    assert "上限は線を超えている" in text                     # 読めた日数が短いので、30日あたりに直すと線を超える
    assert "読めなかったチェーン" not in text


def test_stranded_money_is_not_takeable_when_the_protocol_price_is_far_from_the_market(tmp_path):
    node = FakeNode(head=100_000)
    setup(node)
    node.latest_prices[WETH] = 6_000.0          # 仕組みの値段表だけが高い(市場は 3,000)
    r = tail.survey_chain({**TARGET, "compound": []}, out_dir=tmp_path, cfg=dict(tail.DEFAULTS), days=1, client=client_for(node, Llama()), out=lambda s: None)
    ua = next(s for s in r["stranded"] if s["borrower"] == UA)
    assert ua["market_ratio"] == pytest.approx(2) and not ua["takeable"] and ua["market_profit_usd"] < 0
    assert r["places"][0]["open"] == 1 and r["places"][0]["takeable"] == 0 and r["places"][0]["takeable_usd"] == 0


def test_chain_without_usable_endpoints_is_recorded_with_the_reason(tmp_path):
    node = FakeNode()
    node.mode[A] = node.mode[B] = "down"
    r = tail.survey_chain(TARGET, out_dir=tmp_path, cfg=dict(tail.DEFAULTS), client=client_for(node, Llama()),
                          out=lambda s: None)
    assert r["status"] == "接続先が無い" and "a.example" in r["note"]
    text = "\n".join(tail.describe(tail.load_results(tmp_path)))
    assert "読めなかったチェーン" in text and "| arbitrum | 接続先が無い |" in text
    assert "上限と取り残しを足しても届かない" in text


def test_sweep_keeps_the_days_it_could_read_when_old_logs_are_refused():
    node = FakeNode(head=1_000_000)
    setup(node)
    borrow(node, 900_000, CUSDC, U1)
    inner = node.handle

    def handle(request: httpx.Request):
        body = json.loads(request.content)
        if isinstance(body, dict) and body["method"] == "eth_getLogs" and int(body["params"][0]["toBlock"], 16) < 600_000:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32000, "message": "history has been pruned"}})
        return inner(request)
    clock = Clock()
    relay = Relay("arbitrum", [A, B], client=httpx.Client(transport=httpx.MockTransport(handle)), clock=clock,
                  sleep=clock.sleep)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    s = tail.Surveyor(conn, Chain(relay, cache_depth=20), tail.Prices(client_for(node, Llama()), "arbitrum"),
                      days=3, out=lambda s: None)
    logs, days_read, why = s.sweep([CUSDC, POOL.lower()], 999_980, 0.25)
    # 新しい方から20万ブロックずつ読む。2回目は60万より前のブロックにかかって断られる。読めた1回目の分は残る
    assert days_read == pytest.approx(200_000 * 0.25 / 86400) and "記録が読めなくなった" in why
    assert [int(lg["blockNumber"], 16) for lg in logs] == [900_000]


def test_run_skips_chains_that_already_have_results(tmp_path, monkeypatch):
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps([TARGET, {**TARGET, "chain": "other"}]), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "other.json").write_text(json.dumps({"chain": "other", "places": [], "status": "接続先が無い"}),
                                    encoding="utf-8")
    seen = []

    def fake(target, **kw):
        seen.append(target["chain"])
        r = {"chain": target["chain"], "places": [], "liquidations": [], "stranded": [], "status": "接続先が無い"}
        (kw["out_dir"] / f"{target['chain']}.json").write_text(json.dumps(r), encoding="utf-8")
        return r
    monkeypatch.setattr(tail, "survey_chain", fake)
    said: list[str] = []
    assert tail.run(targets, out, workers=1, out=said.append) == 0
    assert seen == ["arbitrum"] and (out / "summary.md").exists()
    assert tail.run(targets, out, workers=1, redo=True, chains=["other"], out=said.append) == 0
    assert seen == ["arbitrum", "other"]
