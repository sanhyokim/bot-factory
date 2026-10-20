"""AI窓口: 工場の中でAIに質問を送る唯一の口(SPEC 3.1・7章)。

- 接続先(api / claude_code / mock)とモデルは config.yaml で切り替える
- 呼ぶたびに AI費用帳簿(ai_costs)に1行つける
- 1日の費用が上限を超えていたら呼ばない
- 送信文に .env の鍵が含まれていたら送らない(SPEC 9章)
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass

from . import config
from .models import MODELS, now_jst

SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PRIVATE")


class BudgetExceeded(RuntimeError):
    """今日のAI代が上限に達している。"""


class SecretLeak(RuntimeError):
    """送信文に鍵が含まれている。"""


@dataclass
class AIResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


def model_for_stage(stage: str) -> str:
    return config.load_config()["ai"]["stages"][stage]


def price(model: str, input_tokens: int, output_tokens: int, *, batch: bool = False) -> float:
    ai = config.load_config()["ai"]
    p = ai["prices"][model]
    cost = (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000
    return cost * (ai["batch_discount"] if batch else 1.0)


def today_cost(conn: sqlite3.Connection) -> float:
    today = now_jst().date().isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM ai_costs WHERE substr(created_at, 1, 10) = ?",
        (today,)).fetchone()
    return float(row[0])


def check_secrets(text: str) -> None:
    for name, value in os.environ.items():
        if value and len(value) >= 12 and any(m in name.upper() for m in SECRET_MARKERS):
            if value in text:
                raise SecretLeak(f"送信文に {name} の値が含まれているため送信を止めた")


def _call_api(model_id: str, prompt: str, system: str | None) -> tuple[str, int, int]:
    import anthropic

    ai = config.load_config()["ai"]
    client = anthropic.Anthropic()
    kwargs = {}
    if system:
        kwargs["system"] = system
    resp = client.messages.create(
        model=model_id,
        max_tokens=ai["max_tokens"],
        output_config={"effort": ai["effort"]},
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("AIが回答を断った(refusal)")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens


def _call_claude_code(model: str, prompt: str, system: str | None) -> tuple[str, int, int]:
    """Claude Code(Max契約)の非対話モードで呼ぶ。費用は帳簿上トークン数から計算する。"""
    exe = shutil.which("claude")
    if exe is None:
        raise RuntimeError("claude コマンドが見つからない")
    cmd = [exe, "-p", "--model", model, "--output-format", "json"]
    if system:
        cmd += ["--append-system-prompt", system]
    out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600, check=True)
    data = json.loads(out.stdout)
    usage = data.get("usage", {})
    return data.get("result", ""), int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


def _call_mock(prompt: str) -> tuple[str, int, int]:
    # 1トークン ≒ 4文字 として数える(テスト・試し用)
    return f"[mock] {prompt[:40]}", max(1, len(prompt) // 4), 10


def ask(conn: sqlite3.Connection, stage: str, prompt: str, *, system: str | None = None,
        idea_id: str | None = None, bot_id: str | None = None) -> AIResult:
    """工程 stage の割り当てモデルに1問送り、費用帳簿に1行つける。"""
    ai = config.load_config()["ai"]
    model = model_for_stage(stage)
    if model not in MODELS:
        raise ValueError(f"モデル名が不正: {model}")
    spent = today_cost(conn)
    if spent >= ai["daily_cost_limit_usd"]:
        raise BudgetExceeded(f"今日のAI代 {spent:.4f}ドルが上限 {ai['daily_cost_limit_usd']}ドルに達している")
    check_secrets(prompt + (system or ""))

    backend = os.environ.get("FACTORY_AI_BACKEND") or ai["backend"]
    if backend == "api":
        text, tin, tout = _call_api(ai["models"][model], prompt, system)
    elif backend == "claude_code":
        text, tin, tout = _call_claude_code(model, prompt, system)
    elif backend == "mock":
        text, tin, tout = _call_mock(prompt)
    else:
        raise ValueError(f"接続先が不正: {backend}")

    cost = 0.0 if backend == "mock" else price(model, tin, tout)
    conn.execute(
        """INSERT INTO ai_costs(created_at, stage, idea_id, bot_id, model, backend,
               input_tokens, output_tokens, batch, cost_usd)
           VALUES (?,?,?,?,?,?,?,?,0,?)""",
        (now_jst().isoformat(timespec="seconds"), stage, idea_id, bot_id, model, backend, tin, tout, cost),
    )
    conn.commit()
    return AIResult(text=text, model=model, input_tokens=tin, output_tokens=tout, cost_usd=cost)
