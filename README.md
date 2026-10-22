# bot-factory

儲かる可能性が高いBOTを、人手をかけずに探し続ける工場。仕様の正本は [docs/SPEC.md](docs/SPEC.md)、現状の要約は [docs/snapshot.md](docs/snapshot.md)。

## 使い方(手元のWindows)

初回の準備から画面を開くまでの詳しい手順は、プロジェクトの共有フォルダの `guides/haiku-check-windows.md` にあります。PowerShell(管理者ではない普通のもの)で次を実行します。

```powershell
cd ~\bot-factory
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env                          # ANTHROPIC_API_KEY を書く
.venv\Scripts\python -m factory init           # data/factory.db を作る
.venv\Scripts\python -m factory seed-sample    # 手で書いた案を10件入れる
.venv\Scripts\python -m factory ask "1+1は?"   # Haiku に1問。費用帳簿に1行つく
.venv\Scripts\python -m factory dashboard      # http://localhost:8800 を開く
.venv\Scripts\python -m pytest -q
```

Mac・Linux では `py` を `python3` に、`.venv\Scripts\python` を `.venv/bin/python` に、`copy` を `cp` に読み替えます。

BOTを試す(段階2):

```powershell
.venv\Scripts\python -m factory check-bot bots\sample_breakout   # 静的検査と自動テスト(過去30日の価格)
.venv\Scripts\python -m factory paper-ff bots\sample_breakout    # 早送りのpaper評価と反証(開発用)
```

BOTは `bots/<名前>/` に `bot.yaml`(数字)と `strategy.py`(入る・出る条件)を置く。書き方は `bots/_template/` を参照。
段階3bから、実装工程は `bots/_template/base.py`(土台)もBOTのフォルダにコピーし、BOTはそのコピーで動く(1本ずつ自己完結。
土台を直しても既存のBOTには効かない)。`base.py` のないBOT(段階3bより前のもの)は工場の `factory/engine.py` で動く。

工場を1周させる(段階3):

```powershell
.venv\Scripts\python -m factory run-daily                  # 探索〜実装、paper評価・反証。結果は data\logs\factory-日付.log にも残る
.venv\Scripts\python -m factory run-daily --skip-explore   # 順位表と資金調達料の探索をせずに、それ以外の工程だけ
```

1日1回の順番: 棚卸し(週1回・型ごと・メモのまとめ) → 前回のAIの答えの受け取り → paper評価・反証 → 探索の入口2(資金調達料の歪み) → 探索の入口1(財布分析) → 調査 → 新ロジック開発 → 実装・修理。
毎日自動で動かすには、Windowsのタスクスケジューラに `run-daily.cmd` を登録する(手順は共有フォルダの `guides/run-daily-windows.md`)。
実装工程が作ったBOTは `bots/B-0001/` などに置かれる(gitには入れない)。`paper-ff bots\B-0001` で過去30日の早送りも見られる。

段階3bで足したもの:

```powershell
.venv\Scripts\python -m factory funding-check   # 探索の入口2が今どの銘柄を案にするかを見る(登録しない。開発用)
.venv\Scripts\python -m factory watchdog        # 見張り役: 画面と価格の受け取り口を起こし、落ちたら再起動する(Ctrl+C で全部止まる)
```

- 見張り役は 画面(dashboard)と 価格の受け取り口(feed)を別々のプロセスで動かし、1分ごとに生死とメモリ(上限1GB)を見る。1時間に3回落ちたら再起動をやめ、画面の「要判断」に出す。見張り役を使う時は、`dashboard` を別に起動しない(同じ番号の口を取り合う)
- ログは全プロセス共通で `data\logs\factory-YYYYMMDD.log` の1か所。画面の「ログ」(http://localhost:8800/logs)から日付と工程で引ける

そのほか: `add-idea`(照合してから案を登録。`--type 歪み --source funding` も可)、`add-memo`(否決メモを残す)。
