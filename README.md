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

工場を1周させる(段階3):

```powershell
.venv\Scripts\python -m factory run-daily                  # 探索〜実装、paper評価・反証。結果は data\logs\ にも残る
.venv\Scripts\python -m factory run-daily --skip-explore   # 順位表を読まずに、それ以外の工程だけ
```

1日1回の順番: 棚卸し(週1回) → 前回のAIの答えの受け取り → paper評価・反証 → 探索 → 調査 → 新ロジック開発 → 実装・修理。
毎日自動で動かすには、Windowsのタスクスケジューラに `run-daily.cmd` を登録する(手順は共有フォルダの `guides/run-daily-windows.md`)。
実装工程が作ったBOTは `bots/B-0001/` などに置かれる(gitには入れない)。`paper-ff bots\B-0001` で過去30日の早送りも見られる。

そのほか: `add-idea`(照合してから案を登録)、`add-memo`(否決メモを残す)。
