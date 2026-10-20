"""案・BOT・記録の型と、仕様で固定された語の一覧(SPEC 4章・6章)。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def now_jst() -> datetime:
    return datetime.now(JST)


# 工程(SPEC 4.1)。否決メモの工程には「関所」も使える(SPEC 6.1)
STAGES = ["探索", "調査", "新ロジック開発", "実装", "paper評価", "反証", "修理", "本番", "否決"]
MEMO_STAGES = ["探索", "調査", "新ロジック開発", "実装", "paper評価", "反証", "修理", "本番", "関所"]

# 判定(SPEC 4.1・5章)
VERDICTS = ["案登録", "待機", "継続", "保留", "見送り", "完了", "仮説を支持", "修理", "未対応"]

# 出どころ(SPEC 4.1)
SOURCES = ["wallet", "market"]

# 札(SPEC 6.2)。固定。増やす時はボスの承認
TAGS = [
    "重複", "手数料負け", "機会不足", "資金不足", "ルール不成立", "実装不良", "連敗", "落ち込み",
    "機会なし", "利益不足", "損益比不足", "前後半不一致", "手数料2倍で赤字", "一発依存", "本番乖離",
    "ボス判断", "未対応",
]
# 本番昇格の記録だけに使う札(SPEC 6.3 用途4)
PASS_TAG = "合格"

# BOTの状態(SPEC 4.3)
BOT_STATES = ["paper評価中", "反証中", "本番待ち", "本番運用中", "停止中", "否決"]
# 関所で止まる状態(SPEC 5.8・5.9)
GATE_STATES = {"本番待ち": "関所1: 本番昇格", "停止中": "関所2: 再開"}

MODELS = ["haiku", "sonnet", "opus"]


@dataclass
class Idea:
    summary: str
    hypothesis: str = ""
    source: str = "wallet"
    wallet: str | None = None
    # 登録前照合のキー(SPEC 6.3 用途1: 銘柄+時間の単位+入る条件の種類)
    symbol: str = ""
    timeframe: str = ""
    entry_kind: str = ""
    stage: str = "探索"
    verdict: str = "案登録"


@dataclass
class Memo:
    stage: str
    tag: str
    reason: str
    idea_summary: str
    idea_id: str | None = None
    symbol: str = ""
    timeframe: str = ""
    entry_kind: str = ""
    source: str = ""
