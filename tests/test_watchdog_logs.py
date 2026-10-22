"""見張り役(SPEC 3.3)とログの一元化。本物の子プロセスを起こして、落ちたら再起動することを確かめる。"""
import os
import sys
import time

from fastapi.testclient import TestClient

from factory import logs, watchdog
from factory.dashboard.app import app
from factory.models import now_jst
from factory.watchdog import Watchdog

SLEEPER = ["-c", "import time; print('動いている', flush=True); time.sleep(60)"]
CRASHER = ["-c", "import sys; sys.exit(3)"]


def events(conn):
    return [(r["process"], r["event"]) for r in conn.execute("SELECT * FROM watchdog_events ORDER BY id")]


def wait_until(cond, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def test_dead_process_is_restarted(conn):
    wd = Watchdog({"sleeper": SLEEPER}, memory_limit_mb=1024, max_crashes_per_hour=3)
    wd.start_all()
    try:
        first = wd.children["sleeper"].proc
        first.kill()
        first.wait(10)
        assert wd.check_once() == [("sleeper", "再起動")]
        second = wd.children["sleeper"].proc
        assert second.pid != first.pid and second.poll() is None
        assert wd.check_once() == []                       # 動いていれば何もしない
        assert events(conn) == [("sleeper", "起動"), ("sleeper", "再起動")]
        # 子プロセスの出力はログの1か所に集まる
        today = now_jst().date().isoformat()
        assert wait_until(lambda: any(r["text"] == "動いている" for r in logs.read(today, process="sleeper")))
    finally:
        wd.stop_all()
    assert wd.children["sleeper"].proc.poll() is not None


def test_process_over_the_memory_limit_is_restarted(conn):
    wd = Watchdog({"sleeper": SLEEPER}, memory_limit_mb=1024, max_crashes_per_hour=3, rss=lambda pid: 2048.0)
    wd.start_all()
    try:
        first = wd.children["sleeper"].proc
        assert wd.check_once() == [("sleeper", "メモリ超過で再起動")]
        assert first.poll() is not None and wd.children["sleeper"].proc.poll() is None
        detail = conn.execute("SELECT detail FROM watchdog_events WHERE event = 'メモリ超過で再起動'").fetchone()[0]
        assert "2048MB > 上限1024MB" in detail
    finally:
        wd.stop_all()


def test_gives_up_after_three_crashes_in_an_hour_and_shows_it(conn):
    now = [1000.0]
    wd = Watchdog({"feed": CRASHER}, memory_limit_mb=1024, max_crashes_per_hour=3, clock=lambda: now[0])
    wd.start_all()
    got = []
    for _ in range(3):
        wd.children["feed"].proc.wait(10)
        got += wd.check_once()
        now[0] += 600
    assert got == [("feed", "再起動"), ("feed", "再起動"), ("feed", "再起動をやめた")]
    assert wd.children["feed"].given_up and wd.check_once() == []
    assert [r["process"] for r in watchdog.given_up(conn)] == ["feed"]
    html = TestClient(app).get("/").text
    assert "見張り役が再起動をやめたプロセス" in html and "価格の受け取り口" in html and "要判断が 1 件" in html
    # 見張り役を起動し直せば(そのプロセスの「起動」が記録されれば)、要判断から消える
    watchdog.record("feed", "起動")
    assert watchdog.given_up(conn) == []


def test_crashes_older_than_an_hour_do_not_count(conn):
    now = [0.0]
    wd = Watchdog({"x": CRASHER}, memory_limit_mb=1024, max_crashes_per_hour=3, clock=lambda: now[0])
    wd.start_all()
    try:
        for _ in range(5):
            wd.children["x"].proc.wait(10)
            assert wd.check_once() == [("x", "再起動")]
            now[0] += 1900                   # 落ちる間隔が約32分なら、1時間に3回にはならない
    finally:
        wd.stop_all()


def test_memory_of_a_real_process_can_be_measured():
    mb = watchdog.rss_mb(os.getpid())
    assert mb is not None and 1 < mb < 4096, (sys.platform, mb)


def test_central_log_writes_and_filters(conn):
    at = now_jst()
    logs.write("run-daily", "[探索] 順位表の候補 3財布", at=at)
    logs.write("run-daily", "run-daily 開始", at=at)
    logs.write("feed", "1行目\n2行目", stage="feed", at=at)
    day = at.date().isoformat()
    assert logs.days()[0] == day and logs.path_for(at.date()).name == f"factory-{at:%Y%m%d}.log"
    rows = logs.read(day)
    assert [r["stage"] for r in rows] == ["feed", "feed", "run-daily", "探索"]
    assert [r["text"] for r in logs.read(day, stage="探索")] == ["[探索] 順位表の候補 3財布"]
    assert logs.stages(day) == ["feed", "run-daily", "探索"]
    page = TestClient(app).get("/logs", params={"day": day, "stage": "探索"}).text
    assert "順位表の候補 3財布" in page and "run-daily 開始" not in page
