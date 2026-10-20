"""1日1回の実行(SPEC 3.1: python -m factory run-daily)。

順番:
  0. 週1回の棚卸し(今週まだなら)
  1. 前の実行で送った一括処理(AI)の答えを受け取る
  2. paper評価中のBOTを今日の価格まで進め、合格したら反証
  3. 探索: 順位表 → 財布の絞り込み → Haikuに一括で送り、答えを待って案登録
  4. 調査 → 5. 新ロジック開発 → 6. 実装(と修理)
工程ごとに失敗しても、残りの工程は続ける。結果は data/logs/ にも残す。
"""
from __future__ import annotations

import traceback
from collections.abc import Callable
from pathlib import Path

from . import ai_gateway, config, db, tuning
from .hyperliquid.reader import Reader, ReaderError
from .market import Market
from .models import now_jst
from .stages import design, explore, implement, investigate, paper


def _ids(xs: list[str], n: int = 8) -> str:
    return "、".join(xs[:n]) + (f" ほか{len(xs) - n}件" if len(xs) > n else "") if xs else "なし"


def run_daily(*, reader: Reader | None = None, wait_minutes: float | None = None,
              skip_explore: bool = False, out: Callable[[str], None] = print) -> list[str]:
    conn = db.connect()
    market = Market(conn, reader or Reader())
    cfg = config.load_config()
    wait = cfg["explore"]["batch_wait_minutes"] if wait_minutes is None else wait_minutes
    lines: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        lines.append(text)
        out(text)

    def step(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except (ReaderError, ai_gateway.AIError) as e:
            say(f"[{name}] 止まった: {e}")
        except Exception as e:  # 予想外の異常も、他の工程は続ける。詳細はログへ
            say(f"[{name}] 予想外のエラー: {type(e).__name__}: {e}")
            errors.append(traceback.format_exc())

    say(f"run-daily 開始 {now_jst().strftime('%Y-%m-%d %H:%M')}")

    def stocktake():
        note = tuning.weekly(conn)
        if note:
            say(f"[棚卸し] {note}")

    def leftovers():
        old = explore.reject_registered_other(conn)
        if old:
            say(f"[探索] 以前に登録された「その他」の案 {len(old)}件を否決した(入る条件の種類を分類できず)")
        got = explore.collect(conn)
        if got.wallets:
            say(f"[探索の受け取り] 前回分 {got.wallets}財布 → 案登録 {len(got.registered)}・照合で除外 {len(got.excluded)}")
        n = investigate.collect_reasons(conn)
        if n:
            say(f"[調査の文章化] 否決メモ {n}件の理由をAIの文に置き換えた")

    def paper_step():
        r = paper.run_bots(conn, market)
        say(f"[paper評価] 継続 {len(r.running)}・不合格 {len(r.failed)}・保留 {len(r.held)}・"
            f"修理 {len(r.repaired)}・直らず否決 {len(r.broken)}・開始待ち {len(r.waiting)}")
        if r.refute_passed or r.refute_failed:
            say(f"[反証] 合格(関所1へ) {_ids(r.refute_passed)}・不合格 {_ids(r.refute_failed)}")

    def explore_step():
        s = explore.scan(conn, market, progress=lambda i, n: say(f"[探索] 財布を調べている {i}/{n}"))
        reasons = "、".join(f"{k} {v}" for k, v in sorted(s.reasons.items(), key=lambda kv: -kv[1])[:4])
        say(f"[探索] 順位表の候補 {s.listed}財布・7日以内に分析済み {s.skipped_recent}・今回調べた {s.checked}・"
            f"絞り込み通過 {len(s.passed)}" + (f"(落ちた理由: {reasons})" if reasons else "")
            + (f"・取得失敗 {s.errors}" if s.errors else ""))
        batch_id, n = explore.submit(conn, s.passed)
        if not batch_id:
            return
        say(f"[探索] {n}財布をHaikuに一括で送った。答えを最大{wait:g}分待つ")
        got = explore.collect(conn, wait_seconds=wait * 60)
        if not got.wallets:
            say("[探索] 答えがまだ。次の実行で受け取る")
            return
        say(f"[探索] 案登録 {len(got.registered)}・照合で除外(重複) {len(got.excluded)}"
            + (f"・分類できず {len(got.unclassified)}" if got.unclassified else "")
            + (f"・読めない仮説 {got.unreadable}" if got.unreadable else "")
            + (f"・失敗 {len(got.errors)}" if got.errors else ""))

    def investigate_step():
        r = investigate.run(conn, market)
        rej = "、".join(f"{k} {v}" for k, v in r.rejected.items()) or "なし"
        say(f"[調査] 継続 {len(r.passed)}・見送り({rej})・保留 {len(r.held)}")

    def design_step():
        r = design.run(conn)
        say(f"[新ロジック開発] 完了 {len(r.done)}・ルール不成立 {len(r.rejected)}"
            + (f"・中断: {r.stopped}" if r.stopped else ""))

    def implement_step():
        r = implement.run(conn, market)
        say(f"[実装] paper評価を開始 {_ids(r.started)}・実装不良 {len(r.rejected)}"
            + (f"・データ待ち {len(r.skipped)}" if r.skipped else "")
            + (f"・中断: {r.stopped}" if r.stopped else ""))

    try:
        step("棚卸し", stocktake)
        step("受け取り", leftovers)
        step("paper評価", paper_step)
        if skip_explore:
            say("[探索] 今回は行わない(--skip-explore)")
        else:
            step("探索", explore_step)
        step("調査", investigate_step)
        step("新ロジック開発", design_step)
        step("実装", implement_step)
        say(f"run-daily 終了 {now_jst().strftime('%H:%M')}・今日のAI代 ${ai_gateway.today_cost(conn):.4f}")
    finally:
        _write_log(lines, errors)
        conn.close()
    return lines


def _write_log(lines: list[str], errors: list[str]) -> Path | None:
    try:
        logs = config.db_path().parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / f"run-daily-{now_jst().strftime('%Y%m%d')}.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines + errors) + "\n\n")
        return path
    except OSError:
        return None
