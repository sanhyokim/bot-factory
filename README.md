# bot-factory

儲かる可能性が高いBOTを、人手をかけずに探し続ける工場。仕様の正本は [docs/SPEC.md](docs/SPEC.md)、現状の要約は [docs/snapshot.md](docs/snapshot.md)。

## 使い方(手元のWindows)

初回の準備から画面を開くまでの詳しい手順は、プロジェクトの共有フォルダの `guides/haiku-check-windows.md` にあります。コマンド プロンプトで次を実行します。

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        & rem ANTHROPIC_API_KEY を書く
python -m factory init        & rem data/factory.db を作る
python -m factory seed-sample & rem 手で書いた案を10件入れる
python -m factory ask "1+1は?"  & rem Haiku に1問。費用帳簿に1行つく
python -m factory dashboard   & rem http://localhost:8800 を開く
python -m pytest -q
```

Mac・Linux では `py` を `python3` に、`.venv\Scripts\activate` を `source .venv/bin/activate` に、`copy` を `cp` に読み替えます。

そのほか: `add-idea`(照合してから案を登録)、`add-memo`(否決メモを残す)、`run-daily`(段階3で中身を入れる)。
