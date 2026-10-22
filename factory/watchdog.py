"""見張り役(SPEC 3.3): 工場の各部分を別々のプロセスで動かし、止まっていないかを見る。AIは使わない。

- 見張るプロセスは config.yaml の watchdog.processes(初期は 画面 と 価格の受け取り口)
- 1分ごとに各プロセスの生死とメモリ使用量を確認する
- メモリが上限(1プロセスあたり1GB)を超えたら、そのプロセスだけ再起動する
- 落ちていたら再起動する。1時間に3回落ちたら再起動をやめ、画面の「要判断」に出す
- 各プロセスの出力は、ログの1か所(data/logs/factory-YYYYMMDD.log)に集める
- 再起動の記録は否決メモではなく、ログと帳簿(watchdog_events。画面の枠6)に残す
run-daily は毎朝9時にタスクスケジューラが起こす1回きりの実行なので、ここでは見張らない。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from . import config, db, logs
from .models import now_jst

PROCESS = "watchdog"
LABELS = {"dashboard": "画面", "feed": "価格の受け取り口", "paper": "paper評価", "live": "本番運用"}
GIVE_UP = "再起動をやめた"


def rss_mb(pid: int) -> float | None:
    """プロセスのメモリ使用量(MB)。測れない時は None。"""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/status", encoding="ascii") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024
        except (OSError, ValueError, IndexError):
            return None
        return None
    if sys.platform == "win32":
        return _rss_windows(pid)
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip()) / 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _rss_windows(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)   # 情報の読み取り(限定)・メモリの読み取り
    if not handle:
        return None
    try:
        c = Counters()
        c.cb = ctypes.sizeof(Counters)
        if not kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(c), c.cb):
            return None
        return c.WorkingSetSize / (1024 * 1024)
    finally:
        kernel32.CloseHandle(handle)


@dataclass
class Child:
    name: str
    args: list[str]
    proc: subprocess.Popen | None = None
    crashes: deque = field(default_factory=deque)     # 落ちた時刻(秒)
    given_up: bool = False


def record(name: str, event: str, detail: str = "") -> None:
    """帳簿とログの両方に残す。"""
    logs.write(PROCESS, f"{LABELS.get(name, name)}: {event}" + (f"({detail})" if detail else ""), stage=name)
    try:
        conn = db.connect()
        try:
            conn.execute("INSERT INTO watchdog_events(created_at, process, event, detail) VALUES (?,?,?,?)",
                         (now_jst().isoformat(timespec="seconds"), name, event, detail[:300]))
            conn.commit()
        finally:
            conn.close()
    except Exception:      # 帳簿に書けなくても見張りは続ける(ログには残っている)
        pass


def given_up(conn) -> list:
    """再起動をやめたまま(その後に起動していない)のプロセス。画面の「要判断」に出す。"""
    return conn.execute(
        """SELECT w.* FROM watchdog_events w
           WHERE w.event = ? AND w.id = (SELECT MAX(id) FROM watchdog_events v WHERE v.process = w.process)
           ORDER BY w.id""", (GIVE_UP,)).fetchall()


class Watchdog:
    def __init__(self, processes: dict[str, list[str]], *, memory_limit_mb: float, max_crashes_per_hour: int,
                 clock: Callable[[], float] = time.time, rss: Callable[[int], float | None] = rss_mb,
                 python: str = sys.executable):
        self.children = {name: Child(name, list(args)) for name, args in processes.items()}
        self.memory_limit_mb = memory_limit_mb
        self.max_crashes = max_crashes_per_hour
        self._clock, self._rss, self._python = clock, rss, python

    def _spawn(self, child: Child) -> None:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        child.proc = subprocess.Popen([self._python, *child.args], cwd=config.ROOT, env=env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                      encoding="utf-8", errors="replace")
        threading.Thread(target=self._pump, args=(child.name, child.proc), daemon=True).start()

    @staticmethod
    def _pump(name: str, proc: subprocess.Popen) -> None:
        """子プロセスの出力を、ログの1か所へ流す。"""
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                logs.write(name, line, stage=name)

    def start_all(self) -> None:
        for child in self.children.values():
            self._spawn(child)
            record(child.name, "起動", f"pid {child.proc.pid}")

    def _stop(self, child: Child) -> None:
        if child.proc is not None and child.proc.poll() is None:
            child.proc.kill()
            try:
                child.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def check_once(self) -> list[tuple[str, str]]:
        """1回分の確認。起きたこと (プロセス名, 出来事) の一覧を返す。"""
        events = []
        now = self._clock()
        for child in self.children.values():
            if child.given_up or child.proc is None:
                continue
            code = child.proc.poll()
            if code is not None:
                child.crashes.append(now)
                while child.crashes and now - child.crashes[0] >= 3600:
                    child.crashes.popleft()
                if len(child.crashes) >= self.max_crashes:
                    child.given_up = True
                    record(child.name, GIVE_UP, f"1時間に{len(child.crashes)}回落ちた(終了コード {code})")
                    events.append((child.name, GIVE_UP))
                    continue
                self._spawn(child)
                record(child.name, "再起動", f"落ちていた(終了コード {code})。新しい pid {child.proc.pid}")
                events.append((child.name, "再起動"))
                continue
            mem = self._rss(child.proc.pid)
            if mem is not None and mem > self.memory_limit_mb:
                self._stop(child)
                self._spawn(child)
                record(child.name, "メモリ超過で再起動",
                       f"{mem:.0f}MB > 上限{self.memory_limit_mb:g}MB。新しい pid {child.proc.pid}")
                events.append((child.name, "メモリ超過で再起動"))
        return events

    def stop_all(self) -> None:
        for child in self.children.values():
            if child.given_up or child.proc is None:
                continue                 # 再起動をやめたものは、要判断に出したまま残す
            self._stop(child)
            record(child.name, "停止", "見張り役の終了")

    def run_forever(self, check_seconds: float, *, sleep=time.sleep) -> None:
        self.start_all()
        try:
            while True:
                sleep(check_seconds)
                self.check_once()
        finally:
            self.stop_all()


def from_config() -> Watchdog:
    cfg = config.load_config()["watchdog"]
    return Watchdog({k: [str(a) for a in v] for k, v in cfg["processes"].items()},
                    memory_limit_mb=cfg["memory_limit_mb"], max_crashes_per_hour=cfg["max_crashes_per_hour"])
