"""1日1回の実行(SPEC 3.1: python -m factory run-daily)。

工程は段階3で順に足す。段階1では帳簿の準備だけ行う。
"""
from __future__ import annotations

from . import db


def run_daily() -> None:
    conn = db.connect()
    conn.close()
    print("run-daily: 工程はまだありません(段階3で探索〜反証を接続します)")
