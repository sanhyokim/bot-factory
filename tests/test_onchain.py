"""チェーンの道具(第0期): Keccak・回線の中継・読み取り・キャッシュ・回線の確かめ。ネットにはつながない。"""
import pytest

from factory import config
from factory.onchain import abi, check
from factory.onchain.cache import ChainCache
from factory.onchain.chain import Chain
from factory.onchain.keccak import keccak256
from factory.onchain.rpc import AllBusy, CallReverted, RangeTooLarge, Relay, RpcError
from tests.fakes_chain import (
    LIQUIDATOR,
    POOL,
    A,
    B,
    C,
    Clock,
    FakeNode,
    liquidation_log,
)


def make_relay(node, urls=(A, B, C), clock=None, **kw):
    clock = clock or Clock()
    return Relay("arbitrum", list(urls), client=node.client(), clock=clock, sleep=clock.sleep, **kw)


# --- Keccak・ABI ---
@pytest.mark.parametrize("data, digest", [
    (b"", "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"),
    (b"abc", "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
    (b"a" * 135, "34367dc248bbd832f4e3e69dfaac2f92638bd0bbd18f2912ba4ef454919cf446"),
    (b"a" * 136, "a6c4d403279fe3e0af03729caada8374b5ca54d8065329a3ebcaeb4b60aa386e"),
    (b"a" * 137, "d869f639c7046b4929fc92a4d988a8b22c55fbadb802c0c66ebcd484f1915f39"),
    (b"a" * 300, "5b7e0e47a96f32a88b4f14ca177982790807c40e1a105742ba0fc1babe1ef826"),
])
def test_keccak_matches_known_digests(data, digest):
    assert keccak256(data).hex() == digest


def test_topic_selector_and_words():
    assert abi.topic("Transfer(address,address,uint256)") == \
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    assert check.LIQUIDATION_CALL == "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
    assert abi.selector("transfer(address,uint256)") == "0xa9059cbb"
    assert abi.address("0x" + "00" * 12 + "AB" * 20) == "0x" + "ab" * 20
    assert abi.words("0x" + f"{5:064x}" + f"{6:064x}") == [5, 6]
    assert abi.to_int("0x1a") == 26
    call = abi.encode_call("balanceOf(address)", LIQUIDATOR)
    assert call == "0x70a08231" + LIQUIDATOR[2:].rjust(64, "0")
    with pytest.raises(ValueError):
        abi.words("0x1234")


# --- 回線の中継 ---
def test_relay_spreads_calls_and_rejects_send_methods():
    node = FakeNode()
    relay = make_relay(node)
    for _ in range(3):
        assert relay.call("eth_blockNumber") == hex(100_000)
    assert [u for u, _ in node.calls] == [A, B, C]          # 空いている所に回す
    with pytest.raises(ValueError):
        relay.call("eth_sendRawTransaction", ["0x00"])
    with pytest.raises(ValueError):
        relay.call("eth_blockNumber", purpose="何か")


def test_rate_limited_endpoint_rests_then_comes_back():
    node = FakeNode()
    node.mode[A] = "rate"
    clock = Clock()
    relay = make_relay(node, clock=clock, cooldown_rate=60)
    assert relay.call("eth_blockNumber") == hex(100_000)
    assert [u for u, _ in node.calls] == [A, B]
    node.calls.clear()
    node.mode[A] = "ok"
    for _ in range(4):
        relay.call("eth_blockNumber")
    assert A not in [u for u, _ in node.calls]               # 休み中は使わない
    clock.t += 61
    node.calls.clear()
    relay.call("eth_blockNumber")
    assert node.calls[0][0] == A                             # 休みが明けたら戻る
    assert relay.totals[(A, "research")]["rate_limited"] == 1


@pytest.mark.parametrize("mode", ["down", "timeout", "http500"])
def test_broken_endpoint_is_skipped(mode):
    node = FakeNode()
    node.mode[A] = mode
    relay = make_relay(node, cooldown_error=30)
    assert relay.call("eth_chainId") == hex(42161)
    assert relay.endpoints[0].cooldown_until > 0
    assert relay.totals[(A, "research")]["errors"] == 1


def test_revert_is_returned_without_resting_or_retrying():
    node = FakeNode()
    node.revert = True
    relay = make_relay(node)
    with pytest.raises(CallReverted) as e:
        relay.call("eth_call", [{"to": POOL, "data": "0x"}, "latest"])
    assert e.value.data == "0x08c379a0"
    assert len(node.calls) == 1 and all(ep.cooldown_until == 0 for ep in relay.endpoints)


def test_definite_error_tries_each_endpoint_once():
    node = FakeNode()
    for u in (A, B, C):
        node.mode[u] = "archive"            # 古いブロックの状態が無い接続先
    relay = make_relay(node)
    with pytest.raises(RpcError, match="missing trie node"):
        relay.call("eth_call", [{"to": POOL, "data": "0x"}, hex(5)])
    assert sorted(u for u, _ in node.calls) == [A, B, C]
    assert all(ep.cooldown_until == 0 for ep in relay.endpoints)


def test_research_gives_up_when_everything_rests_but_waits_if_it_can():
    node = FakeNode()
    for u in (A, B):
        node.mode[u] = "rate"
    clock = Clock()
    relay = make_relay(node, urls=(A, B), clock=clock, waits={"research": 10, "paper": 90}, cooldown_rate=60)
    with pytest.raises(AllBusy):
        relay.call("eth_blockNumber")
    node.mode[A] = "ok"
    assert relay.call("eth_blockNumber", purpose="paper") == hex(100_000)   # paper は休み明けまで待てる
    assert clock.t >= 1060


def test_each_purpose_has_its_own_share():
    node = FakeNode()
    clock = Clock()
    relay = make_relay(node, urls=(A,), clock=clock, per_minute=10,
                       shares={"research": 0.5, "paper": 0.3, "live": 0.2}, waits={"research": 0})
    for _ in range(5):
        relay.call("eth_blockNumber")
    with pytest.raises(AllBusy):
        relay.call("eth_blockNumber")                       # 調査の分(5回)を使い切った
    assert relay.call("eth_blockNumber", purpose="live") == hex(100_000)    # 本番の分は残っている
    clock.t += 61
    assert relay.call("eth_blockNumber") == hex(100_000)


def test_usage_is_written_to_the_ledger(conn, tmp_path):
    node = FakeNode()
    cache = ChainCache(tmp_path / "cache.db")
    relay = make_relay(node, cache=cache, usage_conn=conn)
    relay.call("eth_chainId", cache=True)
    relay.call("eth_chainId", cache=True)
    relay.flush()
    rows = {r["endpoint"]: r for r in conn.execute("SELECT * FROM rpc_usage")}
    assert rows[A]["calls"] == 1 and rows["cache"]["cache_hits"] == 1
    assert rows[A]["purpose"] == "research" and rows[A]["chain"] == "arbitrum"
    relay.call("eth_blockNumber")
    relay.flush()
    total = conn.execute("SELECT SUM(calls) FROM rpc_usage").fetchone()[0]
    assert total == 2
    cache.close()


# --- チェーンの読み取り ---
def test_logs_are_chunked_and_halved_when_refused(tmp_path):
    node = FakeNode(head=100_000)
    node.logs = [liquidation_log(b) for b in (10, 2_500, 2_501, 9_999)]
    node.max_range[A] = node.max_range[B] = node.max_range[C] = 2_500
    chain = Chain(make_relay(node), cache_depth=100, max_log_blocks=10_000)
    logs = chain.logs(from_block=1, to_block=10_000, address=POOL, topics=[check.LIQUIDATION_CALL])
    assert [int(lg["blockNumber"], 16) for lg in logs] == [10, 2_500, 2_501, 9_999]
    assert chain.log_step == 2_500                    # 10,000 → 5,000 → 2,500 で通った


def test_logs_give_up_instead_of_shrinking_forever():
    node = FakeNode(head=100_000)
    node.max_range[A] = node.max_range[B] = node.max_range[C] = -1     # 1ブロックでも断る
    chain = Chain(make_relay(node), cache_depth=100, max_log_blocks=10_000, min_log_blocks=100)
    with pytest.raises(RangeTooLarge, match="100ブロックずつにしても読めない"):
        chain.logs(from_block=1, to_block=50_000)
    assert len(node.calls) < 30                     # 何万回も呼ばない


def test_one_narrow_endpoint_does_not_shrink_the_others(tmp_path):
    node = FakeNode(head=200_000)
    node.logs = [liquidation_log(b) for b in (5, 15_000, 99_999)]
    node.max_range[C] = 50                            # 1つだけ、とても狭い接続先
    cache = ChainCache(tmp_path / "cache.db")
    relay = make_relay(node, cache=cache)
    chain = Chain(relay, cache_depth=100, max_log_blocks=10_000)
    logs = chain.logs(from_block=1, to_block=100_000, address=POOL, topics=[check.LIQUIDATION_CALL])
    assert len(logs) == 3 and chain.log_step == 10_000
    assert relay.endpoints[2].log_limit == 50 and "exceed maximum block range" in relay.endpoints[2].log_note
    assert relay.endpoints[2].cooldown_until == 0     # 休ませない(ほかの読み取りには使う)
    calls = len(node.calls)
    again = chain.logs(from_block=1, to_block=100_000, address=POOL, topics=[check.LIQUIDATION_CALL])
    assert len(again) == 3 and len(node.calls) == calls          # 2回目は全部キャッシュから
    cache.close()


def test_limit_in_the_message_is_used():
    node = FakeNode(head=100_000)
    for u in (A, B, C):
        node.max_range[u] = 2_000
    chain = Chain(make_relay(node), cache_depth=100, max_log_blocks=10_000)
    chain.logs(from_block=1, to_block=9_999)
    assert chain.log_step == 2_000                   # 半分ずつではなく、言われた上限に一度で合わせる


def test_quota_message_on_logs_is_rate_not_range():
    node = FakeNode(head=100_000)
    clock = Clock()
    relay = make_relay(node, urls=(A, B), clock=clock)
    orig = node.answer

    def answer(url, body):
        if url == A and body["method"] == "eth_getLogs":
            return {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32005, "message": "daily limit exceeded"}}
        return orig(url, body)

    node.answer = answer
    assert relay.call("eth_getLogs", [{"fromBlock": "0x1", "toBlock": "0x10"}]) == []
    a = relay.endpoints[0]
    assert a.log_limit is None and a.cooldown_until > clock.t
    assert relay.totals[(A, "research")]["rate_limited"] == 1

def refuse(node, url, message, when=lambda body: True, code=-32000):
    """url の接続先だけ、when に当たる呼び出しを message で断るようにする。"""
    orig = node.answer

    def answer(u, body):
        if u == url and when(body):
            return {"jsonrpc": "2.0", "id": body["id"], "error": {"code": code, "message": message}}
        return orig(u, body)

    node.answer = answer


def old_block(body):
    p = body["params"]
    return body["method"] == "eth_call" and p[1] != "latest"


def test_used_up_endpoint_rests_long():
    node = FakeNode()
    refuse(node, A, "You've reached the usage limit for your current plan")
    clock = Clock()
    relay = make_relay(node, urls=(A, B), clock=clock, cooldown_quota=1800)
    assert relay.call("eth_blockNumber") == hex(100_000)
    a = relay.endpoints[0]
    assert a.cooldown_until == clock.t + 1800 and "usage limit" in a.quota_note
    assert relay.totals[(A, "research")]["rate_limited"] == 1
    clock.t += 120                                           # 1分休ませるだけなら、もう戻っている
    node.calls.clear()
    for _ in range(5):
        relay.call("eth_blockNumber")
    assert A not in [u for u, _ in node.calls]


def test_endpoint_that_refuses_old_blocks_is_skipped_for_old_reads_only():
    node = FakeNode()
    refuse(node, A, "Archive requests require a personal token", old_block)
    clock = Clock()
    relay = make_relay(node, urls=(A, B), clock=clock, skip_after=3, skip_seconds=600)
    old = [{"to": POOL, "data": "0x"}, hex(5)]
    for _ in range(6):
        assert relay.call("eth_call", old) == "0x" + f"{7:064x}"
    a = relay.endpoints[0]
    assert a.strikes["eth_call@old"] == 3 and a.blocked["eth_call@old"] == clock.t + 600
    assert "personal token" in a.block_notes["eth_call@old"]
    node.calls.clear()
    for _ in range(5):
        relay.call("eth_call", old)
    assert [u for u, _ in node.calls] == [B] * 5             # 古いブロックの読み取りでは、もう A に行かない
    node.calls.clear()
    relay.call("eth_call", [{"to": POOL, "data": "0x"}, "latest"])
    relay.call("eth_call", [{"to": POOL, "data": "0x"}, "latest"])
    assert A in [u for u, _ in node.calls]                   # "latest" の読み取りは外さない
    assert a.cooldown_until == 0                             # 休ませてはいない
    clock.t += 601
    node.calls.clear()
    for _ in range(4):
        relay.call("eth_call", old)
    assert [u for u, _ in node.calls].count(A) == 1          # 10分たったら1回だけ試し、また外す
    assert a.blocked["eth_call@old"] > clock.t


def test_old_reads_are_not_skipped_when_no_endpoint_can_serve_them():
    node = FakeNode()
    for u in (A, B):
        refuse(node, u, "missing trie node", old_block)
    relay = make_relay(node, urls=(A, B))
    for _ in range(5):
        with pytest.raises(RpcError, match="missing trie node"):
            relay.call("eth_call", [{"to": POOL, "data": "0x"}, hex(5)])
    assert all(not ep.blocked for ep in relay.endpoints)    # 全部断るなら、頼み方の方を疑う(外さない)
    assert len(node.calls) == 10


def test_success_clears_the_count_of_refusals():
    node = FakeNode()
    flaky = {"on": True}
    refuse(node, A, "header not found", lambda body: flaky["on"] and body["method"] == "eth_getBlockByNumber")
    relay = make_relay(node, urls=(A, B), skip_after=3)
    for _ in range(2):
        relay.call("eth_getBlockByNumber", [hex(9), False])
    a = relay.endpoints[0]
    assert a.strikes["eth_getBlockByNumber@old"] == 2
    flaky["on"] = False
    for _ in range(4):
        relay.call("eth_getBlockByNumber", [hex(9), False])
    assert a.strikes["eth_getBlockByNumber@old"] == 0 and not a.blocked


def test_broken_endpoint_rests_longer_each_time():
    node = FakeNode()
    node.mode[A] = "down"
    clock = Clock()
    relay = make_relay(node, urls=(A, B), clock=clock, cooldown_error=30)
    a = relay.endpoints[0]
    rests = []
    for _ in range(6):
        start = clock.t
        relay.call("eth_chainId")
        assert node.calls[-2][0] == A                        # 休みが明けるたびに1回試す
        rests.append(a.cooldown_until - start)
        clock.t = a.cooldown_until + 1
    assert rests == [30, 60, 120, 240, 480, 600]             # 上限は10分
    node.mode[A] = "ok"
    relay.call("eth_chainId")
    assert a.error_streak == 0


def test_only_settled_blocks_are_cached(tmp_path):
    node = FakeNode(head=10_000)
    cache = ChainCache(tmp_path / "cache.db")
    chain = Chain(make_relay(node, cache=cache), cache_depth=100)
    chain.block(9_000)
    chain.block(9_000)
    chain.block(9_950)
    chain.block(9_950)
    assert [m for _, m in node.calls].count("eth_getBlockByNumber") == 3                            # 9,000 は2回目がキャッシュ、9,950 は確定前なので毎回読む
    chain.receipt("0x" + "00" * 30 + "2328")          # ブロック 9,000 の受領書
    chain.receipt("0x" + "00" * 30 + "2328")
    assert [m for _, m in node.calls].count("eth_getTransactionReceipt") == 1
    assert chain.call(POOL, "0x", block=9_000) == "0x" + f"{7:064x}"
    chain.call(POOL, "0x", block=9_000)
    chain.call(POOL, "0x")                            # "latest" はキャッシュしない
    chain.call(POOL, "0x")
    assert [m for _, m in node.calls].count("eth_call") == 3
    cache.close()


def test_chain_check_reports_endpoints_and_liquidators(conn, tmp_path, monkeypatch):
    node = FakeNode(head=400_000)
    node.mode[C] = "rate"
    node.max_range[A] = node.max_range[B] = 50_000
    node.logs = [liquidation_log(397_000), liquidation_log(390_000),
                 liquidation_log(380_000, "0x" + "cd" * 20)]
    cfg = config.load_config()
    chain_cfg = {**cfg["onchain"]["chains"]["arbitrum"], "endpoints": [A, B, C]}
    patched = {**cfg, "onchain": {**cfg["onchain"], "chains": {"arbitrum": chain_cfg}}}
    monkeypatch.setattr(config, "load_config", lambda *a: patched)
    lines: list[str] = []
    cache = ChainCache(tmp_path / "cache.db")
    ok = check.run("arbitrum", 24, client=node.client(), cache=cache, usage_conn=conn, out=lines.append)
    cache.close()
    text = "\n".join(lines)
    assert ok, text
    assert "使える接続先 2/3" in text and f"NG    {C}  HTTP 429" in text
    assert "ログ: 10,000ブロックまで読めた" in text
    assert "Aave v3 の記録(直近1,000ブロック、種類を問わず): 1件" in text
    assert "3件、清算した主体 2件" in text
    assert LIQUIDATOR in text
    assert "回線を使った回数 0回" in text
    assert conn.execute("SELECT SUM(cache_hits) FROM rpc_usage").fetchone()[0] > 0


def test_chain_check_lists_endpoints_skipped_for_old_reads(conn, tmp_path, monkeypatch):
    node = FakeNode(head=400_000)
    node.max_range[A] = 50_000
    refuse(node, B, "Archive requests require a personal token",
           lambda body: body["method"] in ("eth_getLogs", "eth_getBlockByNumber"))
    refuse(node, C, "You've reached the usage limit for your current plan")
    node.logs = [liquidation_log(397_000)]
    cfg = config.load_config()
    chain_cfg = {**cfg["onchain"]["chains"]["arbitrum"], "endpoints": [A, B, C]}
    monkeypatch.setattr(config, "load_config",
                        lambda *a: {**cfg, "onchain": {**cfg["onchain"], "chains": {"arbitrum": chain_cfg}}})
    lines: list[str] = []
    cache = ChainCache(tmp_path / "cache.db")
    ok = check.run("arbitrum", 24, client=node.client(), cache=cache, usage_conn=conn, out=lines.append)
    cache.close()
    text = "\n".join(lines)
    assert ok, text
    assert "eth_getLogs(古いブロック)「Archive requests require a personal token」" in text
    assert "使える量を使い切ったと言われた接続先(30分休ませた)" in text
    relayed = [m for u, m in node.calls if u == B and m == "eth_getLogs"]
    assert len(relayed) == 6 + 3                             # 直接の確かめ6回(広さを変えて) + 外すまでの3回


def test_chain_check_stops_when_no_endpoint_answers(tmp_path, monkeypatch):
    node = FakeNode()
    for u in (A, B):
        node.mode[u] = "down"
    cfg = config.load_config()
    chain_cfg = {**cfg["onchain"]["chains"]["arbitrum"], "endpoints": [A, B]}
    monkeypatch.setattr(config, "load_config",
                        lambda *a: {**cfg, "onchain": {**cfg["onchain"], "chains": {"arbitrum": chain_cfg}}})
    lines: list[str] = []
    assert not check.run("arbitrum", 1, client=node.client(), out=lines.append)
    assert "使える接続先が1つもない" in "\n".join(lines)


def test_probe_logs_reports_the_widest_span_and_the_refusal():
    from factory.onchain.rpc import probe_logs
    node = FakeNode(head=100_000)
    node.max_range[A] = 1_000
    node.mode[B] = "http500"
    a = probe_logs(node.client(), A, POOL, 100_000)
    assert a["max_ok"] == 1_000 and "10,000ブロック: exceed maximum block range: 1000" in a["error"]
    b = probe_logs(node.client(), B, POOL, 100_000)
    assert b["max_ok"] is None and "HTTP 502" in b["error"]
