# bot-factory

儲かる可能性が高いBOTを、人手をかけずに探し続ける工場。仕様の正本は [docs/SPEC.md](docs/SPEC.md)、現状の要約は [docs/snapshot.md](docs/snapshot.md)。

## 使い方(手元のMac)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # ANTHROPIC_API_KEY を書く
python -m factory init        # data/factory.db を作る
python -m factory seed-sample # 手で書いた案を10件入れる
python -m factory dashboard   # http://localhost:8800 を開く
python -m factory ask "1+1は?"  # Haiku に1問。費用帳簿に1行つく
python -m pytest -q
```

そのほか: `add-idea`(照合してから案を登録)、`add-memo`(否決メモを残す)、`run-daily`(段階3で中身を入れる)。
