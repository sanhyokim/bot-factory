"""チェーンの読み取りのキャッシュ(data/chain_cache.db)。

確定したブロックの読み取りは何度読んでも同じ答えなので、2回目からは回線を使わない。
大きくなりやすいので工場の帳簿(factory.db)とは別のファイルにする。消しても読み直すだけで困らない。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .. import config
from ..models import now_jst

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rpc_cache (
    key        TEXT PRIMARY KEY,     -- チェーン・呼び方・引数から作る
    chain      TEXT NOT NULL,
    method     TEXT NOT NULL,
    result     TEXT NOT NULL,        -- 答え(JSON)
    created_at TEXT NOT NULL
);
"""


def cache_path() -> Path:
    p = Path(os.environ.get("FACTORY_CHAIN_CACHE")
             or config.load_config().get("onchain", {}).get("cache_path", "data/chain_cache.db"))
    return p if p.is_absolute() else config.ROOT / p


class ChainCache:
    def __init__(self, path: str | Path | None = None):
        p = Path(path) if path else cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(p, timeout=30)
        self.conn.executescript(_SCHEMA)

    @staticmethod
    def key(chain: str, method: str, params: list) -> str:
        raw = json.dumps([chain, method, params], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> tuple[bool, object]:
        row = self.conn.execute("SELECT result FROM rpc_cache WHERE key = ?", (key,)).fetchone()
        return (True, json.loads(row[0])) if row else (False, None)

    def put(self, key: str, chain: str, method: str, result) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO rpc_cache(key, chain, method, result, created_at) VALUES (?,?,?,?,?)",
            (key, chain, method, json.dumps(result, separators=(",", ":")),
             now_jst().isoformat(timespec="seconds")))
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM rpc_cache").fetchone()[0]

    def close(self) -> None:
        self.conn.close()
