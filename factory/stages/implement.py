"""実装(SPEC 5.4): 土台(bots/_template/)を bots/B-XXXX/ にコピーし、AI(Sonnet)には strategy.py の関数だけを書かせる。

BOTは1本ずつ自己完結: コピーした base.py がそのBOTの土台で、後で _template を直しても既存のBOTには効かない。
注文・記録・停止・損切り・最長保有・上限額・レバレッジ1倍・同時保有1は土台が受け持ち、AIは触れない。
自動テスト(過去30日)に落ちたら修理(repair.py)。修理は通算3回まで。通ったら paper評価を始める。
BOTには土台の版・作ったAI・継続性と競争の札(新ロジック開発が決めたもの)を記録する(SPEC 4.3)。
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .. import ai_gateway, autotest, botrun, config, db, memo
from ..botbase import BotRejected
from ..hyperliquid.reader import INTERVAL_MS, Candle, Funding, ReaderError
from ..market import DAY_MS, Market
from ..models import DISTORTION, now_jst
from . import repair

TEMPLATE_DIR = config.ROOT / "bots" / "_template"
TEMPLATE = TEMPLATE_DIR / "strategy.py"
COPIED = ("base.py", "strategy.py")      # コピーする土台。bot.yaml は売買ルールから書く

SYSTEM = ("あなたは暗号資産BOTの「入る条件」「出る条件」の関数を書く担当。"
          "書くのは strategy.py だけ。中身は定数と entry_signal・exit_signal の2つの関数。"
          "import は math と statistics だけ。class・try・with・global・ファイルの読み書き・外部通信は書けない。"
          "名前が _ で始まる属性は使えない。利益確定・損切り・最長保有・上限額・レバレッジ1倍・同時保有1は"
          "土台が行うので書かない。")


@dataclass
class TestData:
    candles: list[Candle]
    funding: list[Funding]
    size_decimals: int


@dataclass
class ImplementReport:
    started: list[str] = field(default_factory=list)      # paper評価を始めたBOT
    rejected: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)      # データが取れず翌日へ
    stopped: str | None = None


def bot_dir(bot_id: str) -> Path:
    return config.bots_dir() / bot_id


def code_path(path: Path) -> str:
    try:
        return path.relative_to(config.ROOT).as_posix()
    except ValueError:
        return str(path)


def copy_template(path: Path) -> int:
    """土台をBOTのフォルダにコピーし、土台の版を返す。"""
    path.mkdir(parents=True, exist_ok=True)
    for name in COPIED:
        shutil.copyfile(TEMPLATE_DIR / name, path / name)
    return botrun.base_version(path)


def write_yaml(path: Path, rule) -> None:
    data = {"type": rule["type"], "symbol": rule["symbol"], "timeframe": rule["timeframe"],
            "stop_loss_pct": float(rule["stop_loss_pct"]), "max_hold_hours": float(rule["max_hold_hours"]),
            "max_trades_per_day": int(rule["max_trades_per_day"])}
    if rule["type"] == DISTORTION:
        data |= {"funding_x": float(rule["funding_x"]), "funding_y": float(rule["funding_y"])}
    else:
        data["take_profit_pct"] = float(rule["take_profit_pct"])
    data |= {"leverage": 1, "max_positions": 1}
    head = (f"# 売買ルール {rule['id']}(SPEC 4.2)。実装工程が作った\n"
            f"# 入る条件: {rule['entry_condition']}\n# 出る条件: {rule['exit_condition']}\n")
    (path / "bot.yaml").write_text(head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                                   encoding="utf-8")


def load_test_data(market: Market, rule) -> TestData:
    days = config.load_config()["implement"]["test_days"]
    now = market.now()
    symbol = rule["symbol"]
    decimals = market.size_decimals().get(symbol)
    if decimals is None:
        raise ReaderError(f"{symbol} はHyperliquidの無期限先物にない")
    candles = market.candles(symbol, rule["timeframe"], now - days * DAY_MS, now)
    if len(candles) < 2:
        raise ReaderError(f"{symbol} {rule['timeframe']} の価格が取れなかった")
    return TestData(candles, market.funding(symbol, now - days * DAY_MS, now), decimals)


def test(path: Path, data: TestData) -> str | None:
    """静的検査と自動テスト(SPEC 5.4 の2)。落ちた時はその内容、通れば None。"""
    try:
        bot = botrun.load(path)
    except BotRejected as e:
        return f"土台の決まりに反する: {e}"
    except Exception as e:  # 読み込みの時点の異常も修理に回す
        return f"読み込めない: {type(e).__name__}: {e}"
    cfg = config.load_config()
    checks, _ = autotest.run(bot, data.candles, data.funding, amount_usd=cfg["paper"]["amount_usd"],
                             fee_rate=cfg["hyperliquid"]["taker_fee_rate"], size_decimals=data.size_decimals,
                             min_order_usd=cfg["hyperliquid"]["min_order_usd"],
                             slippage_rate=cfg["hyperliquid"]["slippage_pct"] / 100)
    failed = [c for c in checks if not c.ok]
    return "\n".join(f"{c.name}: {c.detail}" for c in failed) or None


def fix_until_pass(conn: sqlite3.Connection, path: Path, rule, data: TestData, failure: str | None, *,
                   attempts: int, bot_id: str, idea_id: str) -> tuple[bool, int, str]:
    """不具合が無くなるまで修理する。戻り値: (直ったか, 修理回数の累計, 直らなかった理由)。"""
    max_repairs = config.load_config()["implement"]["max_repairs"]
    while failure:
        if attempts >= max_repairs:
            return False, attempts, f"修理{max_repairs}回で直らない: {failure.splitlines()[0]}"
        db.set_idea_state(conn, idea_id, "修理", "継続")
        code = (path / "strategy.py").read_text(encoding="utf-8")
        res = repair.repair(conn, rule, code, failure, bot_id=bot_id, idea_id=idea_id)
        attempts += 1
        if res.gave_up:
            return False, attempts, f"直すとルールの意味が変わる: {res.note}"
        if res.code is None:
            failure = res.note
            continue
        (path / "strategy.py").write_text(res.code, encoding="utf-8")
        failure = test(path, data)
    return True, attempts, ""


def _prompt(rule, idea) -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    head = (f"売買ルール {rule['id']}({rule['type']}型)\n銘柄: {rule['symbol']} / 時間の単位: {rule['timeframe']}\n"
            f"入る条件: {rule['entry_condition']}\n出る条件: {rule['exit_condition']}\n")
    if rule["type"] == DISTORTION:
        head += (f"入る資金調達料 x = {rule['funding_x']:g}%/時・出る資金調達料 y = {rule['funding_y']:g}%/時"
                 "(history.funding_rates の率は小数。x・y を100で割って比べる)\n"
                 f"損切り −{rule['stop_loss_pct']}%・最長保有 {rule['max_hold_hours']:g}時間(土台が行う。書かない)\n")
        ask = ("\n次のひな形の関数の中身を書いて。entry_signal は一番新しい資金調達料が +x% 以上なら \"short\"、"
               "−x% 以下なら \"long\"、それ以外は None。exit_signal は資金調達料が ±y% の内側に戻ったら True。\n")
    else:
        head += (f"利益確定 +{rule['take_profit_pct']}%・損切り −{rule['stop_loss_pct']}%・"
                 f"最長保有 {rule['max_hold_hours']:g}時間(土台が行う。書かない)\n")
        ask = ("\n次のひな形の関数の中身を、入る条件どおりに書いて。出る条件のうち利益確定・損切り・最長保有以外"
               "があれば exit_signal に書き、なければ False を返す。\n")
    return (head + f"1日の最大取引回数 {rule['max_trades_per_day']}(土台が守る。書かない)\n"
            + (f"元の案の入る条件の種類: {idea['entry_kind']} 数字: {idea['entry_params']}\n" if idea else "")
            + ask + "直した strategy.py の全体を ```python のかたまり1つで答えて。\n\n"
            f"```python\n{template}```")


def _insert_bot(conn: sqlite3.Connection, bot_id: str, rule, path: Path, attempts: int, status: str, *,
                base_version: int, last_error: str | None = None, paper_start_ms: int | None = None) -> None:
    ts = now_jst().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO bots(id, rule_id, code_path, attempts, status, created_at, updated_at, last_error,
               paper_start_ms, base_version, made_by, continuity, competition)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (bot_id, rule["id"], code_path(path), attempts, status, ts, ts, last_error, paper_start_ms, base_version,
         ai_gateway.model_for_stage("implement"), rule["continuity"], rule["competition"]))
    conn.commit()


def reject(conn: sqlite3.Connection, bot_id: str, rule, path: Path, attempts: int, why: str, *,
           base_version: int) -> None:
    _insert_bot(conn, bot_id, rule, path, min(attempts, 3), "否決", base_version=base_version,
                last_error=f"実装不良: {why}"[:300])
    memo.memo_for_idea(conn, rule["id"], "修理" if attempts else "実装", "実装不良", why[:memo.REASON_MAX])


def build(conn: sqlite3.Connection, market: Market, idea) -> str | None:
    """1つの案を実装する。paper評価を始めたらBOT記号を、否決なら None を返す。"""
    rule = conn.execute("SELECT * FROM rules WHERE id = ?", (idea["id"],)).fetchone()
    data = load_test_data(market, rule)          # 取れなければ ReaderError(翌日へ)
    bot_id = db.next_id(conn, "bot")
    path = bot_dir(bot_id)
    try:
        version = copy_template(path)
        write_yaml(path, rule)
        db.set_idea_state(conn, idea["id"], "実装", "継続")
        r = ai_gateway.ask(conn, "implement", _prompt(rule, idea), system=SYSTEM, bot_id=bot_id,
                           idea_id=idea["id"])
        code = repair.extract_code(r.text)
        if code is None:
            failure = "AIの答えにコードがない"
            (path / "strategy.py").write_text("", encoding="utf-8")
        else:
            (path / "strategy.py").write_text(code, encoding="utf-8")
            failure = test(path, data)
        ok, attempts, why = fix_until_pass(conn, path, rule, data, failure, attempts=0, bot_id=bot_id,
                                           idea_id=idea["id"])
    except ai_gateway.AIError:
        # 予算切れなど。作りかけは消して、次の実行でやり直す
        shutil.rmtree(path, ignore_errors=True)
        db.set_idea_state(conn, idea["id"], "新ロジック開発", "完了")
        raise
    if not ok:
        reject(conn, bot_id, rule, path, attempts, why, base_version=version)
        return None
    step = INTERVAL_MS[rule["timeframe"]]
    now = market.now()
    _insert_bot(conn, bot_id, rule, path, attempts, "paper評価中", base_version=version,
                paper_start_ms=now - now % step + step)
    db.set_idea_state(conn, idea["id"], "paper評価", "継続")
    return bot_id


def send_back_old_rules(conn: sqlite3.Connection) -> list[str]:
    """段階3bより前に作った売買ルール(最長保有がない)は、実装せずに新ロジック開発へ戻して作り直す。
    最長保有は両型で必須で、土台が強制するため(SPEC 9章)。"""
    ids = [r[0] for r in conn.execute(
        """SELECT i.id FROM ideas i JOIN rules r ON r.id = i.id
           WHERE ((i.stage = '新ロジック開発' AND i.verdict = '完了') OR (i.stage IN ('実装', '修理') AND i.verdict = '継続'))
             AND r.max_hold_hours IS NULL AND i.id NOT IN (SELECT rule_id FROM bots)""")]
    for idea_id in ids:
        db.set_idea_state(conn, idea_id, "新ロジック開発", "保留")
    return ids


def run(conn: sqlite3.Connection, market: Market, *, limit: int | None = None) -> ImplementReport:
    limit = limit or config.load_config()["implement"]["max_per_day"]
    send_back_old_rules(conn)
    rows = db.interleave_types(conn.execute(
        """SELECT * FROM ideas WHERE ((stage = '新ロジック開発' AND verdict = '完了')
               OR (stage IN ('実装', '修理') AND verdict = '継続'))
             AND id IN (SELECT id FROM rules) AND id NOT IN (SELECT rule_id FROM bots)
           ORDER BY id""").fetchall(), limit)
    rep = ImplementReport()
    for idea in rows:
        try:
            bot_id = build(conn, market, idea)
        except ReaderError:
            rep.skipped.append(idea["id"])
            continue
        except ai_gateway.AIError as e:
            rep.stopped = str(e)
            break
        (rep.started if bot_id else rep.rejected).append(bot_id or idea["id"])
    return rep
