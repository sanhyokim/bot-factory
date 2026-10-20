"""BOTの「入る条件」「出る条件」。AIが書き換えるのはこのファイルの関数の中だけ。

- history: 今の足までのローソク足(先の足は見えない)。history[-1] が今閉じた足
    各足は .t(始まりの時刻・ミリ秒) .o .h .l .c(始値・高値・安値・終値) .v(出来高)
    便利な関数: history.closes(n) / highs(n) / lows(n) / volumes(n) は直近n本のリスト
- 使えるのは math と statistics だけ。ファイルの読み書きや外部通信は書けない(検査で弾く)
- 注文・記録・利益確定・損切り・上限額・レバレッジ1倍・同時保有1は土台が受け持つ
"""

LOOKBACK = 20


def entry_signal(history):
    """入るなら "long"(買い)か "short"(売り)、入らないなら None を返す。"""
    if len(history) <= LOOKBACK:
        return None
    return None


def exit_signal(history, position):
    """利益確定・損切りより先に手仕舞いたい時だけ True。position.side / entry_price / bars_held が使える。"""
    return False
