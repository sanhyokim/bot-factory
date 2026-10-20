import json

import httpx
import pytest

from factory.hyperliquid.reader import Reader, ReaderError, interval_code
from tests.helpers import HOUR, STEP, T0


def make_reader(handler):
    return Reader(httpx.Client(transport=httpx.MockTransport(handler)), retries=2)


def test_candles_paginate_and_parse():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        req = body["req"]
        assert body["type"] == "candleSnapshot" and req["interval"] == "15m"
        rows = [{"t": t, "T": t + STEP - 1, "s": "BTC", "i": "15m", "o": "1", "h": "2", "l": "0.5",
                 "c": "1.5", "v": "10", "n": 3} for t in range(req["startTime"], req["endTime"], STEP)]
        return httpx.Response(200, json=rows)

    cs = make_reader(handler).candles("BTC", "15分", T0, T0 + 6000 * STEP)
    assert len(cs) == 6000 and len(calls) == 2
    assert cs[0].t == T0 and cs[0].h == 2.0 and cs[-1].t == T0 + 5999 * STEP


def test_funding_paginates():
    def handler(request):
        body = json.loads(request.content)
        start = body["startTime"] + (-(body["startTime"] - T0) % HOUR)   # 毎時ちょうど
        rows = [{"coin": "BTC", "fundingRate": "0.0000125", "premium": "0", "time": t}
                for t in range(start, min(start + 500 * HOUR, body["endTime"]), HOUR)]
        return httpx.Response(200, json=rows)

    fs = make_reader(handler).funding_history("BTC", T0, T0 + 720 * HOUR)
    assert len(fs) == 720 and fs[0].rate == pytest.approx(0.0000125)


def test_leaderboard_and_fills_and_meta():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"leaderboardRows": [{
                "ethAddress": "0xabc", "accountValue": "1234.5", "displayName": None, "prize": 0,
                "windowPerformances": [["day", {"pnl": "1", "roi": "0.01", "vlm": "5"}],
                                       ["month", {"pnl": "300", "roi": "0.2", "vlm": "90000"}]]}]})
        body = json.loads(request.content)
        if body["type"] == "meta":
            return httpx.Response(200, json={"universe": [{"name": "BTC", "szDecimals": 5}]})
        return httpx.Response(200, json=[{"coin": "ETH", "px": "2500.1", "sz": "0.1", "side": "B",
                                          "time": T0 + 5, "dir": "Open Long", "closedPnl": "0",
                                          "fee": "0.11", "tid": 1}])

    r = make_reader(handler)
    row = r.leaderboard()[0]
    assert row["address"] == "0xabc" and row["month_pnl"] == 300 and row["month_volume"] == 90000
    assert r.size_decimals() == {"BTC": 5}
    fills = r.user_fills("0xabc", T0, T0 + HOUR)
    assert len(fills) == 1 and fills[0].px == 2500.1 and fills[0].direction == "Open Long"


def test_errors_become_reader_error(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    r = make_reader(lambda request: httpx.Response(503))
    with pytest.raises(ReaderError):
        r.info({"type": "meta"})


def test_interval_code():
    assert interval_code("1時間") == "1h" and interval_code("15m") == "15m"
    with pytest.raises(ValueError):
        interval_code("7分")
