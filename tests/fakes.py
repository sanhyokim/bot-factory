"""テスト用の偽の取引所(Hyperliquid)と偽のAI。ネットにはつながない。"""
import json
import math

import httpx

from factory.hyperliquid.reader import INTERVAL_MS, now_ms

HOUR = 3_600_000
DAY = 24 * HOUR


def price_at(t: int, base: float) -> float:
    """ゆっくりした波と速い波を重ねた値動き(決まった式なので何度呼んでも同じ)。"""
    return base * (1 + 0.03 * math.sin(t / (6 * HOUR)) + 0.01 * math.sin(t / (1.3 * HOUR)))


def trader_fills(coin: str, start_ms: int, n_trades: int, *, notional: float = 500.0, price: float = 60000.0,
                 gap: int = 6 * HOUR, win_every: int = 3) -> list[dict]:
    """買って1時間後に売る取引を n_trades 回。win_every 回に1回は小さく負ける。"""
    out = []
    t = start_ms
    sz = round(notional / price, 5)
    for i in range(n_trades):
        px_out = price * (0.995 if win_every and i % win_every == win_every - 1 else 1.01)
        fee_in, fee_out = price * sz * 0.00045, px_out * sz * 0.00045
        out.append({"coin": coin, "px": str(price), "sz": str(sz), "side": "B", "time": t, "dir": "Open Long",
                    "closedPnl": "0.0", "fee": str(fee_in), "tid": 2 * i, "startPosition": "0.0"})
        out.append({"coin": coin, "px": str(px_out), "sz": str(sz), "side": "A", "time": t + HOUR,
                    "dir": "Close Long", "closedPnl": str((px_out - price) * sz), "fee": str(fee_out),
                    "tid": 2 * i + 1, "startPosition": str(sz)})
        t += gap
    return out


def board_row(address: str, *, account_value: float = 10_000, month_pnl: float = 1_500) -> dict:
    return {"ethAddress": address, "accountValue": str(account_value), "displayName": None,
            "windowPerformances": [["day", {"pnl": "0", "roi": "0", "vlm": "0"}],
                                   ["month", {"pnl": str(month_pnl), "roi": "0.1", "vlm": "100000"}]]}


class FakeExchange:
    def __init__(self, *, coins=None, fills=None, board=None):
        self.coins = coins or {"BTC": (60000.0, 5), "ETH": (3000.0, 4)}
        self.fills = fills or {}
        self.board = board or []
        self.funding_fn = {}            # 銘柄 → 時刻から1時間あたりの率を返す関数(既定は小さな波)
        self.volumes = {}               # 銘柄 → 直近24時間の出来高(ドル)。既定は500万ドル
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = set()

    def candle(self, coin: str, t: int, step: int, interval: str) -> dict:
        base = self.coins[coin][0]
        o, c = price_at(t, base), price_at(t + step, base)
        return {"t": t, "T": t + step - 1, "s": coin, "i": interval, "o": str(o), "c": str(c),
                "h": str(max(o, c) * 1.002), "l": str(min(o, c) * 0.998), "v": str(10 + (t // step) % 7), "n": 5}

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.calls.append(("leaderboard", {}))
            if "leaderboard" in self.fail:
                return httpx.Response(500)
            return httpx.Response(200, json={"leaderboardRows": self.board})
        body = json.loads(request.content)
        typ = body["type"]
        self.calls.append((typ, body))
        if typ in self.fail:
            return httpx.Response(500)
        now = now_ms()
        if typ == "meta":
            return httpx.Response(200, json={"universe": [{"name": k, "szDecimals": d}
                                                          for k, (_, d) in self.coins.items()]})
        if typ == "metaAndAssetCtxs":
            meta = {"universe": [{"name": k, "szDecimals": d} for k, (_, d) in self.coins.items()]}
            ctxs = [{"funding": str(self.rate(k, now)), "dayNtlVlm": str(self.volumes.get(k, 5_000_000)),
                     "markPx": str(price_at(now, base))} for k, (base, _) in self.coins.items()]
            return httpx.Response(200, json=[meta, ctxs])
        if typ == "candleSnapshot":
            req = body["req"]
            if req["coin"] not in self.coins:
                return httpx.Response(200, json=[])
            step = INTERVAL_MS[req["interval"]]
            start = max(req["startTime"], now - 5000 * step)    # 取引所は直近5000本まで
            first = start - start % step + (step if start % step else 0)
            end = min(req["endTime"], now)
            return httpx.Response(200, json=[self.candle(req["coin"], t, step, req["interval"])
                                             for t in range(first, end + 1, step)])
        if typ == "fundingHistory":
            if body["coin"] not in self.coins:
                return httpx.Response(200, json=[])
            start = body["startTime"]
            first = start - start % HOUR + (HOUR if start % HOUR else 0)
            end = min(body.get("endTime", now), now)
            rows = [{"coin": body["coin"], "fundingRate": str(self.rate(body["coin"], t)),
                     "premium": "0", "time": t} for t in range(first, end + 1, HOUR)]
            return httpx.Response(200, json=rows[:500])
        if typ == "userFillsByTime":
            rows = sorted((f for f in self.fills.get(body["user"], [])
                           if body["startTime"] <= f["time"] <= body.get("endTime", now)), key=lambda f: f["time"])
            return httpx.Response(200, json=rows[:2000])
        return httpx.Response(400)

    def rate(self, coin: str, t: int) -> float:
        fn = self.funding_fn.get(coin)
        return fn(t) if fn else 0.0000125 * (1 + math.sin(t / (12 * HOUR)))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def count(self, typ: str) -> int:
        return sum(1 for t, _ in self.calls if t == typ)


BREAKOUT_CODE = '''LOOKBACK = 20


def entry_signal(history):
    if len(history) <= LOOKBACK:
        return None
    if history[-1].c > max(history.highs(LOOKBACK + 1)[:-1]):
        return "long"
    return None


def exit_signal(history, position):
    return False
'''

GOOD_HYPOTHESIS = {"symbol": "BTC", "timeframe": "1h", "side": "long", "entry_kind": "高値抜け", "n": 20, "x": 0,
                   "take_profit_pct": 1.5, "stop_loss_pct": 0.8,
                   "summary": "BTCの1時間足で直前20本の高値を抜けたら買い、+1.5%で利益確定・−0.8%で損切り",
                   "hypothesis": "上昇の勢いが出た所で入り、短く利益を取っている。"}
RULE = {"symbol": "BTC", "timeframe": "1h", "entry_condition": "終値が直前20本の高値を上に抜けたら買い",
        "exit_condition": "+1.5%で利益確定、−0.8%で損切り、最長12時間", "take_profit_pct": 1.5, "stop_loss_pct": 0.8,
        "max_hold_hours": 12, "max_trades_per_day": 4, "continuity": "常にある", "competition": "競争あり"}
DISTORTION_RULE = {"symbol": "BTC", "funding_y_pct": 0.0005, "max_hold_hours": 24, "stop_loss_pct": 2.0,
                   "max_trades_per_day": 3, "continuity": "常にある", "competition": "競争なし"}


class FakeAI:
    """工程ごとに決まった答えを返す偽のAI(ai_gateway.mock_responder に差す)。"""

    def __init__(self):
        self.hypotheses = [GOOD_HYPOTHESIS,
                           {**GOOD_HYPOTHESIS, "entry_kind": "急騰", "n": 3, "x": 2, "take_profit_pct": 0.2,
                            "summary": "BTCが3時間で2%上げたら買い、+0.2%で利益確定"},
                           {**GOOD_HYPOTHESIS, "symbol": "NOPE", "summary": "NOPEの高値抜けで買い"}]
        self.rule = dict(RULE)
        self.distortion_rule = dict(DISTORTION_RULE)
        self.code = BREAKOUT_CODE
        self.repair_answers: list[str] = []
        self.calls: list[str] = []

    def __call__(self, stage: str, prompt: str, schema):
        self.calls.append(stage)
        if stage == "explore":
            return json.dumps({"hypotheses": self.hypotheses}, ensure_ascii=False)
        if stage == "investigate":
            return "値幅が小さく、手数料と滑りで利益が消えるため見送り"
        if stage == "design":
            rule = self.distortion_rule if "funding_y_pct" in str(schema) else self.rule
            return json.dumps(rule, ensure_ascii=False)
        if stage == "implement":
            return f"書きました。\n```python\n{self.code}```"
        if stage == "repair":
            return self.repair_answers.pop(0) if self.repair_answers else f"```python\n{BREAKOUT_CODE}```"
        if stage == "refute":
            return "前半も後半も利益が出ており、手数料が倍でも黒字。1回の大勝ちに頼っていない。"
        return "[fake]"
