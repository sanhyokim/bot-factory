"""BOTの「入る条件」「出る条件」。AIが書き換えるのはこのファイルの関数の中だけ。

- history: 今の足までのローソク足(先の足は見えない)。history[-1] が今閉じた足
    各足は .t(始まりの時刻・ミリ秒) .o .h .l .c(始値・高値・安値・終値) .v(出来高)
    便利な関数: history.closes(n) / highs(n) / lows(n) / volumes(n) は直近n本のリスト
    history.funding_rates(n) は直近n回の1時間ごとの資金調達料のリスト(古い順。最後が一番新しい)。
    率は小数で 0.0001 = 0.01%。プラスなら買い側が払い売り側が受け取る、マイナスならその逆
- 歪み型(資金調達料)の書き方: 一番新しい率が +x% 以上なら "short"(売りで受け取る)、−x% 以下なら "long"。
    出る条件は、率が ±y% の内側に戻ったら True。x・y は % なので、率と比べる時は 100 で割る
- 使えるのは math と statistics だけ。ファイルの読み書きや外部通信は書けない(検査で弾く)
- 注文・記録・利益確定・損切り・最長保有・上限額・レバレッジ1倍・同時保有1は土台(base.py)が受け持つ
"""

LOOKBACK = 20


def entry_signal(history):
    """入るなら "long"(買い)か "short"(売り)、入らないなら None を返す。"""
    if len(history) <= LOOKBACK:
        return None
    return None


def exit_signal(history, position):
    """利益確定・損切り・最長保有より先に手仕舞いたい時だけ True。position.side / entry_price / bars_held が使える。"""
    return False
