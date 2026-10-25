"""AI窓口: 工場の中でAIに質問を送る唯一の口(SPEC 3.1・7章)。

- 接続先(api / claude_code / mock)とモデルは config.yaml で切り替える
- 呼ぶたびに AI費用帳簿(ai_costs)に1行つける。答えが返らなかった・読めなかった呼び出しは ai_failures に1行
- 1日の費用が上限を超えていたら呼ばない
- 送信文に .env の鍵が含まれていたら送らない(SPEC 9章)
- 答えの形を決めたい時は schema(JSON Schema)を渡す。答えは AIResult.data に入る
- 急がない処理は一括処理(半額。SPEC 7.2)で送る: submit_batch で送り、collect_batches で受け取る
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import config
from .models import MODELS, now_jst

SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PRIVATE")
CUSTOM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# テスト・試し用: mock の答えを差し替える関数 (工程, 送信文, schema) -> 答えの文
mock_responder: Callable[[str, str, dict | None], str] | None = None


class AIError(RuntimeError):
    """AI窓口で止まった理由。画面やコマンドにはこの文だけを出す。"""


class BudgetExceeded(AIError):
    """今日のAI代が上限に達している。"""


class SecretLeak(AIError):
    """送信文に鍵が含まれている。"""


@dataclass
class AIResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    data: dict | None = None    # schema を渡した時の答え


@dataclass
class BatchItem:
    key: str                    # 一括処理の中の名札(英数字と - _ だけ・64字以内)
    prompt: str
    meta: dict = field(default_factory=dict)   # 受け取った時に使う付帯情報
    system: str | None = None
    idea_id: str | None = None
    bot_id: str | None = None


@dataclass
class BatchOutcome:
    key: str
    meta: dict
    result: AIResult | None
    error: str | None = None


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


def check_budget(conn: sqlite3.Connection) -> None:
    limit = config.load_config()["ai"]["daily_cost_limit_usd"]
    spent = today_cost(conn)
    if spent >= limit:
        raise BudgetExceeded(f"今日のAI代 {spent:.4f}ドルが上限 {limit}ドルに達している")


def _backend() -> str:
    backend = os.environ.get("FACTORY_AI_BACKEND") or config.load_config()["ai"]["backend"]
    if backend not in ("api", "claude_code", "mock"):
        raise ValueError(f"接続先が不正: {backend}")
    return backend


def _model(stage: str) -> str:
    model = model_for_stage(stage)
    if model not in MODELS:
        raise ValueError(f"モデル名が不正: {model}")
    return model


# --- 接続先ごとの呼び出し ---

@contextmanager
def _api_errors():
    import anthropic

    try:
        yield
    except anthropic.AuthenticationError as e:
        raise AIError("APIキーが正しくない。.env のキーを確認する(前後の空白や欠けがないか)") from e
    except anthropic.PermissionDeniedError as e:
        raise AIError("APIキーに使用権限がない。API管理画面でクレジット残高と上限を確認する") from e
    except anthropic.RateLimitError as e:
        raise AIError("AIの利用回数か上限額に達した。少し待つか、API管理画面の上限を確認する") from e
    except anthropic.APIConnectionError as e:
        raise AIError("AIに接続できない。インターネット接続を確認する") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"AIの呼び出しに失敗した(状態コード {e.status_code}: {e.message})") from e


def _client():
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise AIError("APIキーが設定されていない。.env の ANTHROPIC_API_KEY= の右にキーを書いて保存する")
    return anthropic.Anthropic()


def _params(model_id: str, prompt: str, system: str | None, schema: dict | None,
            max_tokens: int | None) -> dict:
    ai = config.load_config()["ai"]
    output_config: dict = {"effort": ai["effort"]}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    params: dict = {"model": model_id, "max_tokens": max_tokens or ai["max_tokens"],
                    "output_config": output_config, "messages": [{"role": "user", "content": prompt}]}
    if system:
        params["system"] = system
    return params


def _message_text(msg) -> str:
    if msg.stop_reason == "refusal":
        raise AIError("AIが回答を断った(refusal)")
    return "".join(b.text for b in msg.content if b.type == "text")


def _call_api(model_id: str, prompt: str, system: str | None, schema: dict | None = None,
              max_tokens: int | None = None) -> tuple[str, int, int]:
    client = _client()
    with _api_errors():
        resp = client.messages.create(**_params(model_id, prompt, system, schema, max_tokens))
    return _message_text(resp), resp.usage.input_tokens, resp.usage.output_tokens


def _call_claude_code(model: str, prompt: str, system: str | None,
                      schema: dict | None = None) -> tuple[str, int, int]:
    """Claude Code(Max契約)の非対話モードで呼ぶ。費用は帳簿上トークン数から計算する。"""
    exe = shutil.which("claude")
    if exe is None:
        raise AIError("claude コマンドが見つからない")
    if schema:
        prompt += ("\n\n答えは次のJSON Schemaに合うJSONだけを書く(説明や```は付けない):\n"
                   + json.dumps(schema, ensure_ascii=False))
    cmd = [exe, "-p", "--model", model, "--output-format", "json"]
    if system:
        cmd += ["--append-system-prompt", system]
    out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600, check=True)
    data = json.loads(out.stdout)
    usage = data.get("usage", {})
    return data.get("result", ""), int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


def _sample(schema: dict):
    """schema に合う最小の値(mock の既定の答え)。"""
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object":
        return {k: _sample(v) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return [_sample(schema.get("items", {}))]
    return {"string": "mock", "integer": 1, "number": 1.0, "boolean": False}.get(t)


def _call_mock(stage: str, prompt: str, schema: dict | None) -> tuple[str, int, int]:
    if mock_responder is not None:
        text = mock_responder(stage, prompt, schema)
    elif schema:
        text = json.dumps(_sample(schema), ensure_ascii=False)
    else:
        text = f"[mock] {prompt[:40]}"
    # 1トークン ≒ 4文字 として数える(テスト・試し用)
    return text, max(1, len(prompt) // 4), max(1, len(text) // 4)


def _dispatch(backend: str, stage: str, model: str, prompt: str, system: str | None,
              schema: dict | None, max_tokens: int | None) -> tuple[str, int, int]:
    if backend == "api":
        return _call_api(config.load_config()["ai"]["models"][model], prompt, system, schema, max_tokens)
    if backend == "claude_code":
        return _call_claude_code(model, prompt, system, schema)
    return _call_mock(stage, prompt, schema)


def parse_json(text: str) -> dict:
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("{"):] if "{" in s else s
    start, end = s.find("{"), s.rfind("}")
    try:
        data = json.loads(s[start:end + 1] if start >= 0 else s)
    except json.JSONDecodeError as e:
        raise AIError(f"AIの答えがJSONになっていない: {text[:60]}") from e
    if not isinstance(data, dict):
        raise AIError("AIの答えがJSONの{ }になっていない")
    return data


def _log_cost(conn: sqlite3.Connection, stage: str, model: str, backend: str, tin: int, tout: int, *,
              batch: bool, idea_id: str | None, bot_id: str | None) -> float:
    if backend == "mock":
        cost = 0.0
    else:
        cost = price(model, tin, tout, batch=batch and backend == "api")
    conn.execute(
        """INSERT INTO ai_costs(created_at, stage, idea_id, bot_id, model, backend,
               input_tokens, output_tokens, batch, cost_usd)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (now_jst().isoformat(timespec="seconds"), stage, idea_id, bot_id, model, backend, tin, tout,
         int(batch), cost),
    )
    conn.commit()
    return cost


def _log_failure(conn: sqlite3.Connection, stage: str, model: str, backend: str, reason: str, *,
                 idea_id: str | None = None, bot_id: str | None = None) -> None:
    """AIが答えを返せなかった呼び出しを残す。予算切れ・鍵の検査で送らなかったものは数えない。"""
    conn.execute(
        """INSERT INTO ai_failures(created_at, stage, model, backend, idea_id, bot_id, reason)
           VALUES (?,?,?,?,?,?,?)""",
        (now_jst().isoformat(timespec="seconds"), stage, model, backend, idea_id, bot_id, reason[:200]))
    conn.commit()


# --- 1問ずつ送る ---

def ask(conn: sqlite3.Connection, stage: str, prompt: str, *, system: str | None = None,
        idea_id: str | None = None, bot_id: str | None = None, schema: dict | None = None,
        max_tokens: int | None = None) -> AIResult:
    """工程 stage の割り当てモデルに1問送り、費用帳簿に1行つける。"""
    model = _model(stage)
    check_budget(conn)
    check_secrets(prompt + (system or ""))
    backend = _backend()
    try:
        text, tin, tout = _dispatch(backend, stage, model, prompt, system, schema, max_tokens)
    except (AIError, subprocess.SubprocessError, OSError, ValueError) as e:
        _log_failure(conn, stage, model, backend, str(e) or type(e).__name__, idea_id=idea_id, bot_id=bot_id)
        raise
    cost = _log_cost(conn, stage, model, backend, tin, tout, batch=False, idea_id=idea_id, bot_id=bot_id)
    try:
        data = parse_json(text) if schema else None
    except AIError as e:
        _log_failure(conn, stage, model, backend, str(e), idea_id=idea_id, bot_id=bot_id)
        raise
    return AIResult(text=text, model=model, input_tokens=tin, output_tokens=tout, cost_usd=cost, data=data)


# --- 一括処理(半額) ---

def submit_batch(conn: sqlite3.Connection, stage: str, purpose: str, items: list[BatchItem], *,
                 schema: dict | None = None, max_tokens: int | None = None,
                 note: dict | None = None) -> str | None:
    """まとめて送る。api では受付だけして戻る(答えは collect_batches で受け取る)。

    api 以外の接続先には一括処理がないので、その場で1件ずつ答えを得て保管し、受け取りは同じ道を通す。
    """
    if not items:
        return None
    model = _model(stage)
    check_budget(conn)
    keys = set()
    for it in items:
        if not CUSTOM_ID.match(it.key) or it.key in keys:
            raise ValueError(f"一括処理の名札が不正か重複: {it.key}")
        keys.add(it.key)
        check_secrets(it.prompt + (it.system or ""))
    backend = _backend()
    results = None
    if backend == "api":
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        model_id = config.load_config()["ai"]["models"][model]
        client = _client()
        requests = [Request(custom_id=it.key, params=MessageCreateParamsNonStreaming(
            **_params(model_id, it.prompt, it.system, schema, max_tokens))) for it in items]
        try:
            with _api_errors():
                batch_id = client.messages.batches.create(requests=requests).id
        except AIError as e:
            _log_failure(conn, stage, model, backend, f"一括処理を送れなかった({len(items)}件): {e}")
            raise
    else:
        batch_id = f"local-{now_jst().strftime('%Y%m%d%H%M%S')}-{purpose}-{os.urandom(3).hex()}"
        results = {}
        for it in items:
            try:
                text, tin, tout = _dispatch(backend, stage, model, it.prompt, it.system, schema, max_tokens)
                results[it.key] = {"text": text, "in": tin, "out": tout}
            except (AIError, subprocess.SubprocessError, OSError, ValueError) as e:
                results[it.key] = {"error": str(e)}
    info = {"schema": bool(schema), "note": note or {},
            "items": {it.key: {"meta": it.meta, "idea_id": it.idea_id, "bot_id": it.bot_id} for it in items}}
    conn.execute(
        """INSERT INTO ai_batches(id, stage, purpose, model, backend, created_at, status, items_json, results_json)
           VALUES (?,?,?,?,?,?,'処理中',?,?)""",
        (batch_id, stage, purpose, model, backend, now_jst().isoformat(timespec="seconds"),
         json.dumps(info, ensure_ascii=False), json.dumps(results, ensure_ascii=False) if results is not None else None),
    )
    conn.commit()
    return batch_id


def pending_batches(conn: sqlite3.Connection, purpose: str | None = None) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM ai_batches WHERE status = '処理中'", []
    if purpose:
        sql += " AND purpose = ?"
        args.append(purpose)
    return conn.execute(sql + " ORDER BY created_at", args).fetchall()


def _outcome(conn, row, key: str, item: dict, schema: bool, text: str | None, tin: int, tout: int,
             error: str | None) -> BatchOutcome:
    meta = item.get("meta", {})
    if error is not None:
        return BatchOutcome(key, meta, None, error)
    cost = _log_cost(conn, row["stage"], row["model"], row["backend"], tin, tout, batch=True,
                     idea_id=item.get("idea_id"), bot_id=item.get("bot_id"))
    result = AIResult(text=text or "", model=row["model"], input_tokens=tin, output_tokens=tout, cost_usd=cost)
    if schema:
        try:
            result.data = parse_json(result.text)
        except AIError as e:
            return BatchOutcome(key, meta, result, str(e))
    return BatchOutcome(key, meta, result)


def _collect_one(conn: sqlite3.Connection, row: sqlite3.Row) -> list[BatchOutcome] | None:
    """終わっていれば答えを返して受取済みにする。まだなら None。"""
    info = json.loads(row["items_json"])
    items, schema = info["items"], info["schema"]
    outcomes: list[BatchOutcome] = []
    if row["backend"] == "api":
        client = _client()
        with _api_errors():
            if client.messages.batches.retrieve(row["id"]).processing_status != "ended":
                return None
            responses = list(client.messages.batches.results(row["id"]))
        for r in responses:
            item = items.get(r.custom_id, {})
            if r.result.type == "succeeded":
                msg = r.result.message
                try:
                    text, error = _message_text(msg), None
                except AIError as e:
                    text, error = None, str(e)
                outcomes.append(_outcome(conn, row, r.custom_id, item, schema, text,
                                         msg.usage.input_tokens, msg.usage.output_tokens, error))
            else:
                outcomes.append(BatchOutcome(r.custom_id, item.get("meta", {}), None,
                                             f"一括処理の1件が失敗した({r.result.type})"))
    else:
        for key, res in json.loads(row["results_json"] or "{}").items():
            outcomes.append(_outcome(conn, row, key, items.get(key, {}), schema, res.get("text"),
                                     res.get("in", 0), res.get("out", 0), res.get("error")))
    for o in outcomes:
        if o.error:
            item = items.get(o.key, {})
            _log_failure(conn, row["stage"], row["model"], row["backend"], o.error,
                         idea_id=item.get("idea_id"), bot_id=item.get("bot_id"))
    conn.execute("UPDATE ai_batches SET status = '受取済み' WHERE id = ?", (row["id"],))
    conn.commit()
    return outcomes


def collect_batches(conn: sqlite3.Connection, purpose: str, *, wait_seconds: float = 0,
                    poll_seconds: float = 30) -> list[BatchOutcome]:
    """終わった一括処理の答えを受け取る。wait_seconds までは終わるのを待つ。"""
    deadline = time.monotonic() + wait_seconds
    pending = list(pending_batches(conn, purpose))
    outcomes: list[BatchOutcome] = []
    while pending:
        still = []
        for row in pending:
            got = _collect_one(conn, row)
            if got is None:
                still.append(row)
            else:
                outcomes.extend(got)
        pending = still
        left = deadline - time.monotonic()
        if not pending or left <= 0:
            break
        time.sleep(min(poll_seconds, left))
    return outcomes
