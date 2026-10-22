"""ログの一元化(SPEC 3.3): 全プロセスの記録を、data/logs/factory-YYYYMMDD.log の1か所に日付ごとに集める。

1行の形: 「2026-10-23 09:00:01 [run-daily/探索] 本文」。[プロセス/工程]。画面(/logs)から日付と工程で引ける。
本文が「[探索] …」のように工程の名前で始まる時は、それを工程にする。複数行の本文は行ごとに同じ頭を付ける。
書けなくても(ディスクがいっぱい等)工場は止めない。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from . import config
from .models import now_jst

PREFIX = "factory-"
LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([^/\]]*)/([^\]]*)\] ?(.*)$")
STAGE_HEAD = re.compile(r"^\[([^\]]{1,20})\]")


def log_dir() -> Path:
    return config.db_path().parent / "logs"


def path_for(day: date) -> Path:
    return log_dir() / f"{PREFIX}{day.strftime('%Y%m%d')}.log"


def stage_of(message: str) -> str | None:
    m = STAGE_HEAD.match(message)
    return m.group(1) if m else None


def write(process: str, message: str, *, stage: str | None = None, at: datetime | None = None) -> Path | None:
    at = at or now_jst()
    lines = message.splitlines() or [""]
    stage = stage or stage_of(lines[0]) or process
    head = f"{at.strftime('%Y-%m-%d %H:%M:%S')} [{process}/{stage}] "
    try:
        path = path_for(at.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(head + line + "\n" for line in lines))
        return path
    except OSError:
        return None


def days() -> list[str]:
    """ログのある日付(新しい順、YYYY-MM-DD)。"""
    out = []
    for p in log_dir().glob(f"{PREFIX}*.log"):
        s = p.stem[len(PREFIX):]
        if len(s) == 8 and s.isdigit():
            out.append(f"{s[:4]}-{s[4:6]}-{s[6:]}")
    return sorted(out, reverse=True)


def read(day: str, *, stage: str | None = None, process: str | None = None, limit: int = 2000) -> list[dict]:
    """その日のログの行(新しい順)。day は YYYY-MM-DD。工程・プロセスで絞れる。"""
    try:
        path = path_for(date.fromisoformat(day))
        text = path.read_text(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        return []
    rows = []
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        row = {"at": m.group(1), "process": m.group(2), "stage": m.group(3), "text": m.group(4)}
        if (stage and row["stage"] != stage) or (process and row["process"] != process):
            continue
        rows.append(row)
    return rows[::-1][:limit]


def stages(day: str) -> list[str]:
    return sorted({r["stage"] for r in read(day, limit=100_000)})
