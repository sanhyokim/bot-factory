"""修理(SPEC 5.7)。テストに落ちたBOT、またはpaper評価で例外を起こしたBOTを、AI(Sonnet)に直させる。

直すのは strategy.py の「入る条件」「出る条件」の関数だけ。土台は触らせない。
軽い不具合(引数の渡し忘れ・型の違いなど)は止めずに直す。売買ルールの意味を変えないと直らない時は
「直せない」と答えさせ、否決(札 実装不良)にする。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .. import ai_gateway

CANNOT = "直せない"
CODE_BLOCK = re.compile(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", re.S)

SYSTEM = ("あなたは暗号資産BOTの strategy.py を直す担当。直してよいのは entry_signal と exit_signal の中身と定数だけ。"
          "import は math と statistics だけ。class・try・with・global・ファイルの読み書き・外部通信は書けない。"
          "売買ルールの意味(銘柄・時間の単位・入る条件・出る条件の数字)は変えない。"
          f"意味を変えないと直らない時は、コードを書かずに「{CANNOT}: 理由」とだけ答える。")


@dataclass
class RepairResult:
    code: str | None
    gave_up: bool
    note: str


def extract_code(text: str) -> str | None:
    blocks = CODE_BLOCK.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    if "def entry_signal" in text:
        return text.strip() + "\n"
    return None


def repair(conn: sqlite3.Connection, rule, code: str, failure: str, *, bot_id: str | None,
           idea_id: str | None) -> RepairResult:
    prompt = (f"売買ルール: 銘柄 {rule['symbol']} / 時間の単位 {rule['timeframe']}\n"
              f"入る条件: {rule['entry_condition']}\n出る条件: {rule['exit_condition']}\n"
              f"(利益確定 +{rule['take_profit_pct']}%・損切り −{rule['stop_loss_pct']}% は土台が行う)\n\n"
              f"今の strategy.py:\n```python\n{code}\n```\n\n"
              f"テストまたは試運転で次の不具合が出た:\n{failure}\n\n"
              "直した strategy.py の全体を ```python のかたまり1つで答えて。")
    r = ai_gateway.ask(conn, "repair", prompt, system=SYSTEM, bot_id=bot_id, idea_id=idea_id)
    text = r.text.strip()
    fixed = extract_code(text)
    if fixed is None and CANNOT in text[:80]:
        return RepairResult(None, True, text.split(CANNOT, 1)[1].lstrip(":: ").strip()[:60] or CANNOT)
    if fixed is None:
        return RepairResult(None, False, "AIの答えにコードがない")
    return RepairResult(fixed, False, "")
