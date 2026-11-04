"""チェーンの数字と文字の読み書き(16進・32バイトの語・ログの種類・関数の番号)。"""
from __future__ import annotations

from .keccak import keccak256

WORD = 64   # 32バイト = 16進で64文字


def topic(signature: str) -> str:
    """ログの種類。例: topic("Transfer(address,address,uint256)") → 0xddf252ad…"""
    return "0x" + keccak256(signature.encode()).hex()


def selector(signature: str) -> str:
    """関数の番号(先頭4バイト)。例: selector("transfer(address,uint256)") → 0xa9059cbb"""
    return "0x" + keccak256(signature.encode()).hex()[:8]


def to_int(value: str | int | None) -> int:
    """チェーンの答えの数字("0x1a" など)を整数に。"""
    if value is None:
        raise ValueError("数字がない")
    if isinstance(value, int):
        return value
    return int(value, 16) if value.startswith(("0x", "0X")) else int(value)


def address(value: str) -> str:
    """アドレスを小文字の 0x+40文字にそろえる。32バイトの語(ログの topic など)なら末尾の20バイトを取る。"""
    h = value[2:] if value.startswith(("0x", "0X")) else value
    if len(h) not in (40, WORD):
        raise ValueError(f"アドレスではない: {value}")
    return "0x" + h[-40:].lower()


def words(data: str) -> list[int]:
    """ログの data や eth_call の答えを、32バイトごとの整数に分ける。"""
    h = data[2:] if data.startswith(("0x", "0X")) else data
    if len(h) % WORD:
        raise ValueError(f"32バイトの区切りになっていない({len(h) // 2}バイト)")
    return [int(h[i:i + WORD], 16) for i in range(0, len(h), WORD)]


def encode_call(signature: str, *args: int | str | bool) -> str:
    """固定長の引数(uint・address・bool)だけの関数の呼び出しを作る(eth_call の data)。"""
    out = [selector(signature)[2:]]
    for a in args:
        if isinstance(a, bool):
            v = int(a)
        elif isinstance(a, int):
            if a < 0:
                raise ValueError("負の数は扱わない")
            v = a
        else:
            v = int(address(a), 16)
        out.append(f"{v:064x}")
    return "0x" + "".join(out)
