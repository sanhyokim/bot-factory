"""設定ファイル(config.yaml)と .env の読み込み。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=None)
def load_config(path: str | None = None) -> dict:
    cfg_path = Path(path or os.environ.get("FACTORY_CONFIG", ROOT / "config.yaml"))
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env() -> None:
    """.env があれば環境変数に読み込む(無くても動く)。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")


def db_path() -> Path:
    p = Path(os.environ.get("FACTORY_DB") or load_config()["db_path"])
    return p if p.is_absolute() else ROOT / p
