"""テスト用の偽のチェーンの接続先。ネットにはつながない。"""
import json
import time

import httpx

from factory.onchain.abi import address, selector, topic
from factory.onchain.actors import LIQUIDATION_CALL

A = "https://a.example/rpc"
B = "https://b.example/rpc"
C = "https://c.example/rpc"
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
LIQUIDATOR = "0x" + "ab" * 20
PROVIDER = "0x" + "a1" * 20
ORACLE = "0x" + "0c" * 20
WETH = "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
USDC = "0x" + "05" * 20
WBTC = "0x" + "0b" * 20

MULTICALL = "0xca11bde05977b3631167028862be2a173976ca11"
BORROW = topic("Borrow(address,address,address,uint256,uint8,uint256,uint16)")
SEL = {name: selector(name) for name in ("ADDRESSES_PROVIDER()", "getPriceOracle()", "BASE_CURRENCY_UNIT()",
                                         "getAssetPrice(address)", "decimals()", "symbol()",
                                         "aggregate3((address,bool,bytes)[])", "getUserAccountData(address)")}


def word(n: int) -> str:
    return f"{n:064x}"


def string_answer(s: str) -> str:
    raw = s.encode().hex()
    return "0x" + word(32) + word(len(s)) + raw.ljust(64 * max(1, -(-len(raw) // 64)), "0")


def split_aggregate3(data: str) -> list[tuple[str, str]]:
    """aggregate3 の data を (宛先, data) の並びに戻す(偽の接続先が読むため)。"""
    raw = bytes.fromhex(data[10:])
    w = lambda i: int.from_bytes(raw[i:i + 32], "big")
    arr = w(0)
    n, base = w(arr), arr + 32
    out = []
    for k in range(n):
        t = base + w(base + 32 * k)
        b = t + w(t + 64)
        out.append(("0x" + raw[t + 12:t + 32].hex(), "0x" + raw[b + 32:b + 32 + w(b)].hex()))
    return out


def join_results(results: list[tuple[bool, str]]) -> str:
    """(通ったか, data) の並びを aggregate3 の答えの形にする。"""
    parts = []
    for ok, data in results:
        body = data[2:]
        parts.append(word(int(ok)) + word(64) + word(len(body) // 2) + body.ljust(-(-len(body) // 64) * 64, "0"))
    offsets, pos = [], 32 * len(parts)
    for part in parts:
        offsets.append(word(pos))
        pos += len(part) // 2
    return "0x" + word(32) + word(len(parts)) + "".join(offsets) + "".join(parts)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeNode:
    """偽の接続先。URL ごとに振る舞いを変えられる。"""

    def __init__(self, head=100_000):
        self.head = head
        self.t0 = int(time.time()) - head // 4              # 1ブロック0.25秒で、最新のブロックが今
        self.calls: list[tuple[str, str]] = []           # (URL, 呼び方)
        self.mode: dict[str, str] = {}                     # URL → ok / rate / down / timeout / http500 / archive
        self.max_range: dict[str, int] = {}                # URL → eth_getLogs で読める最大ブロック数
        self.logs: list[dict] = []
        self.revert = False
        self.archive = True                                # 古いブロックの状態を読めるか
        self.prices = {WETH: 3000.0, USDC: 1.0, WBTC: 60000.0}      # ドル(古いブロックでも同じ)
        self.latest_prices: dict[str, float] = {}         # 今の値段だけ変えたい時
        self.tokens = {WETH: ("WETH", 18), USDC: ("USDC", 6), WBTC: ("WBTC", 8)}
        self.txs: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.accounts: dict[str, tuple] = {}   # 借り手 → (担保ドル, 借金ドル, 清算の線, 健康度)
        self.history: dict[str, object] = {}   # 借り手 → ブロックを受けて (担保ドル, 借金ドル, 清算の線, 健康度) を返す関数
        self.price_changes: list[tuple[int, str, float]] = []   # (このブロックから, トークン, ドル)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request):
        url = str(request.url)
        body = json.loads(request.content)
        for b in body if isinstance(body, list) else [body]:
            self.calls.append((url, b["method"]))
        mode = self.mode.get(url, "ok")
        if mode == "down":
            raise httpx.ConnectError("つながらない", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout("時間切れ", request=request)
        if mode == "rate":
            return httpx.Response(429, text="Too Many Requests")
        if mode == "http500":
            return httpx.Response(502, text="bad gateway")
        if isinstance(body, list):
            return httpx.Response(200, json=[self.answer(url, b) for b in body])
        if mode == "archive" and body["method"] == "eth_call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32000, "message": "missing trie node"}})
        return httpx.Response(200, json=self.answer(url, body))

    def answer(self, url, body):
        m, p, i = body["method"], body["params"], body["id"]

        def ok(r):
            return {"jsonrpc": "2.0", "id": i, "result": r}

        def err(code, message, data=None):
            e = {"code": code, "message": message}
            if data:
                e["data"] = data
            return {"jsonrpc": "2.0", "id": i, "error": e}

        if m == "eth_chainId":
            return ok(hex(42161))
        if m == "eth_blockNumber":
            return ok(hex(self.head))
        if m == "eth_getBlockByNumber":
            n = int(p[0], 16)
            return ok(None if n > self.head else {"number": p[0], "timestamp": hex(self.t0 + n // 4)})
        if m == "eth_getCode":
            return ok("0x6080")
        if m == "eth_getTransactionByHash":
            return ok(self.txs.get(p[0]))
        if m == "eth_getTransactionReceipt":
            if p[0] in self.receipts:
                return ok(self.receipts[p[0]])
            return ok({"transactionHash": p[0], "blockNumber": hex(int(p[0][-4:], 16)), "status": "0x1"})
        if m == "eth_call":
            if self.revert:
                return err(3, "execution reverted", "0x08c379a0")
            to, data, tag = p[0]["to"].lower(), p[0]["data"], p[1]
            sel = data[:10]
            if sel == SEL["ADDRESSES_PROVIDER()"] and to == POOL.lower():
                return ok("0x" + word(int(PROVIDER, 16)))
            if sel == SEL["getPriceOracle()"] and to == PROVIDER:
                return ok("0x" + word(int(ORACLE, 16)))
            if sel == SEL["BASE_CURRENCY_UNIT()"] and to == ORACLE:
                return ok("0x" + word(10**8))
            if sel == SEL["getAssetPrice(address)"] and to == ORACLE:
                asset = address(data[10:])
                if tag != "latest" and not self.archive:
                    return err(-32000, "missing trie node abc (path )")
                px = (self.latest_prices if tag == "latest" else {}).get(asset, self.prices.get(asset))
                at = self.head if tag == "latest" else int(tag, 16)
                for start, token, value in sorted(self.price_changes):
                    if token == asset and start <= at:
                        px = value
                if px is None:
                    return err(3, "execution reverted")
                return ok("0x" + word(int(px * 10**8)))
            if sel == SEL["decimals()"] and to in self.tokens:
                return ok("0x" + word(self.tokens[to][1]))
            if sel == SEL["symbol()"] and to in self.tokens:
                return ok(string_answer(self.tokens[to][0]))
            at = self.head if tag == "latest" else int(tag, 16)
            if sel == SEL["aggregate3((address,bool,bytes)[])"] and to == MULTICALL:
                return ok(join_results([self.account_answer(t, d, at) for t, d in split_aggregate3(data)]))
            if sel == SEL["getUserAccountData(address)"] and to == POOL.lower():
                if tag != "latest" and not self.archive:
                    return err(-32000, "missing trie node abc (path )")
                return ok(self.account_answer(to, data, at)[1])
            return ok("0x" + word(7))
        if m == "eth_getLogs":
            lo, hi = int(p[0]["fromBlock"], 16), int(p[0]["toBlock"], 16)
            limit = self.max_range.get(url)
            if limit and hi - lo + 1 > limit:
                return err(-32602, f"exceed maximum block range: {limit}")
            t0 = (p[0].get("topics") or [None])[0]
            want = (p[0].get("address") or "").lower()
            return ok([lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi
                       and (t0 is None or lg["topics"][0] == t0) and (not want or lg["address"].lower() == want)])
        return err(-32601, "method not found")

    def account_answer(self, target: str, data: str, block: int | None = None) -> tuple[bool, str]:
        if target != POOL.lower() or data[:10] != SEL["getUserAccountData(address)"]:
            return False, "0x"
        user = address(data[10:])
        if user in self.history:
            coll, debt, lt, hf = self.history[user](self.head if block is None else block)
        else:
            coll, debt, lt, hf = self.accounts.get(user, (0.0, 0.0, 0.0, None))
        hf_raw = 2**256 - 1 if hf is None else int(hf * 10**18)
        return True, "0x" + "".join(word(v) for v in (int(coll * 1e8), int(debt * 1e8), 0, int(lt * 1e4), 0, hf_raw))

    def add_borrow(self, block: int, user: str) -> None:
        self.logs.append({"address": POOL.lower(), "blockNumber": hex(block), "transactionHash": "0x" + f"{block:064x}",
                          "logIndex": "0x0",
                          "topics": [BORROW, "0x" + USDC[2:].rjust(64, "0"), "0x" + user[2:].rjust(64, "0"),
                                     "0x" + word(0)],
                          "data": "0x" + word(int(user, 16)) + word(10**6) + word(2) + word(5)})

    def add_liquidation(self, block: int, *, tx_hash: str | None = None, log_index: int = 3,
                        liquidator: str = LIQUIDATOR, sender: str = "0x" + "e0" * 20, to: str | None = None,
                        collateral: str = WETH, debt: str = USDC, debt_amount: int = 1_000 * 10**6,
                        collateral_amount: int = 35 * 10**16, gas_used: int = 1_000_000,
                        gas_price_wei: int = 10**7, extra_logs: list | None = None,
                        borrower: str = "0x" + "bb" * 20) -> str:
        """清算を1件足す(取引・受領書つき)。既定は 1,000 USDC を返して 0.35 WETH(1,050ドル)を受け取る。"""
        tx_hash = tx_hash or "0x" + f"{block:064x}"
        log = liquidation_log(block, liquidator, collateral=collateral, debt=debt, debt_amount=debt_amount,
                              collateral_amount=collateral_amount, tx_hash=tx_hash, log_index=log_index,
                              borrower=borrower)
        self.logs.append(log)
        self.txs[tx_hash] = {"hash": tx_hash, "from": sender, "to": to or liquidator, "blockNumber": hex(block),
                             "transactionIndex": "0x1", "gasPrice": hex(gas_price_wei)}
        rc = self.receipts.setdefault(tx_hash, {"transactionHash": tx_hash, "blockNumber": hex(block), "status": "0x1",
                                                "gasUsed": hex(gas_used), "effectiveGasPrice": hex(gas_price_wei),
                                                "logs": []})
        rc["logs"].extend((extra_logs or []) + [log])
        return tx_hash


def liquidation_log(block: int, liquidator: str = LIQUIDATOR, *, collateral: str = WETH, debt: str = USDC,
                    debt_amount: int = 10**6, collateral_amount: int = 2 * 10**6, tx_hash: str | None = None,
                    log_index: int = 0, borrower: str = "0x" + "bb" * 20) -> dict:
    data = "0x" + word(debt_amount) + word(collateral_amount) + liquidator[2:].rjust(64, "0") + word(0)
    return {"address": POOL.lower(), "blockNumber": hex(block), "transactionHash": tx_hash or "0x" + f"{block:064x}",
            "logIndex": hex(log_index),
            "topics": [LIQUIDATION_CALL, "0x" + collateral[2:].rjust(64, "0"), "0x" + debt[2:].rjust(64, "0"),
                       "0x" + borrower[2:].rjust(64, "0")],
            "data": data}
